from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import QQBotConfig, QQConfig, resolve_secret
from ..models import Announcement, JoinRequest


class OneBotError(RuntimeError):
    pass


def onebot_plain_text(message: object) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        kind = message.get("type")
        data = message.get("data")
        if isinstance(data, dict):
            if kind == "text":
                return str(data.get("text", ""))
            if kind == "at":
                return f"@{data.get('qq', '')}"
            if kind in {"image", "record", "video", "file"}:
                return f"[{kind}]"
        # NapCat's group-notice action returns a structured message such as
        # {"text": "...", "image": [...]}, rather than OneBot segments.
        # Persist its actual text, never the unstable Python representation of
        # the whole payload (which also includes incidental media metadata).
        for field in ("text", "content", "message", "segments"):
            value = message.get(field)
            if value is not None:
                return onebot_plain_text(value)
        return ""
    if not isinstance(message, list):
        return str(message or "")
    parts: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        data = segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "at":
            parts.append(f"@{data.get('qq', '')}")
        elif kind in {"image", "record", "video", "file"}:
            parts.append(f"[{kind}]")
    return "".join(parts).strip()


def _timestamp(value: object) -> datetime | None:
    if value in (None, "", 0):
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=UTC)
    except (TypeError, ValueError, OSError):
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None


class OneBotClient:
    def __init__(self, config: QQConfig | QQBotConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        headers: dict[str, str] = {}
        access_token = resolve_secret(config.access_token, config.access_token_file)
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self._client = httpx.AsyncClient(
            base_url=config.onebot_base_url.rstrip("/") + "/",
            headers=headers,
            timeout=config.request_timeout_seconds,
        )

    async def _call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._client.post(action, json=params or {})
        response.raise_for_status()
        payload = response.json()
        retcode = payload.get("retcode")
        if payload.get("status") not in (None, "ok") or retcode not in (None, 0):
            raise OneBotError(
                f"OneBot action {action} failed: {json.dumps(payload, ensure_ascii=False)}"
            )
        return payload.get("data")

    async def approve_join(self, request: JoinRequest, approve: bool, reason: str = "") -> None:
        if self.dry_run:
            return
        await self._call(
            "set_group_add_request",
            {
                "flag": request.flag,
                "sub_type": request.sub_type,
                "approve": approve,
                "reason": reason,
            },
        )

    async def send_private_message(self, user_id: str, message: str) -> None:
        if self.dry_run:
            return
        await self._call("send_private_msg", {"user_id": int(user_id), "message": message})

    async def send_group_message(self, group_id: str, message: str) -> None:
        if self.dry_run:
            return
        await self._call("send_group_msg", {"group_id": int(group_id), "message": message})

    async def notify_administrators(self, message: str) -> None:
        # 私聊通知管理员，同时把同一消息发到配置的管理群。
        if not self.config.administrator_qq_ids and not self.config.management_group_ids:
            raise OneBotError("No administrator QQ IDs or management groups are configured")
        errors: list[str] = []
        delivered = 0
        for user_id in self.config.administrator_qq_ids:
            try:
                await self.send_private_message(user_id, message)
                delivered += 1
            except Exception as exc:  # continue notifying the remaining administrators
                errors.append(f"{user_id}: {exc}")
        for group_id in self.config.management_group_ids:
            try:
                await self.send_group_message(group_id, message)
                delivered += 1
            except Exception as exc:  # continue posting to the remaining management groups
                errors.append(f"group {group_id}: {exc}")
        if errors and delivered == 0:
            raise OneBotError("; ".join(errors))

    async def fetch_announcements(self, group_id: str) -> list[Announcement]:
        last_error: Exception | None = None
        data: object = None
        for action in self.config.announcement_actions:
            try:
                data = await self._call(action, {"group_id": int(group_id)})
                last_error = None
                break
            except Exception as exc:
                last_error = exc
        if last_error:
            raise OneBotError(f"No announcement action succeeded: {last_error}")
        if isinstance(data, dict):
            raw_items = data.get("notices") or data.get("notice_list") or data.get("data") or []
        else:
            raw_items = data or []
        if not isinstance(raw_items, list):
            raise OneBotError(f"Unexpected announcement response: {type(raw_items).__name__}")

        announcements: list[Announcement] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            message = item.get("message")
            content = onebot_plain_text(message if message is not None else item.get("content", ""))
            title = str(item.get("title") or item.get("notice_title") or "")
            identifier = item.get("notice_id") or item.get("fid") or item.get("id")
            if not identifier:
                identifier = hashlib.sha256(f"{title}\n{content}".encode()).hexdigest()[:24]
            announcements.append(
                Announcement(
                    bot_id=getattr(self.config, "id", "default"),
                    announcement_id=str(identifier),
                    group_id=group_id,
                    title=title,
                    content=content,
                    author_id=str(
                        item.get("sender_id")
                        or item.get("user_id")
                        or (item.get("sender") or {}).get("user_id", "")
                    ),
                    published_at=_timestamp(
                        item.get("publish_time") or item.get("time") or item.get("create_time")
                    ),
                    source_payload=item,
                )
            )
        return announcements

    async def doctor(self) -> dict[str, object]:
        data = await self._call("get_login_info")
        return {"ok": True, "login_info": data}

    async def close(self) -> None:
        await self._client.aclose()
