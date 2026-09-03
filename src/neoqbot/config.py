from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from ipaddress import ip_network
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def resolve_secret(value: str, file_path: str = "") -> str:
    """Prefer a mounted secret file while retaining direct-value compatibility."""
    if file_path:
        try:
            mounted = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            mounted = ""
        if mounted:
            return mounted
    return value.strip()


BUNDLED_NAPCAT_ONEBOT_URL = "http://qq-bridge:3000"
BUNDLED_NAPCAT_WEBUI_URL = "http://qq-bridge:6099"
BUNDLED_NAPCAT_ONEBOT_TOKEN_FILE = "data/secrets/napcat-onebot.token"
BUNDLED_NAPCAT_WEBUI_TOKEN_FILE = "data/secrets/napcat-webui.token"
BUNDLED_NAPCAT_QRCODE_PATH = "/app/napcat-cache/qrcode.png"


class AppConfig(BaseModel):
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    database_path: str = "data/neoqbot.db"
    message_archive_path: str = "data/group-message-records"
    admin_api_token: str = ""
    admin_api_token_file: str = "data/secrets/admin-api.token"
    dry_run: bool = True
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "neoqbot", "testserver"]
    )
    # 允许通过服务器 IPv4/IPv6 字面量访问，同时继续拒绝未列入白名单的域名。
    allow_ip_hosts: bool = True
    management_allowed_networks: list[str] = Field(default_factory=list)
    require_https: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    max_request_body_bytes: int = Field(default=2 * 1024 * 1024, ge=16 * 1024, le=16 * 1024 * 1024)
    expose_api_docs: bool = False

    @field_validator("admin_api_token")
    @classmethod
    def strong_admin_token(cls, value: str) -> str:
        value = value.strip()
        if value and len(value) < 32:
            raise ValueError("app.admin_api_token 至少需要 32 个字符")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def valid_allowed_hosts(cls, value: list[str]) -> list[str]:
        hosts = [host.strip() for host in value if host.strip()]
        if not hosts:
            raise ValueError("app.allowed_hosts 不能为空")
        if any("/" in host or "://" in host for host in hosts):
            raise ValueError("app.allowed_hosts 只填写主机名或 IP，不要填写 URL/路径")
        return list(dict.fromkeys(hosts))

    @field_validator("management_allowed_networks")
    @classmethod
    def valid_management_networks(cls, value: list[str]) -> list[str]:
        networks = [network.strip() for network in value if network.strip()]
        for network in networks:
            ip_network(network, strict=False)
        return list(dict.fromkeys(networks))


class QQConnectionConfig(BaseModel):
    enabled: bool = False
    onebot_base_url: str = "http://127.0.0.1:3000"
    access_token: str = ""
    access_token_file: str = "data/secrets/napcat-onebot.token"
    webhook_secret: str = ""
    request_timeout_seconds: float = 15.0
    webui_base_url: str = "http://127.0.0.1:6099"
    webui_token: str = ""
    webui_token_file: str = "data/secrets/napcat-webui.token"
    webui_public_url: str = ""
    webui_public_port: int = Field(default=6099, ge=1, le=65535)
    qrcode_path: str = "data/napcat-cache/qrcode.png"
    administrator_qq_ids: list[str] = Field(default_factory=list)
    # 管理群：notify_administrators 在私聊通知管理员的同时，也会在这些群里发消息。
    # 不要求启用任何编排连接；群号即为 Bot 可发消息的 QQ 群。
    management_group_ids: list[str] = Field(default_factory=list)
    announcement_actions: list[str] = Field(
        default_factory=lambda: ["get_group_notice", "_get_group_notice"]
    )

    @field_validator("administrator_qq_ids", "management_group_ids", mode="before")
    @classmethod
    def ids_as_strings(cls, value: object) -> object:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return value


class QQJoinTaskConfig(BaseModel):
    enabled: bool = False
    detect_requests: bool = False
    execute_management: bool = False
    auto_approve: bool = False
    auto_reject: bool = False
    minimum_confidence: float = Field(default=0.88, ge=0, le=1)

    @model_validator(mode="after")
    def apply_dependencies(self) -> QQJoinTaskConfig:
        if self.execute_management:
            self.enabled = True
            self.detect_requests = True
        if self.detect_requests:
            self.enabled = True
        return self


