from __future__ import annotations

# ruff: noqa: B008 - FastAPI dependency injection intentionally uses Depends in defaults.
import asyncio
import hashlib
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .adapters.llm import OpenAICompatibleDecisionEngine
from .auth import GuiSession
from .config import (
    FeishuBotConfig,
    LLMConfig,
    OrchestrationResourceConfig,
    QQBotConfig,
    QQGroupAssignment,
    Settings,
    resolve_secret,
)
from .container import Container
from .qq_status import classify_qq_connection
from .security import FailureLimiter


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)


class PasswordPayload(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=14, max_length=512)


class ProfilePayload(BaseModel):
    username: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    current_password: str = Field(min_length=1, max_length=512)


class UserCreatePayload(BaseModel):
    username: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    password: str = Field(min_length=14, max_length=512)


class UserPasswordResetPayload(BaseModel):
    password: str = Field(min_length=14, max_length=512)


class BotIdentityChanges(BaseModel):
    qq_created: list[str] = Field(default_factory=list, max_length=1000)
    qq_deleted: list[str] = Field(default_factory=list, max_length=1000)
    feishu_created: list[str] = Field(default_factory=list, max_length=1000)
    feishu_deleted: list[str] = Field(default_factory=list, max_length=1000)


class SettingsPayload(BaseModel):
    config: dict[str, Any]
    revision: str = Field(default="", max_length=64)
    identity_changes: BotIdentityChanges = Field(default_factory=BotIdentityChanges)


class LLMConnectionTestPayload(BaseModel):
    driver: Literal["openai_compatible", "rules"] = "rules"
    base_url: str = Field(default="", min_length=1, max_length=2000)
    api_key: str = Field(default="", max_length=4096)
    model: str = Field(default="", min_length=1, max_length=256)
    timeout_seconds: float = Field(default=30, ge=1, le=300)
    json_response_format: bool = True


PLATFORM_SETTINGS_SECTIONS = {
    "app",
    "llm",
    "join_approval",
    "moderation",
    "announcements",
    "runtime",
    "retention",
    "gui",
}
ORCHESTRATION_SETTINGS_SECTIONS = {"qq", "feishu", "orchestration"}
GROUP_RECORD_KINDS = {"messages", "announcements", "joins", "moderation"}
RECORD_TIME_RANGES = {"all", "today", "week", "month", "half_year", "year"}


def _record_range_cutoff(
    time_range: str,
    timezone_offset_minutes: int = 0,
    *,
    now: datetime | None = None,
) -> datetime | None:
    if time_range not in RECORD_TIME_RANGES:
        raise ValueError(f"Unsupported record time range: {time_range}")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    if time_range == "all":
        return None
    if time_range == "today":
        local_zone = timezone(timedelta(minutes=-timezone_offset_minutes))
        local_now = current.astimezone(local_zone)
        return local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    days = {"week": 7, "month": 30, "half_year": 183, "year": 365}[time_range]
    return current - timedelta(days=days)