class QQMessageTaskConfig(BaseModel):
    enabled: bool = False
    record: bool = False
    scheduled_analysis: bool = False
    interval_minutes: int = Field(default=30, ge=1, le=1440)
    window_minutes: int = Field(default=5, ge=1, le=1440)
    risk_threshold: float = Field(default=0.7, ge=0, le=1)
    max_messages_per_run: int = Field(default=300, ge=1, le=5000)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_switches(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        if "record" not in migrated:
            migrated["record"] = bool(
                migrated.get("record_only") or migrated.get("realtime_detection")
            )
        if "scheduled_analysis" not in migrated:
            migrated["scheduled_analysis"] = bool(
                migrated.get("analyze")
                or migrated.get("polling_detection")
                or migrated.get("handle")
            )
        return migrated

    @model_validator(mode="after")
    def apply_dependencies(self) -> QQMessageTaskConfig:
        self.enabled = self.record or self.scheduled_analysis
        return self


class QQAnnouncementTaskConfig(BaseModel):
    enabled: bool = False
    sync_interval_minutes: int = Field(default=30, ge=1, le=10080)
    feishu_bot_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_switches(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        migrated["enabled"] = bool(
            migrated.get("enabled") or migrated.get("auto_sync") or migrated.get("sync_on_startup")
        )
        return migrated


class QQTaskConfig(BaseModel):
    join_management: QQJoinTaskConfig = Field(default_factory=QQJoinTaskConfig)
    message_detection: QQMessageTaskConfig = Field(default_factory=QQMessageTaskConfig)
    announcement_sync: QQAnnouncementTaskConfig = Field(default_factory=QQAnnouncementTaskConfig)


class QQGroupJoinPolicyConfig(BaseModel):
    """Optional join-review policy that overrides only one QQ group.

    ``None`` means inherit the platform setting.  An explicitly empty keyword
    list is meaningful: it clears the platform keyword rule for this group.
    """

    policy: str | None = Field(default=None, max_length=4000)
    required_keywords: list[str] | None = None
    forbidden_keywords: list[str] | None = None

    @field_validator("policy", mode="before")
    @classmethod
    def strip_policy(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @field_validator("required_keywords", "forbidden_keywords", mode="before")
    @classmethod
    def clean_keywords(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError("群入群审核关键词必须是列表")
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


class QQBotConfig(QQConnectionConfig):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(default="QQ Bot", min_length=1, max_length=80)
    connection_mode: Literal["bundled_napcat", "external"] = "external"
    search_feishu_bot_id: str = ""

    @model_validator(mode="after")
    def apply_connection_mode(self) -> QQBotConfig:
        if self.connection_mode == "bundled_napcat":
            self.onebot_base_url = BUNDLED_NAPCAT_ONEBOT_URL
            self.access_token = ""
            self.access_token_file = BUNDLED_NAPCAT_ONEBOT_TOKEN_FILE
            self.webhook_secret = ""
            self.webui_base_url = BUNDLED_NAPCAT_WEBUI_URL
            self.webui_token = ""
            self.webui_token_file = BUNDLED_NAPCAT_WEBUI_TOKEN_FILE
            self.qrcode_path = BUNDLED_NAPCAT_QRCODE_PATH
        return self


class QQConfig(BaseModel):
    bots: list[QQBotConfig] = Field(
        default_factory=lambda: [
            QQBotConfig(
                id="default",
                name="默认 QQ Bot",
                connection_mode="bundled_napcat",
            )
        ],
    )

    @model_validator(mode="after")
    def unique_bot_ids(self) -> QQConfig:
        identifiers = [bot.id for bot in self.bots]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("qq.bots 中的 id 必须唯一")
        bundled = [bot.id for bot in self.bots if bot.connection_mode == "bundled_napcat"]
        if len(bundled) > 1:
            raise ValueError(
                "Compose 内置 NapCat 只能绑定一个 QQ Bot；其他 Bot 请使用 external 连接模式"
            )
        return self


class LLMConfig(BaseModel):
    driver: Literal["openai_compatible", "rules"] = "rules"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "replace-with-agnes-model"
    timeout_seconds: float = 60.0
    temperature: float = 0.0
    max_retries: int = 2
    json_response_format: bool = True


class JoinApprovalConfig(BaseModel):
    enabled: bool = False
    auto_approve: bool = False
    auto_reject: bool = False
    minimum_confidence: float = Field(default=0.88, ge=0, le=1)
    policy: str = "申请者应说明加入目的，并同意群规；信息不充分时转人工审核。"
    required_keywords: list[str] = Field(default_factory=list)
    forbidden_keywords: list[str] = Field(default_factory=list)


class ModerationConfig(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(default=30, ge=1, le=1440)
    window_minutes: int = Field(default=5, ge=1, le=1440)
    risk_threshold: float = Field(default=0.7, ge=0, le=1)
    max_messages_per_run: int = Field(default=300, ge=1, le=5000)
    policy: str = (
        "识别违法违规、涉政、色情、仇恨或群体对立、人身攻击、骚扰、诈骗、"
        "泄露隐私以及其他违反群规的内容。正常事实讨论不应仅因关键词命中而判违规。"
    )
    rule_keywords: dict[str, list[str]] = Field(default_factory=dict)


class AnnouncementConfig(BaseModel):
    enabled: bool = False
    sync_interval_minutes: int = Field(default=30, ge=1, le=10080)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_switch(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        migrated["enabled"] = bool(migrated.get("enabled") or migrated.get("sync_on_startup"))
        return migrated


class FeishuConnectionConfig(BaseModel):
    enabled: bool = False
    driver: Literal["cli", "disabled"] = "disabled"
    executable: str = "feishu"
    timeout_seconds: float = 60.0
    search_prefixes: list[str] = Field(default_factory=lambda: ["搜索 ", "查询 ", "/search "])
    max_search_results: int = Field(default=5, ge=1, le=20)
    # Each item is one argv token. No shell is involved. Supported actions:
    # archive_announcement, search, doctor.
    command_templates: dict[str, list[str]] = Field(default_factory=dict)
    archive_payload_stdin: bool = False
    extra_environment: dict[str, str] = Field(default_factory=dict)


class FeishuBotConfig(FeishuConnectionConfig):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(default="飞书 Bot", min_length=1, max_length=80)


class FeishuConfig(BaseModel):
    bots: list[FeishuBotConfig] = Field(
        default_factory=lambda: [FeishuBotConfig(id="default", name="默认飞书 Bot", enabled=False)],
    )

    @model_validator(mode="after")
    def unique_bot_ids(self) -> FeishuConfig:
        identifiers = [bot.id for bot in self.bots]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("feishu.bots 中的 id 必须唯一")
        return self


class OrchestrationPosition(BaseModel):
    x: float = Field(default=80, ge=-10000, le=10000)
    y: float = Field(default=80, ge=-10000, le=10000)


class OrchestrationResourceConfig(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    kind: Literal["qq_group", "feishu_group", "knowledge_base"]
    name: str = Field(min_length=1, max_length=80)
    external_id: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=1000)
    enabled: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)
    join_approval: QQGroupJoinPolicyConfig | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("name", "external_id", "description", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class OrchestrationEdgeConfig(BaseModel):
    id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    source: str = Field(min_length=1, max_length=96)
    target: str = Field(min_length=1, max_length=96)
    relation: Literal["manages", "observes", "archives_to", "searches", "syncs"] = "manages"
    enabled: bool = True
    tasks: QQTaskConfig | None = Field(default=None, exclude_if=lambda value: value is None)


class QQGroupAssignment(BaseModel):
    edge_id: str
    bot_id: str
    resource_id: str
    group_id: str
    group_name: str
    relation: Literal["manages", "observes"]
    tasks: QQTaskConfig


class OrchestrationConfig(BaseModel):
    resources: list[OrchestrationResourceConfig] = Field(default_factory=list)
    edges: list[OrchestrationEdgeConfig] = Field(default_factory=list)
    layout: dict[str, OrchestrationPosition] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_identifiers(self) -> OrchestrationConfig:
        resource_ids = [resource.id for resource in self.resources]
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("orchestration.resources 中的 id 必须唯一")
        external_resources = [
            (resource.kind, resource.external_id)
            for resource in self.resources
            if resource.external_id
        ]
        if len(external_resources) != len(set(external_resources)):
            raise ValueError("同一类型的编排资源不能使用重复的平台标识")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("orchestration.edges 中的 id 必须唯一")
        connections = [
            (edge.source, edge.target, edge.relation) for edge in self.edges if edge.enabled
        ]
        if len(connections) != len(set(connections)):
            raise ValueError("编排连接不能重复")
        return self


class RuntimeConfig(BaseModel):
    event_workers: int = Field(default=2, ge=1, le=32)
    shutdown_grace_seconds: float = Field(default=15.0, ge=0.1, le=300)


class RetentionConfig(BaseModel):
    enabled: bool = True
    message_days: int = Field(default=30, ge=1, le=3650)
    join_request_days: int = Field(default=180, ge=1, le=3650)
    moderation_run_days: int = Field(default=365, ge=1, le=3650)
    audit_days: int = Field(default=365, ge=1, le=3650)


class GuiConfig(BaseModel):
    enabled: bool = True
    bootstrap_username: str = Field(default="admin", min_length=1, max_length=64)
    bootstrap_password: str = Field(default="", max_length=512)
    bootstrap_password_file: str = "data/secrets/gui-bootstrap-password"
    session_hours: int = Field(default=8, ge=1, le=168)
    max_sessions_per_user: int = Field(default=5, ge=1, le=20)
    secure_cookie: bool = False
    allow_sensitive_settings_edits: bool = False


def _migration_identifier(value: str, fallback: str, limit: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or fallback
    return cleaned[:limit]


def _legacy_tasks_enabled(value: object) -> bool:
    if isinstance(value, dict):
        return any(_legacy_tasks_enabled(item) for item in value.values())
    if isinstance(value, list):
        return any(_legacy_tasks_enabled(item) for item in value)
    return value is True


def _looks_like_legacy_bundled_bot(raw_bot: dict[str, object], bot_id: str) -> bool:
    """Recognize the old/default single Compose sidecar without capturing custom adapters."""
    onebot_urls = {
        "",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        BUNDLED_NAPCAT_ONEBOT_URL,
    }
    webui_urls = {
        "",
        "http://127.0.0.1:6099",
        "http://localhost:6099",
        BUNDLED_NAPCAT_WEBUI_URL,
    }
    generated_roots = {f"data/secrets/qq/{bot_id}", f"/app/data/secrets/qq/{bot_id}"}
    onebot_files = {"", BUNDLED_NAPCAT_ONEBOT_TOKEN_FILE} | {
        f"{root}/onebot.token" for root in generated_roots
    }
    webui_files = {"", BUNDLED_NAPCAT_WEBUI_TOKEN_FILE} | {
        f"{root}/webui.token" for root in generated_roots
    }
    return (
        str(raw_bot.get("onebot_base_url") or "").rstrip("/") in onebot_urls
        and str(raw_bot.get("webui_base_url") or "").rstrip("/") in webui_urls
        and str(raw_bot.get("access_token_file") or "").replace("\\", "/") in onebot_files
        and str(raw_bot.get("webui_token_file") or "").replace("\\", "/") in webui_files
        and not str(raw_bot.get("access_token") or "").strip()
        and not str(raw_bot.get("webui_token") or "").strip()
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEOQBOT_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    app: AppConfig = Field(default_factory=AppConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    join_approval: JoinApprovalConfig = Field(default_factory=JoinApprovalConfig)
    moderation: ModerationConfig = Field(default_factory=ModerationConfig)
    announcements: AnnouncementConfig = Field(default_factory=AnnouncementConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    gui: GuiConfig = Field(default_factory=GuiConfig)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_qq_tasks(cls, values: object) -> object:
        """Move legacy bot-wide groups and tasks onto bot-to-group orchestration edges."""
        if not isinstance(values, dict):
            return values
        data = copy.deepcopy(values)
        qq = data.get("qq")
        if not isinstance(qq, dict) or not isinstance(qq.get("bots"), list):
            return data
        for index, raw_bot in enumerate(qq["bots"]):
            if not isinstance(raw_bot, dict):
                continue
            bot_id = str(raw_bot.get("id") or "default")
            # A previous migration could persist the Compose sidecar as an external
            # bot when a legacy webhook_secret was still present.  The exact
            # qq-bridge endpoints and shared secret files identify it unambiguously;
            # Restore bundled mode so the sidecar consistently uses the shared
            # OneBot token as both its API credential and HTTP-event HMAC key.
            if (
                index == 0
                and raw_bot.get("connection_mode") == "external"
                and str(raw_bot.get("onebot_base_url") or "").rstrip("/")
                == BUNDLED_NAPCAT_ONEBOT_URL
                and str(raw_bot.get("webui_base_url") or "").rstrip("/") == BUNDLED_NAPCAT_WEBUI_URL
                and _looks_like_legacy_bundled_bot(raw_bot, bot_id)
            ):
                raw_bot["connection_mode"] = "bundled_napcat"
            if "connection_mode" in raw_bot:
                continue
            connection_mode = (
                "bundled_napcat"
                if index == 0 and _looks_like_legacy_bundled_bot(raw_bot, bot_id)
                else "external"
            )
            raw_bot["connection_mode"] = connection_mode
            if connection_mode == "external" and str(raw_bot.get("qrcode_path") or "").replace(
                "\\", "/"
            ) in {"", "data/napcat-cache/qrcode.png"}:
                raw_bot["qrcode_path"] = f"data/napcat-cache/{bot_id}/qrcode.png"
        orchestration = data.setdefault("orchestration", {})
        if not isinstance(orchestration, dict):
            return data
        resources = orchestration.setdefault("resources", [])
        edges = orchestration.setdefault("edges", [])
        orchestration.setdefault("layout", {})
        if not isinstance(resources, list) or not isinstance(edges, list):
            return data

        used_resource_ids = {
            str(resource.get("id"))
            for resource in resources
            if isinstance(resource, dict) and resource.get("id")
        }
        used_edge_ids = {
            str(edge.get("id")) for edge in edges if isinstance(edge, dict) and edge.get("id")
        }
        qq_resources = {
            str(resource.get("external_id")): str(resource.get("id"))
            for resource in resources
            if isinstance(resource, dict)
            and resource.get("kind") == "qq_group"
            and resource.get("external_id")
            and resource.get("id")
        }

        def unique_identifier(base: str, used: set[str], limit: int) -> str:
            candidate = base[:limit]
            suffix = 2
            while candidate in used:
                marker = f"-{suffix}"
                candidate = f"{base[: limit - len(marker)]}{marker}"
                suffix += 1
            used.add(candidate)
            return candidate

        for raw_bot in qq["bots"]:
            if not isinstance(raw_bot, dict) or not raw_bot.get("id"):
                continue
            bot_id = str(raw_bot["id"])
            source = f"qq-bot:{bot_id}"
            legacy_tasks = raw_bot.pop("tasks", None)
            legacy_groups = raw_bot.pop("managed_group_ids", [])
            assignment_edges = [
                edge
                for edge in edges
                if isinstance(edge, dict)
                and edge.get("source") == source
                and edge.get("target") in set(qq_resources.values())
                and edge.get("relation", "manages") in {"manages", "observes"}
            ]
            if not assignment_edges and isinstance(legacy_groups, list):
                for raw_group_id in legacy_groups:
                    group_id = str(raw_group_id).strip()
                    if not group_id:
                        continue
                    resource_id = qq_resources.get(group_id)
                    if resource_id is None:
                        base = _migration_identifier(
                            f"qq-group-{group_id}", "qq-group-migrated", 64
                        )
                        resource_id = unique_identifier(base, used_resource_ids, 64)
                        resources.append(
                            {
                                "id": resource_id,
                                "kind": "qq_group",
                                "name": f"QQ群 {group_id}",
                                "external_id": group_id,
                                "description": "由旧版 managed_group_ids 自动迁移",
                                "enabled": True,
                                "metadata": {},
                            }
                        )
                        qq_resources[group_id] = resource_id
                    edge_base = _migration_identifier(
                        f"{bot_id}-manages-{resource_id}", "migrated-assignment", 96
                    )
                    edge = {
                        "id": unique_identifier(edge_base, used_edge_ids, 96),
                        "source": source,
                        "target": resource_id,
                        "relation": "manages",
                        "enabled": True,
                    }
                    edges.append(edge)
                    assignment_edges.append(edge)
            if isinstance(legacy_tasks, dict):
                for edge in assignment_edges:
                    if edge.get("tasks") is None:
                        edge["tasks"] = copy.deepcopy(legacy_tasks)
                if _legacy_tasks_enabled(legacy_tasks) and not assignment_edges:
                    raise ValueError(f"qq.bots.{bot_id}.tasks 已启用，但没有可迁移的 QQ 群编排连接")
        return data

    @model_validator(mode="after")
    def validate_orchestration(self) -> Settings:
        resources = {resource.id: resource for resource in self.orchestration.resources}
        resource_ids = set(resources)
        qq_bot_node_ids = {f"qq-bot:{bot.id}" for bot in self.effective_qq_bots()}
        bot_node_ids = qq_bot_node_ids | {
            f"feishu-bot:{bot.id}" for bot in self.effective_feishu_bots()
        }
        valid_node_ids = resource_ids | bot_node_ids
        active_assignments: set[tuple[str, str]] = set()
        for edge in self.orchestration.edges:
            if edge.source == edge.target:
                raise ValueError(f"编排连接 {edge.id} 不能连接节点自身")
            if edge.source not in valid_node_ids or edge.target not in valid_node_ids:
                raise ValueError(f"编排连接 {edge.id} 指向未知节点")
            target = resources.get(edge.target)
            is_assignment = (
                edge.source in qq_bot_node_ids
                and target is not None
                and target.kind == "qq_group"
                and edge.relation in {"manages", "observes"}
            )
            if is_assignment:
                edge.tasks = edge.tasks or QQTaskConfig()
                assignment_key = (edge.source, edge.target)
                if edge.enabled and assignment_key in active_assignments:
                    raise ValueError(f"QQ Bot 与 QQ 群之间只能有一条启用的事务分工连接：{edge.id}")
                if edge.enabled:
                    active_assignments.add(assignment_key)
            elif edge.tasks is not None:
                raise ValueError(f"编排连接 {edge.id} 不是 QQ Bot 到 QQ 群，不能配置 QQ 事务")
        return self

    def effective_qq_bots(self) -> list[QQBotConfig]:
        return self.qq.bots

    def effective_feishu_bots(self) -> list[FeishuBotConfig]:
        return self.feishu.bots

    def qq_bot(self, bot_id: str | None = None) -> QQBotConfig | None:
        bots = self.effective_qq_bots()
        if bot_id is None:
            return bots[0] if bots else None
        return next((bot for bot in bots if bot.id == bot_id), None)

    def bundled_qq_bot(self) -> QQBotConfig | None:
        return next(
            (bot for bot in self.effective_qq_bots() if bot.connection_mode == "bundled_napcat"),
            None,
        )

    def feishu_bot(self, bot_id: str | None = None) -> FeishuBotConfig | None:
        bots = self.effective_feishu_bots()
        if bot_id is None:
            return bots[0] if bots else None
        return next((bot for bot in bots if bot.id == bot_id), None)

    def qq_group_assignments(
        self, bot_id: str | None = None, *, include_disabled: bool = False
    ) -> list[QQGroupAssignment]:
        bots = {bot.id: bot for bot in self.effective_qq_bots()}
        resources = {resource.id: resource for resource in self.orchestration.resources}
        assignments: list[QQGroupAssignment] = []
        for edge in self.orchestration.edges:
            if not edge.source.startswith("qq-bot:") or edge.relation not in {
                "manages",
                "observes",
            }:
                continue
            edge_bot_id = edge.source.removeprefix("qq-bot:")
            bot = bots.get(edge_bot_id)
            resource = resources.get(edge.target)
            if bot is None or resource is None or resource.kind != "qq_group":
                continue
            if bot_id is not None and edge_bot_id != bot_id:
                continue
            if not include_disabled and (
                not bot.enabled
                or not edge.enabled
                or not resource.enabled
                or not resource.external_id
            ):
                continue
            assignments.append(
                QQGroupAssignment(
                    edge_id=edge.id,
                    bot_id=edge_bot_id,
                    resource_id=resource.id,
                    group_id=resource.external_id,
                    group_name=resource.name,
                    relation=edge.relation,
                    tasks=edge.tasks or QQTaskConfig(),
                )
            )
        return sorted(assignments, key=lambda item: (item.bot_id, item.group_id, item.edge_id))

    def qq_group_assignment(self, bot_id: str, group_id: str) -> QQGroupAssignment | None:
        return next(
            (
                assignment
                for assignment in self.qq_group_assignments(bot_id)
                if assignment.group_id == group_id
            ),
            None,
        )

    def join_approval_for_group(self, resource_id: str) -> JoinApprovalConfig:
        """Resolve the policy for one QQ-group resource without mutating defaults."""
        resource = next(
            (item for item in self.orchestration.resources if item.id == resource_id), None
        )
        override = resource.join_approval if resource is not None else None
        if override is None:
            return self.join_approval
        changes: dict[str, object] = {}
        if override.policy is not None:
            changes["policy"] = override.policy
        if override.required_keywords is not None:
            changes["required_keywords"] = override.required_keywords
        if override.forbidden_keywords is not None:
            changes["forbidden_keywords"] = override.forbidden_keywords
        return self.join_approval.model_copy(update=changes, deep=True)

    def bot_group_ids(self, bot_id: str) -> list[str]:
        return sorted({assignment.group_id for assignment in self.qq_group_assignments(bot_id)})

    def managed_group_ids(self) -> list[str]:
        return sorted({assignment.group_id for assignment in self.qq_group_assignments()})

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        config_path = Path(path or "config.yaml")
        values: dict[str, object] = {}
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"Configuration root must be a mapping: {config_path}")
            values = loaded
        # Explicitly overlay nested environment variables on YAML. BaseSettings normally gives
        # constructor values priority, while deployments reasonably expect secrets in env to win.
        prefix = "NEOQBOT_"
        for name, raw_value in os.environ.items():
            if not name.upper().startswith(prefix):
                continue
            path_parts = name[len(prefix) :].lower().split("__")
            if not path_parts or path_parts == ["config"]:
                continue
            cursor = values
            for part in path_parts[:-1]:
                child = cursor.get(part)
                if not isinstance(child, dict):
                    child = {}
                    cursor[part] = child
                cursor = child
            if raw_value.lstrip().startswith(("[", "{")):
                try:
                    parsed_value = json.loads(raw_value)
                except json.JSONDecodeError:
                    parsed_value = raw_value
            else:
                parsed_value = raw_value
            cursor[path_parts[-1]] = parsed_value
        return cls(**values)

    def redacted_dict(self) -> dict[str, object]:
        data = self.model_dump()
        for bot in data["qq"]["bots"]:
            bot["access_token"] = "***" if bot["access_token"] else ""
            bot["webui_token"] = "***" if bot["webui_token"] else ""
            bot["webhook_secret"] = "***" if bot["webhook_secret"] else ""
        data["llm"]["api_key"] = "***" if self.llm.api_key else ""
        data["app"]["admin_api_token"] = "***" if self.app.admin_api_token else ""
        for bot in data["feishu"]["bots"]:
            bot["extra_environment"] = {key: "***" for key in bot["extra_environment"]}
        data["gui"]["bootstrap_password"] = "***"
        return json.loads(json.dumps(data, ensure_ascii=False, default=str))

    def deployment_security_errors(self) -> list[str]:
        """Return configuration errors that make serving the control plane unsafe."""
        errors: list[str] = []
        production = self.app.environment.lower() == "production"
        if production and not self.app.require_https:
            errors.append("生产环境必须设置 app.require_https=true 并使用可信 HTTPS 反向代理")
        if self.gui.enabled and self.app.require_https and not self.gui.secure_cookie:
            errors.append("app.require_https=true 时必须同时启用 gui.secure_cookie")
        if production and "*" in self.app.allowed_hosts:
            errors.append("生产环境禁止在 app.allowed_hosts 中使用通配符")
        if production and self.app.forwarded_allow_ips.strip() == "*":
            errors.append("生产环境禁止设置 app.forwarded_allow_ips='*'")
        if production and self.app.expose_api_docs:
            errors.append("生产环境禁止公开 OpenAPI/Swagger 文档")
        return errors

    def diagnostics(self) -> dict[str, list[str]]:
        errors = self.deployment_security_errors()
        warnings: list[str] = []
        qq_bots = [bot for bot in self.effective_qq_bots() if bot.enabled]
        if not qq_bots:
            warnings.append("没有启用任何 QQ Bot")
        assignments = self.qq_group_assignments()
        assignments_by_bot = {
            bot.id: [assignment for assignment in assignments if assignment.bot_id == bot.id]
            for bot in qq_bots
        }
        for bot in qq_bots:
            prefix = f"qq.bots.{bot.id}"
            if not assignments_by_bot[bot.id]:
                errors.append(f"{prefix} 必须在资源编排中至少连接一个已启用且填写群号的 QQ 群")
            if not bot.administrator_qq_ids:
                errors.append(f"{prefix}.administrator_qq_ids 不能为空")
            onebot_token = resolve_secret(bot.access_token, bot.access_token_file)
            if not onebot_token:
                errors.append(
                    f"{prefix}.access_token 未设置，OneBot 出站 API 调用将无法使用 Token 鉴权"
                )
            if not bot.webhook_secret and not onebot_token:
                errors.append(f"{prefix} 未设置 Webhook HMAC 或 Bearer Token，事件入口已安全禁用")
        if self.llm.driver == "openai_compatible":
            if not self.llm.api_key:
                errors.append("llm.api_key 未设置")
            if not self.llm.model or self.llm.model.startswith("replace-with"):
                errors.append("llm.model 仍是占位值")
        feishu_bots = self.effective_feishu_bots()
        default_feishu = next(
            (bot for bot in feishu_bots if bot.enabled),
            feishu_bots[0] if feishu_bots else None,
        )
        archive_targets = {
            assignment.tasks.announcement_sync.feishu_bot_id
            or (default_feishu.id if default_feishu else "")
            for assignment in assignments
            if assignment.tasks.announcement_sync.enabled
        }
        archive_targets.discard("")
        search_targets = {
            bot.search_feishu_bot_id or (default_feishu.id if default_feishu else "")
            for bot in qq_bots
        }
        search_targets.discard("")
        for bot in feishu_bots:
            if not bot.enabled:
                continue
            prefix = f"feishu.bots.{bot.id}"
            if bot.id in archive_targets and not bot.command_templates.get("archive_announcement"):
                errors.append(f"{prefix}.command_templates.archive_announcement 未配置")
            if bot.id in search_targets and not bot.command_templates.get("search"):
                errors.append(f"{prefix}.command_templates.search 未配置")
        feishu_ids = {bot.id for bot in feishu_bots}
        for assignment in assignments:
            announcement_target = assignment.tasks.announcement_sync.feishu_bot_id
            if announcement_target and announcement_target not in feishu_ids:
                errors.append(
                    f"orchestration.edges.{assignment.edge_id}.tasks.announcement_sync."
                    "feishu_bot_id 指向未知飞书 Bot"
                )
            if (
                assignment.relation == "observes"
                and assignment.tasks.join_management.execute_management
            ):
                warnings.append(
                    f"编排连接 {assignment.edge_id} 是 observes，但启用了入群管理执行；"
                    "建议改为 manages 以准确表达权限边界"
                )
        for bot in qq_bots:
            if bot.search_feishu_bot_id and bot.search_feishu_bot_id not in feishu_ids:
                errors.append(f"qq.bots.{bot.id}.search_feishu_bot_id 指向未知飞书 Bot")
        admin_api_token = resolve_secret(self.app.admin_api_token, self.app.admin_api_token_file)
        if not admin_api_token:
            warnings.append("app.admin_api_token 未设置，管理 API 将禁用")
        elif len(admin_api_token) < 32:
            errors.append("管理 API Token 至少需要 32 个字符")
        if self.app.dry_run:
            warnings.append("app.dry_run=true，所有 QQ 出站动作均被抑制")
        if self.join_approval.auto_reject or any(
            assignment.tasks.join_management.auto_reject for assignment in assignments
        ):
            warnings.append("join_approval.auto_reject=true，建议保持人工拒绝")
        bootstrap_password = resolve_secret(
            self.gui.bootstrap_password, self.gui.bootstrap_password_file
        )
        if self.gui.enabled and len(bootstrap_password) < 16:
            errors.append("GUI 初始密码缺失或少于 16 个字符，服务将拒绝创建管理员")
        if not self.gui.secure_cookie:
            warnings.append("gui.secure_cookie=false，仅适合 HTTP 联调或由可信反向代理提供 HTTPS")
        if "*" in self.app.allowed_hosts:
            warnings.append("app.allowed_hosts 包含通配符，Host Header 防护已弱化")
        if self.app.forwarded_allow_ips.strip() == "*":
            warnings.append("app.forwarded_allow_ips='*' 会信任任意代理头，不建议使用")
        if self.app.expose_api_docs:
            warnings.append("OpenAPI 文档已公开，仅应在受限开发环境使用")
        connected_resources = {
            endpoint
            for edge in self.orchestration.edges
            if edge.enabled
            for endpoint in (edge.source, edge.target)
        }
        for resource in self.orchestration.resources:
            if resource.enabled and resource.id not in connected_resources:
                warnings.append(f"编排资源 {resource.name} 尚未连接到任何 Bot 或资源")
        return {"errors": errors, "warnings": warnings}

    def save(self, path: str | Path) -> None:
        config_path = Path(path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json")
        environment_secrets = {
            "NEOQBOT_APP__ADMIN_API_TOKEN": ("app", "admin_api_token"),
            "NEOQBOT_LLM__API_KEY": ("llm", "api_key"),
            "NEOQBOT_GUI__BOOTSTRAP_PASSWORD": ("gui", "bootstrap_password"),
        }
        for environment_name, (section, key) in environment_secrets.items():
            if environment_name in os.environ:
                data[section][key] = ""
        payload = yaml.safe_dump(
            data,
            allow_unicode=True,
            sort_keys=False,
        )
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=config_path.parent,
                prefix=f".{config_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, config_path)
        finally:
            if temporary_name and Path(temporary_name).exists():
                Path(temporary_name).unlink()