def _settings_revision(settings: Settings) -> str:
    payload = json.dumps(
        settings.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _merge_settings_sections(
    submitted: dict[str, Any], current: Settings, allowed_sections: set[str]
) -> dict[str, Any]:
    """Merge one GUI domain without allowing it to overwrite the other domain."""
    candidate = current.model_dump(mode="json")
    for section in allowed_sections:
        if section in submitted:
            candidate[section] = submitted[section]
    return candidate


def _redacted_settings_sections(settings: Settings, sections: set[str]) -> dict[str, Any]:
    redacted = settings.redacted_dict()
    return {section: redacted[section] for section in sections}


def _validate_bot_identity_changes(
    submitted: dict[str, Any], current: Settings, changes: BotIdentityChanges
) -> None:
    """Require Bot creation and deletion to be explicit; ordinary edits cannot rename IDs."""

    def incoming_ids(section: str) -> set[str]:
        value = submitted.get(section)
        if not isinstance(value, dict) or not isinstance(value.get("bots"), list):
            raise ValueError(f"{section}.bots 必须是列表")
        return {str(bot.get("id", "")) for bot in value["bots"] if isinstance(bot, dict)}

    platforms = (
        (
            "qq",
            {bot.id for bot in current.effective_qq_bots()},
            changes.qq_created,
            changes.qq_deleted,
        ),
        (
            "feishu",
            {bot.id for bot in current.effective_feishu_bots()},
            changes.feishu_created,
            changes.feishu_deleted,
        ),
    )
    for platform, current_ids, created_list, deleted_list in platforms:
        created = set(created_list)
        deleted = set(deleted_list)
        if len(created) != len(created_list) or len(deleted) != len(deleted_list):
            raise ValueError(f"{platform} Bot 身份变更清单不能包含重复 ID")
        if created & deleted:
            raise ValueError(f"{platform} Bot 不能在同一次保存中同时新增和删除同一 ID")
        if created & current_ids:
            raise ValueError(f"{platform} Bot 新增清单包含已存在的 ID")
        if not deleted <= current_ids:
            raise ValueError(f"{platform} Bot 删除清单包含不存在的 ID")
        expected = (current_ids - deleted) | created
        actual = incoming_ids(platform)
        if actual != expected:
            removed = sorted(current_ids - actual)
            added = sorted(actual - current_ids)
            raise ValueError(
                f"{platform} Bot 内部 ID 不可直接修改；请使用资源编排的新增/删除操作"
                f"（移除={removed}，新增={added}）"
            )


def _preserve_masked_secrets(candidate: dict[str, Any], current: Settings) -> dict[str, Any]:
    secret_values = {
        ("app", "admin_api_token"): current.app.admin_api_token,
        ("llm", "api_key"): current.llm.api_key,
        ("gui", "bootstrap_password"): current.gui.bootstrap_password,
    }
    for (section, key), original in secret_values.items():
        section_value = candidate.setdefault(section, {})
        if not isinstance(section_value, dict):
            continue
        if section_value.get(key) in (None, "", "***"):
            section_value[key] = original

    sensitive_edits = current.gui.allow_sensitive_settings_edits
    if not sensitive_edits:
        incoming_app = candidate.get("app")
        requested_dry_run = (
            incoming_app.get("dry_run", current.app.dry_run)
            if isinstance(incoming_app, dict)
            else current.app.dry_run
        )
        if not isinstance(requested_dry_run, bool):
            raise ValueError("app.dry_run 必须是布尔值")
        candidate["app"] = current.app.model_dump(mode="json")
        candidate["app"]["dry_run"] = requested_dry_run
        candidate["gui"] = current.gui.model_dump(mode="json")
        llm = candidate.setdefault("llm", {})
        if not isinstance(llm, dict):
            raise ValueError("llm 配置必须是对象")

    qq_section = candidate.setdefault("qq", {})
    if isinstance(qq_section, dict) and isinstance(qq_section.get("bots"), list):
        current_bots = {bot.id: bot for bot in current.effective_qq_bots()}
        locked_keys = {
            "onebot_base_url",
            "access_token",
            "access_token_file",
            "webhook_secret",
            "request_timeout_seconds",
            "webui_base_url",
            "webui_token",
            "webui_token_file",
            "webui_public_url",
            "webui_public_port",
            "qrcode_path",
            "announcement_actions",
        }
        mode_connection_keys = {
            "onebot_base_url",
            "access_token",
            "access_token_file",
            "webhook_secret",
            "webui_base_url",
            "webui_token",
            "webui_token_file",
            "qrcode_path",
        }
        for incoming in qq_section["bots"]:
            if not isinstance(incoming, dict):
                continue
            incoming_id = str(incoming.get("id", ""))
            existing = current_bots.get(incoming_id)
            if existing is None:
                if not sensitive_edits:
                    connection_mode = (
                        "bundled_napcat"
                        if incoming.get("connection_mode") == "bundled_napcat"
                        else "external"
                    )
                    safe_defaults = QQBotConfig(
                        id=incoming_id or "new-bot",
                        enabled=False,
                        connection_mode=connection_mode,
                    ).model_dump(mode="json")
                    for key in locked_keys:
                        incoming[key] = safe_defaults[key]
                    if connection_mode == "external":
                        secret_root = f"data/secrets/qq/{incoming_id or 'new-bot'}"
                        incoming["access_token_file"] = f"{secret_root}/onebot.token"
                        incoming["webui_token_file"] = f"{secret_root}/webui.token"
                        incoming["qrcode_path"] = (
                            f"data/napcat-cache/{incoming_id or 'new-bot'}/qrcode.png"
                        )
                continue
            for key in ("access_token", "webui_token", "webhook_secret"):
                if incoming.get(key) in (None, "", "***"):
                    incoming[key] = getattr(existing, key)
            if not sensitive_edits:
                existing_data = existing.model_dump(mode="json")
                connection_mode = (
                    "bundled_napcat"
                    if incoming.get("connection_mode") == "bundled_napcat"
                    else "external"
                )
                if connection_mode == existing.connection_mode:
                    for key in locked_keys:
                        incoming[key] = existing_data[key]
                else:
                    safe_defaults = QQBotConfig(
                        id=incoming_id,
                        enabled=existing.enabled,
                        connection_mode=connection_mode,
                    ).model_dump(mode="json")
                    for key in locked_keys:
                        incoming[key] = (
                            safe_defaults[key]
                            if key in mode_connection_keys
                            else existing_data[key]
                        )
                    if connection_mode == "external":
                        secret_root = f"data/secrets/qq/{incoming_id}"
                        incoming["access_token_file"] = f"{secret_root}/onebot.token"
                        incoming["webui_token_file"] = f"{secret_root}/webui.token"
                        incoming["qrcode_path"] = f"data/napcat-cache/{incoming_id}/qrcode.png"

    feishu = candidate.setdefault("feishu", {})
    if isinstance(feishu, dict) and isinstance(feishu.get("bots"), list):
        current_bots = {bot.id: bot for bot in current.effective_feishu_bots()}
        safe_defaults = FeishuBotConfig(id="new-bot", enabled=False).model_dump(mode="json")
        locked_keys = {
            "driver",
            "executable",
            "command_templates",
            "archive_payload_stdin",
            "extra_environment",
        }
        for incoming in feishu["bots"]:
            if not isinstance(incoming, dict):
                continue
            existing = current_bots.get(str(incoming.get("id", "")))
            if existing is None:
                if not sensitive_edits:
                    incoming["enabled"] = False
                    for key in locked_keys:
                        incoming[key] = safe_defaults[key]
                continue
            environment = incoming.get("extra_environment")
            if environment in (None, "***"):
                incoming["extra_environment"] = existing.extra_environment
            elif isinstance(environment, dict):
                for key, value in list(environment.items()):
                    if value in (None, "", "***") and key in existing.extra_environment:
                        environment[key] = existing.extra_environment[key]
            if not sensitive_edits:
                existing_data = existing.model_dump(mode="json")
                for key in locked_keys:
                    incoming[key] = existing_data[key]
    return candidate


def register_gui(
    app: FastAPI,
    get_container: Callable[[], Container],
    get_settings: Callable[[], Settings],
    reload_settings: Callable[[Settings], Awaitable[list[str]]],
    config_path: str | Path,
) -> None:
    web_root = Path(__file__).with_name("web")
    configuration_path = Path(config_path)
    app.mount("/assets", StaticFiles(directory=web_root), name="gui-assets")
    # Existing bookmarks and cached pages keep working while the GUI moves to
    # page-specific URLs.
    app.mount("/gui/assets", StaticFiles(directory=web_root), name="legacy-gui-assets")
    login_failures = FailureLimiter()

    def current_session(
        neoqbot_session: str | None = Cookie(default=None, alias="neoqbot_session"),
    ) -> GuiSession:
        session = get_container().auth.session(neoqbot_session)
        if session is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return session

    def csrf_session(
        session: GuiSession = Depends(current_session),
        x_csrf_token: str | None = Header(default=None),
    ) -> GuiSession:
        if not x_csrf_token or not secrets.compare_digest(x_csrf_token, session.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        return session

    def ready_admin(
        session: GuiSession = Depends(current_session),
    ) -> GuiSession:
        if session.must_change_password:
            raise HTTPException(status_code=403, detail="首次登录必须先修改随机初始密码")
        return session

    def ready_admin_csrf(
        session: GuiSession = Depends(csrf_session),
    ) -> GuiSession:
        if session.must_change_password:
            raise HTTPException(status_code=403, detail="首次登录必须先修改随机初始密码")
        return session

    def administrator(
        session: GuiSession = Depends(ready_admin),
    ) -> GuiSession:
        if session.role != "admin":
            raise HTTPException(status_code=403, detail="只有管理员可以管理平台用户")
        return session

    def administrator_csrf(
        session: GuiSession = Depends(ready_admin_csrf),
    ) -> GuiSession:
        if session.role != "admin":
            raise HTTPException(status_code=403, detail="只有管理员可以管理平台用户")
        return session

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @app.get("/gui", include_in_schema=False)
    async def gui_redirect() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @app.get("/gui/", include_in_schema=False)
    async def gui_index_redirect() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @app.get("/dashboard", include_in_schema=False)
    @app.get("/orchestration", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    @app.get("/records", include_in_schema=False)
    @app.get("/user", include_in_schema=False)
    async def gui_index() -> FileResponse:
        return FileResponse(web_root / "index.html")

    @app.post("/api/gui/settings/llm/test")
    async def gui_test_llm_connection(
        payload: LLMConnectionTestPayload,
        _: GuiSession = Depends(ready_admin_csrf),
    ) -> dict[str, str | bool]:
        if payload.driver != "openai_compatible":
            raise HTTPException(status_code=422, detail="请选择 OpenAI 兼容驱动后再测试连接")
        current = get_settings()
        try:
            config = LLMConfig(
                driver="openai_compatible",
                base_url=payload.base_url.strip(),
                api_key=payload.api_key or current.llm.api_key,
                model=payload.model.strip(),
                timeout_seconds=payload.timeout_seconds,
                max_retries=0,
                json_response_format=payload.json_response_format,
            )
            engine = OpenAICompatibleDecisionEngine(
                config, current.join_approval, current.moderation
            )
            try:
                await engine.test_connection()
            finally:
                await engine.close()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"模型连接失败：{type(exc).__name__}",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"模型测试失败：{type(exc).__name__}",
            ) from exc
        return {"ok": True, "message": "模型已正常响应", "model": config.model}

    @app.post("/api/gui/auth/login")
    async def gui_login(
        payload: LoginPayload, request: Request, response: Response
    ) -> dict[str, Any]:
        address = request.client.host if request.client else "unknown"
        address_key = f"login-ip:{address}"
        window_seconds = 300
        if login_failures.blocked(address_key, 8, window_seconds):
            raise HTTPException(
                status_code=429,
                detail="登录失败次数过多，请五分钟后再试",
                headers={"Retry-After": str(window_seconds)},
            )
        result = await asyncio.to_thread(
            get_container().auth.login, payload.username, payload.password
        )
        if result is None:
            login_failures.hit(address_key, window_seconds)
            get_container().database.audit(
                "gui_login", "failed", "admin_user", payload.username, {"address": address}
            )
            await asyncio.sleep(0.3 + secrets.randbelow(151) / 1000)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        login_failures.clear(address_key)
        token, session = result
        settings = get_settings()
        response.set_cookie(
            "neoqbot_session",
            token,
            max_age=settings.gui.session_hours * 3600,
            httponly=True,
            secure=settings.gui.secure_cookie,
            samesite="strict",
            path="/",
        )
        get_container().database.audit(
            "gui_login", "success", "admin_user", session.username, {"address": address}
        )
        return {
            "username": session.username,
            "csrf_token": session.csrf_token,
            "must_change_password": session.must_change_password,
            "role": session.role,
        }

    @app.get("/api/gui/auth/session")
    async def gui_session(
        session: GuiSession = Depends(current_session),
    ) -> dict[str, Any]:
        return {
            "username": session.username,
            "csrf_token": session.csrf_token,
            "must_change_password": session.must_change_password,
            "role": session.role,
        }

    @app.post("/api/gui/auth/logout")
    async def gui_logout(
        response: Response,
        _: GuiSession = Depends(csrf_session),
        neoqbot_session: str | None = Cookie(default=None, alias="neoqbot_session"),
    ) -> dict[str, bool]:
        get_container().auth.logout(neoqbot_session)
        settings = get_settings()
        response.delete_cookie(
            "neoqbot_session",
            path="/",
            secure=settings.gui.secure_cookie,
            httponly=True,
            samesite="strict",
        )
        return {"ok": True}

    @app.post("/api/gui/auth/password")
    async def gui_change_password(
        payload: PasswordPayload,
        session: GuiSession = Depends(csrf_session),
    ) -> dict[str, bool]:
        try:
            changed = await asyncio.to_thread(
                get_container().auth.change_password,
                session,
                payload.current_password,
                payload.new_password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not changed:
            raise HTTPException(status_code=400, detail="当前密码错误")
        get_container().database.audit("gui_password", "changed", "admin_user", session.username)
        return {"ok": True}

    @app.put("/api/gui/auth/profile")
    async def gui_update_profile(
        payload: ProfilePayload,
        session: GuiSession = Depends(ready_admin_csrf),
    ) -> dict[str, str]:
        username = payload.username.strip()
        if (
            username.casefold() == get_settings().gui.bootstrap_username.casefold()
            and session.role != "admin"
        ):
            raise HTTPException(status_code=400, detail="该用户名为平台管理员保留")
        try:
            changed = await asyncio.to_thread(
                get_container().auth.rename_user,
                session,
                username,
                payload.current_password,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not changed:
            raise HTTPException(status_code=409, detail="该用户名已存在")
        get_container().database.audit(
            "gui_profile_rename",
            "renamed",
            "gui_user",
            username,
            {"previous_username": session.username, "role": session.role},
        )
        return {"username": username, "role": session.role}

    @app.get("/api/gui/users")
    async def gui_users(
        _: GuiSession = Depends(administrator),
    ) -> dict[str, Any]:
        users = await asyncio.to_thread(get_container().database.list_gui_users)
        for user in users:
            user["must_change_password"] = bool(user["must_change_password"])
            user["active_sessions"] = int(user["active_sessions"])
        return {"users": users}

    @app.post("/api/gui/users", status_code=201)
    async def gui_create_user(
        payload: UserCreatePayload,
        session: GuiSession = Depends(administrator_csrf),
    ) -> dict[str, Any]:
        username = payload.username.strip()
        if username.casefold() == get_settings().gui.bootstrap_username.casefold():
            raise HTTPException(status_code=400, detail="该用户名为平台管理员保留")
        try:
            created = await asyncio.to_thread(
                get_container().auth.create_operator, username, payload.password
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not created:
            raise HTTPException(status_code=409, detail="该用户名已存在")
        get_container().database.audit(
            "gui_user_create",
            "created",
            "gui_user",
            username,
            {"actor": session.username, "role": "operator"},
        )
        return {"ok": True, "username": username, "role": "operator"}

    @app.put("/api/gui/users/{username}/password")
    async def gui_reset_user_password(
        username: str,
        payload: UserPasswordResetPayload,
        session: GuiSession = Depends(administrator_csrf),
    ) -> dict[str, bool]:
        try:
            changed = await asyncio.to_thread(
                get_container().auth.reset_operator_password, username, payload.password
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not changed:
            raise HTTPException(status_code=404, detail="子用户不存在或不可重置")
        get_container().database.audit(
            "gui_user_password_reset",
            "reset",
            "gui_user",
            username,
            {"actor": session.username},
        )
        return {"ok": True}

    @app.delete("/api/gui/users/{username}")
    async def gui_delete_user(
        username: str,
        session: GuiSession = Depends(administrator_csrf),
    ) -> dict[str, bool]:
        deleted = await asyncio.to_thread(get_container().database.delete_gui_user, username)
        if not deleted:
            raise HTTPException(status_code=404, detail="子用户不存在或不可删除")
        get_container().database.audit(
            "gui_user_delete",
            "deleted",
            "gui_user",
            username,
            {"actor": session.username},
        )
        return {"ok": True}

    @app.get("/api/gui/dashboard")
    async def gui_dashboard(
        _: GuiSession = Depends(ready_admin),
    ) -> dict[str, Any]:
        settings = get_settings()
        connected_resources = {
            endpoint
            for edge in settings.orchestration.edges
            if edge.enabled
            for endpoint in (edge.source, edge.target)
        }
        assignments = settings.qq_group_assignments()

        def task_flags(bot_id: str) -> dict[str, bool]:
            scoped = [item for item in assignments if item.bot_id == bot_id]
            return {
                "join_management": any(item.tasks.join_management.enabled for item in scoped),
                "message_detection": any(item.tasks.message_detection.enabled for item in scoped),
                "announcement_sync": any(item.tasks.announcement_sync.enabled for item in scoped),
            }

        return {
            "version": __version__,
            "environment": settings.app.environment,
            "dry_run": settings.app.dry_run,
            "queue_size": get_container().runtime.queue.qsize(),
            "counts": get_container().database.counts(),
            "diagnostics": settings.diagnostics(),
            "managed_groups": settings.managed_group_ids(),
            "orchestration": {
                "resources": len(settings.orchestration.resources),
                "edges": sum(1 for edge in settings.orchestration.edges if edge.enabled),
                "groups": sum(
                    1
                    for resource in settings.orchestration.resources
                    if resource.kind in {"qq_group", "feishu_group"}
                ),
                "knowledge_bases": sum(
                    1
                    for resource in settings.orchestration.resources
                    if resource.kind == "knowledge_base"
                ),
                "disconnected": sum(
                    1
                    for resource in settings.orchestration.resources
                    if resource.enabled and resource.id not in connected_resources
                ),
            },
            "bots": [
                {
                    "id": bot.id,
                    "name": bot.name,
                    "enabled": bot.enabled,
                    "groups": settings.bot_group_ids(bot.id),
                    "task_presence": task_flags(bot.id),
                }
                for bot in settings.effective_qq_bots()
            ],
        }

    @app.get("/api/gui/settings")
    async def gui_settings(
        _: GuiSession = Depends(ready_admin),
    ) -> dict[str, Any]:
        settings = get_settings()
        return {
            "config": _redacted_settings_sections(settings, PLATFORM_SETTINGS_SECTIONS),
            "revision": _settings_revision(settings),
        }

    @app.put("/api/gui/settings")
    async def gui_save_settings(
        payload: SettingsPayload,
        session: GuiSession = Depends(ready_admin_csrf),
    ) -> dict[str, Any]:
        current = get_settings()
        current_revision = _settings_revision(current)
        if payload.revision and not secrets.compare_digest(payload.revision, current_revision):
            get_container().database.audit(
                "gui_settings",
                "conflict",
                "admin_user",
                session.username,
                {
                    "submitted_revision": payload.revision[:12],
                    "current_revision": current_revision[:12],
                },
            )
            raise HTTPException(
                status_code=409,
                detail="配置已被其他会话更新，请刷新后重新应用更改",
            )
        try:
            candidate = _merge_settings_sections(
                payload.config, current, PLATFORM_SETTINGS_SECTIONS
            )
            merged = _preserve_masked_secrets(candidate, current)
            updated = Settings.model_validate(merged)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            updated.save(configuration_path)
            restart_required = await reload_settings(updated)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"配置文件写入失败：{exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"配置已写入，但热加载失败：{exc}") from exc
        get_container().database.audit(
            "gui_settings",
            "saved",
            "configuration",
            str(configuration_path),
            {"restart": restart_required},
        )
        return {
            "ok": True,
            "restart_required": restart_required,
            "diagnostics": updated.diagnostics(),
            "revision": _settings_revision(updated),
        }

    @app.get("/api/gui/orchestration")
    async def gui_orchestration_settings(
        _: GuiSession = Depends(ready_admin),
    ) -> dict[str, Any]:
        settings = get_settings()
        return {
            "config": _redacted_settings_sections(settings, ORCHESTRATION_SETTINGS_SECTIONS),
            "revision": _settings_revision(settings),
            "sensitive_edits_allowed": settings.gui.allow_sensitive_settings_edits,
        }

    @app.put("/api/gui/orchestration")
    async def gui_save_orchestration(
        payload: SettingsPayload,
        session: GuiSession = Depends(ready_admin_csrf),
    ) -> dict[str, Any]:
        current = get_settings()
        current_revision = _settings_revision(current)
        if payload.revision and not secrets.compare_digest(payload.revision, current_revision):
            get_container().database.audit(
                "gui_orchestration",
                "conflict",
                "admin_user",
                session.username,
                {
                    "submitted_revision": payload.revision[:12],
                    "current_revision": current_revision[:12],
                },
            )
            raise HTTPException(
                status_code=409,
                detail="配置已被其他会话更新，请刷新后重新应用更改",
            )
        try:
            _validate_bot_identity_changes(payload.config, current, payload.identity_changes)
            candidate = _merge_settings_sections(
                payload.config, current, ORCHESTRATION_SETTINGS_SECTIONS
            )
            merged = _preserve_masked_secrets(candidate, current)
            updated = Settings.model_validate(merged)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            updated.save(configuration_path)
            restart_required = await reload_settings(updated)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"配置文件写入失败：{exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"配置已写入，但热加载失败：{exc}") from exc
        get_container().database.audit(
            "gui_orchestration",
            "saved",
            "configuration",
            str(configuration_path),
            {
                "restart": restart_required,
                "qq_bots": len(updated.qq.bots),
                "feishu_bots": len(updated.feishu.bots),
                "resources": len(updated.orchestration.resources),
                "edges": len(updated.orchestration.edges),
            },
        )
        return {
            "ok": True,
            "restart_required": restart_required,
            "diagnostics": updated.diagnostics(),
            "revision": _settings_revision(updated),
        }

    @app.get("/api/gui/records/{kind}")
    async def gui_records(
        kind: str,
        _: GuiSession = Depends(ready_admin),
        limit: int = Query(default=50, ge=1, le=200),
        group_id: str | None = Query(default=None, min_length=1, max_length=160),
        bot_id: str | None = Query(default=None, min_length=1, max_length=64),
        search: str | None = Query(default=None, max_length=120),
        offset: int = Query(default=0, ge=0, le=100000),
    ) -> dict[str, Any]:
        try:
            records = get_container().database.recent_records(
                kind,
                limit + 1,
                group_id=group_id,
                bot_id=bot_id,
                search=search,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "kind": kind,
            "records": records[:limit],
            "limit": limit,
            "offset": offset,
            "has_more": len(records) > limit,
        }

    def orchestration_group_context(
        group_id: str, resource_id: str | None
    ) -> tuple[OrchestrationResourceConfig, list[QQGroupAssignment], list[dict[str, Any]]]:
        settings = get_settings()
        resource = next(
            (
                item
                for item in settings.orchestration.resources
                if item.kind in {"qq_group", "feishu_group"}
                and (
                    (
                        resource_id is not None
                        and item.id == resource_id
                        and item.external_id == group_id
                    )
                    or (resource_id is None and item.external_id and item.external_id == group_id)
                )
            ),
            None,
        )
        if resource is None:
            raise HTTPException(status_code=404, detail="资源编排中不存在这个群")
        assignments = [
            assignment
            for assignment in settings.qq_group_assignments(include_disabled=True)
            if assignment.group_id == group_id
            and (resource is None or assignment.resource_id == resource.id)
        ]
        manager_ids = {assignment.bot_id for assignment in assignments}
        managers = [
            {
                "id": bot.id,
                "name": bot.name,
                "enabled": bot.enabled,
                "assignments": [
                    {
                        "edge_id": assignment.edge_id,
                        "relation": assignment.relation,
                        "tasks": assignment.tasks.model_dump(mode="json"),
                    }
                    for assignment in assignments
                    if assignment.bot_id == bot.id
                ],
            }
            for bot in settings.effective_qq_bots()
            if bot.id in manager_ids
        ]
        return resource, assignments, managers

    @app.get("/api/gui/orchestration/group")
    async def gui_orchestration_group(
        _: GuiSession = Depends(ready_admin),
        group_id: str = Query(min_length=1, max_length=160),
        resource_id: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, Any]:
        resource, _, managers = orchestration_group_context(group_id, resource_id)
        overview = await asyncio.to_thread(
            get_container().database.group_overview,
            group_id,
            limit=limit,
        )
        return {
            "resource": resource.model_dump(mode="json"),
            "group_id": group_id,
            "managers": managers,
            **overview,
        }

    @app.get("/api/gui/orchestration/group/records")
    async def gui_orchestration_group_records(
        _: GuiSession = Depends(ready_admin),
        group_id: str = Query(min_length=1, max_length=160),
        resource_id: str | None = Query(default=None, max_length=64),
        kind: str = Query(default="messages", max_length=32),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=1000000),
        search: str | None = Query(default=None, max_length=120),
    ) -> dict[str, Any]:
        if kind not in GROUP_RECORD_KINDS:
            raise HTTPException(status_code=404, detail="未知群记录类型")
        orchestration_group_context(group_id, resource_id)
        records = await asyncio.to_thread(
            get_container().database.recent_records,
            kind,
            limit + 1,
            group_id=group_id,
            search=search,
            offset=offset,
        )
        return {
            "kind": kind,
            "records": records[:limit],
            "limit": limit,
            "offset": offset,
            "has_more": len(records) > limit,
        }

    @app.delete("/api/gui/orchestration/group/records/{kind}")
    async def gui_clear_orchestration_group_records(
        kind: str,
        session: GuiSession = Depends(ready_admin_csrf),
        group_id: str = Query(min_length=1, max_length=160),
        resource_id: str | None = Query(default=None, max_length=64),
        time_range: Literal["all", "today", "week", "month", "half_year", "year"] = Query(
            default="all", alias="range"
        ),
        timezone_offset_minutes: int = Query(default=0, ge=-840, le=840),
    ) -> dict[str, Any]:
        if kind not in GROUP_RECORD_KINDS:
            raise HTTPException(status_code=404, detail="未知群记录类型")
        resource, _, _ = orchestration_group_context(group_id, resource_id)
        cutoff = _record_range_cutoff(time_range, timezone_offset_minutes)
        deleted = await asyncio.to_thread(
            get_container().database.delete_group_records,
            kind,
            group_id,
            since=cutoff,
        )
        archive_result = {"records": 0, "files": 0}
        if kind == "messages":
            archive_result = await asyncio.to_thread(
                get_container().message_recorder.clear_group,
                group_id,
                since=cutoff,
            )
        details = {
            "username": session.username,
            "resource_id": resource.id,
            "kind": kind,
            "range": time_range,
            "cutoff": cutoff.isoformat() if cutoff else None,
            "database_records": deleted,
            "message_archive": archive_result,
        }
        get_container().database.audit(
            "group_records_clear", "completed", "group", group_id, details
        )
        return {"ok": True, **details}

    @app.get("/api/gui/orchestration/group/export")
    async def gui_export_orchestration_group_records(
        session: GuiSession = Depends(ready_admin),
        group_id: str = Query(min_length=1, max_length=160),
        resource_id: str | None = Query(default=None, max_length=64),
        kind: Literal["all", "messages", "announcements", "joins", "moderation"] = Query(
            default="all"
        ),
        time_range: Literal["all", "today", "week", "month", "half_year", "year"] = Query(
            default="all", alias="range"
        ),
        timezone_offset_minutes: int = Query(default=0, ge=-840, le=840),
    ) -> Response:
        resource, _, managers = orchestration_group_context(group_id, resource_id)
        cutoff = _record_range_cutoff(time_range, timezone_offset_minutes)
        selected_kinds = sorted(GROUP_RECORD_KINDS) if kind == "all" else [kind]
        records: dict[str, list[dict[str, Any]]] = {}
        for record_kind in selected_kinds:
            records[record_kind] = await asyncio.to_thread(
                get_container().database.all_group_records,
                record_kind,
                group_id,
                since=cutoff,
            )
        exported_at = datetime.now(UTC).isoformat()
        payload = {
            "schema": "neoqbot.group-records.v1",
            "exported_at": exported_at,
            "exported_by": session.username,
            "range": time_range,
            "cutoff": cutoff.isoformat() if cutoff else None,
            "group": resource.model_dump(mode="json"),
            "managers": managers,
            "counts": {record_kind: len(items) for record_kind, items in records.items()},
            "records": records,
        }
        get_container().database.audit(
            "group_records_export",
            "completed",
            "group",
            group_id,
            {
                "username": session.username,
                "resource_id": resource.id,
                "kind": kind,
                "range": time_range,
                "counts": payload["counts"],
            },
        )
        safe_group = re.sub(r"[^A-Za-z0-9_.-]+", "-", group_id).strip("-.") or "group"
        filename = f"neoqbot-group-{safe_group}-{kind}-{time_range}.json"
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/gui/jobs/{job}")
    async def gui_run_job(
        job: str,
        _: GuiSession = Depends(ready_admin_csrf),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        runtime = get_container().runtime
        if job == "moderation":
            result = (
                await runtime.run_bot_moderation(bot_id)
                if bot_id
                else await runtime.run_all_moderation()
            )
        elif job == "announcements":
            result = (
                await runtime.sync_bot_announcements(bot_id)
                if bot_id
                else await runtime.sync_all_announcements()
            )
        elif job == "maintenance":
            result = await runtime.run_maintenance()
        else:
            raise HTTPException(status_code=404, detail="未知任务")
        return {"ok": True, "result": result}

    def qq_qrcode_path(bot_id: str | None) -> Path:
        bot = get_settings().qq_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="未知 QQ Bot")
        path = Path(bot.qrcode_path)
        try:
            if not path.is_file():
                raise HTTPException(status_code=404, detail="NapCat 尚未生成登录二维码")
            stat = path.stat()
            if stat.st_size > 5 * 1024 * 1024:
                raise HTTPException(status_code=422, detail="NapCat 二维码文件异常")
            age_seconds = max(0.0, time.time() - stat.st_mtime)
            if age_seconds > 90:
                raise HTTPException(
                    status_code=410,
                    detail="NapCat 二维码已过期，请点击“获取新二维码”",
                )
            with path.open("rb") as handle:
                if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise HTTPException(status_code=422, detail="NapCat 二维码不是有效 PNG")
        except HTTPException:
            raise
        except OSError as exc:
            raise HTTPException(status_code=404, detail=f"无法读取 NapCat 二维码：{exc}") from exc
        return path

    def qrcode_fingerprint(path: Path) -> tuple[int, int, str] | None:
        try:
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return stat.st_mtime_ns, stat.st_size, digest
        except OSError:
            return None

    @app.get("/api/gui/integrations/qq/qrcode")
    async def gui_qq_qrcode(
        _: GuiSession = Depends(ready_admin),
        bot_id: str | None = Query(default=None),
    ) -> FileResponse:
        path = await asyncio.to_thread(qq_qrcode_path, bot_id)
        return FileResponse(
            path,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store, max-age=0, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.post("/api/gui/integrations/qq/qrcode/refresh")
    async def gui_qq_qrcode_refresh(
        _: GuiSession = Depends(ready_admin_csrf),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        bot = get_settings().qq_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="未知 QQ Bot")
        path = Path(bot.qrcode_path)
        before = await asyncio.to_thread(qrcode_fingerprint, path)
        try:
            result = await get_container().napcat_clients[bot.id].refresh_qrcode()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"NapCat 刷新二维码失败：{exc}") from exc
        deadline = time.monotonic() + 8
        after = before
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            after = await asyncio.to_thread(qrcode_fingerprint, path)
            if after is not None and after != before:
                break
        if after is None or after == before:
            raise HTTPException(status_code=504, detail="NapCat 已接收刷新请求，但二维码文件未更新")
        return {
            "ok": True,
            "bot_id": bot.id,
            "changed": True,
            "updated_at": after[0] / 1_000_000_000,
            "result": result,
        }

    @app.post("/api/gui/integrations/qq/relogin")
    async def gui_qq_relogin(
        _: GuiSession = Depends(ready_admin_csrf),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        bot = get_settings().qq_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="未知 QQ Bot")
        path = Path(bot.qrcode_path)
        before = await asyncio.to_thread(qrcode_fingerprint, path)
        try:
            result = await get_container().napcat_clients[bot.id].relogin()
        except Exception as exc:
            if "QQ Is Logined" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail="QQ 当前仍在线，请先在手机 QQ 中退出该账号后再重新登录",
                ) from exc
            raise HTTPException(status_code=502, detail=f"退出当前 QQ 登录态失败：{exc}") from exc
        deadline = time.monotonic() + 8
        after = before
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            after = await asyncio.to_thread(qrcode_fingerprint, path)
            if after is not None and after != before:
                break
        if after is None or after == before:
            raise HTTPException(
                status_code=504,
                detail="NapCat 已接收重新登录请求，但二维码文件未更新",
            )
        return {
            "ok": True,
            "bot_id": bot.id,
            "changed": True,
            "updated_at": after[0] / 1_000_000_000,
            "result": result,
        }

    @app.post("/api/gui/integrations/qq/webui-token")
    async def gui_qq_webui_token(
        _: GuiSession = Depends(ready_admin_csrf),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, str]:
        bot = get_settings().qq_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="未知 QQ Bot")
        try:
            token = get_container().napcat_clients[bot.id].token()
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"bot_id": bot.id, "token": token}

    @app.get("/api/gui/integrations/qq")
    async def gui_qq_status(
        _: GuiSession = Depends(ready_admin),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        settings = get_settings()
        bots = settings.effective_qq_bots()
        if bot_id:
            bots = [bot for bot in bots if bot.id == bot_id]
            if not bots:
                raise HTTPException(status_code=404, detail="未知 QQ Bot")
        results = []
        for bot in bots:
            onebot_token_available = bool(resolve_secret(bot.access_token, bot.access_token_file))
            try:
                webui_token_available = bool(get_container().napcat_clients[bot.id].token())
            except Exception:
                webui_token_available = False
            if not bot.enabled:
                onebot_status: dict[str, Any] = {"ok": True, "enabled": False}
                napcat_status: dict[str, Any] = {"ok": True, "enabled": False}
                connection_state = "disabled"
                connection_message = "QQ Bot 已停用"
            elif not onebot_token_available and not webui_token_available:
                onebot_status = {"ok": False, "error": "OneBot Token 尚未初始化"}
                napcat_status = {"ok": False, "error": "NapCat WebUI Token 尚未初始化"}
                connection_state = "credentials_missing"
                connection_message = (
                    "内置 NapCat Secret 尚未初始化，请重新运行 Compose 初始化服务"
                    if bot.connection_mode == "bundled_napcat"
                    else "外部 OneBot/NapCat 尚未配置 Token 与独立连接参数"
                )
            else:
                onebot_result, napcat_result = await asyncio.gather(
                    get_container().qq_clients[bot.id].doctor(),
                    get_container().napcat_clients[bot.id].check_login_status(),
                    return_exceptions=True,
                )
                onebot_status = (
                    {"ok": False, "error": str(onebot_result)}
                    if isinstance(onebot_result, BaseException)
                    else onebot_result
                )
                napcat_status = (
                    {"ok": False, "error": str(napcat_result)}
                    if isinstance(napcat_result, BaseException)
                    else {"ok": True, **napcat_result}
                )
                connection_state, connection_message = classify_qq_connection(
                    onebot_status, napcat_status
                )
            results.append(
                {
                    "id": bot.id,
                    "name": bot.name,
                    "enabled": bot.enabled,
                    "connection_mode": bot.connection_mode,
                    "status": onebot_status,
                    "onebot_status": onebot_status,
                    "napcat_status": napcat_status,
                    "connection_state": connection_state,
                    "connection_message": connection_message,
                    "webhook_url": (
                        "/webhooks/onebot"
                        if bot.connection_mode == "bundled_napcat"
                        else f"/webhooks/onebot/{bot.id}"
                    ),
                    "webui_public_url": bot.webui_public_url,
                    "webui_public_port": bot.webui_public_port,
                    "qrcode_available": await asyncio.to_thread(Path(bot.qrcode_path).is_file),
                    "onebot_token_available": onebot_token_available,
                    "webui_token_available": webui_token_available,
                    "assignment_count": len(get_settings().bot_group_ids(bot.id)),
                }
            )
        if not results:
            return {
                "bots": [],
                "status": {"ok": True, "enabled": False, "reason": "no_bots"},
                "webui_public_url": "",
                "webui_public_port": None,
            }
        primary = results[0]
        return {
            "bots": results,
            "status": primary["status"],
            "webui_public_url": primary["webui_public_url"],
            "webui_public_port": primary["webui_public_port"],
        }

    @app.get("/api/gui/integrations/feishu")
    async def gui_feishu_status(
        _: GuiSession = Depends(ready_admin),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        bots = get_settings().effective_feishu_bots()
        if bot_id:
            bots = [bot for bot in bots if bot.id == bot_id]
            if not bots:
                raise HTTPException(status_code=404, detail="未知飞书 Bot")
        results = []
        for bot in bots:
            try:
                status = await get_container().feishu_clients[bot.id].doctor()
            except Exception as exc:
                status = {"ok": False, "error": str(exc)}
            results.append(
                {"id": bot.id, "name": bot.name, "enabled": bot.enabled, "status": status}
            )
        return {
            "bots": results,
            "status": (
                results[0]["status"]
                if results
                else {"ok": True, "enabled": False, "reason": "no_bots"}
            ),
        }

    @app.post("/api/gui/integrations/feishu/{action}")
    async def gui_feishu_action(
        action: str,
        _: GuiSession = Depends(ready_admin_csrf),
        bot_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        if action not in {"login", "logout"}:
            raise HTTPException(status_code=404, detail="未知飞书动作")
        bot = get_settings().feishu_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="未知飞书 Bot")
        try:
            result = await getattr(get_container().feishu_clients[bot.id], action)()
            return {"ok": True, "bot_id": bot.id, "result": result}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
