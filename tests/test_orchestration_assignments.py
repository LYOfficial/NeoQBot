from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from neoqbot.app import create_app
from neoqbot.auth import GuiSession
from neoqbot.config import Settings
from neoqbot.database import Database
from neoqbot.events import EventHandler
from neoqbot.gui import (
    ORCHESTRATION_SETTINGS_SECTIONS,
    PLATFORM_SETTINGS_SECTIONS,
    BotIdentityChanges,
    _merge_settings_sections,
    _preserve_masked_secrets,
    _validate_bot_identity_changes,
)
from neoqbot.models import GroupMessage, ModerationResult
from neoqbot.napcat import initialize_napcat, napcat_event_client_diagnostics
from neoqbot.runtime import Runtime
from neoqbot.services import ModerationService


def assignment_settings() -> Settings:
    return Settings.model_validate(
        {
            "gui": {"enabled": False},
            "qq": {
                "bots": [
                    {
                        "id": "worker",
                        "name": "Worker",
                        "enabled": True,
                        "connection_mode": "external",
                        "administrator_qq_ids": ["10001"],
                    }
                ]
            },
            "orchestration": {
                "resources": [
                    {
                        "id": "group-record",
                        "kind": "qq_group",
                        "name": "Record group",
                        "external_id": "100",
                    },
                    {
                        "id": "group-analyze",
                        "kind": "qq_group",
                        "name": "Analyze group",
                        "external_id": "200",
                    },
                ],
                "edges": [
                    {
                        "id": "worker-record",
                        "source": "qq-bot:worker",
                        "target": "group-record",
                        "relation": "observes",
                        "tasks": {
                            "message_detection": {
                                "record": True,
                                "interval_minutes": 60,
                            }
                        },
                    },
                    {
                        "id": "worker-analyze",
                        "source": "qq-bot:worker",
                        "target": "group-analyze",
                        "relation": "manages",
                        "tasks": {
                            "join_management": {
                                "detect_requests": True,
                                "minimum_confidence": 0.93,
                            },
                            "message_detection": {
                                "scheduled_analysis": True,
                                "interval_minutes": 15,
                                "window_minutes": 10,
                                "risk_threshold": 0.82,
                            },
                            "announcement_sync": {
                                "enabled": True,
                                "feishu_bot_id": "default",
                            },
                        },
                    },
                ],
            },
        }
    )


class SettingsAssignmentTests(unittest.TestCase):
    def test_qq_group_policy_overrides_only_its_configured_fields(self) -> None:
        config = assignment_settings().model_dump(mode="json")
        config["join_approval"] = {
            "policy": "平台默认策略",
            "required_keywords": ["默认信息"],
            "forbidden_keywords": ["平台禁止项"],
        }
        config["orchestration"]["resources"][1]["join_approval"] = {
            "policy": "审核群专属策略",
            "required_keywords": [],
        }
        settings = Settings.model_validate(config)

        inherited = settings.join_approval_for_group("group-record")
        overridden = settings.join_approval_for_group("group-analyze")

        self.assertEqual(inherited.policy, "平台默认策略")
        self.assertEqual(inherited.required_keywords, ["默认信息"])
        self.assertEqual(overridden.policy, "审核群专属策略")
        self.assertEqual(overridden.required_keywords, [])
        self.assertEqual(overridden.forbidden_keywords, ["平台禁止项"])

    def test_legacy_first_bot_adopts_the_bundled_napcat_identity(self) -> None:
        settings = Settings.model_validate(
            {
                "qq": {
                    "bots": [
                        {
                            "id": "mualliance1",
                            "name": "Primary",
                            "enabled": True,
                            "access_token_file": "data/secrets/qq/mualliance1/onebot.token",
                            "webui_token_file": "data/secrets/qq/mualliance1/webui.token",
                        },
                        {
                            "id": "mualliance2",
                            "name": "Secondary",
                            "enabled": True,
                            "access_token_file": "data/secrets/qq/mualliance2/onebot.token",
                            "webui_token_file": "data/secrets/qq/mualliance2/webui.token",
                        },
                    ]
                }
            }
        )

        primary, secondary = settings.effective_qq_bots()

        self.assertEqual(primary.connection_mode, "bundled_napcat")
        self.assertEqual(primary.onebot_base_url, "http://qq-bridge:3000")
        self.assertEqual(primary.access_token_file, "data/secrets/napcat-onebot.token")
        self.assertEqual(primary.webui_token_file, "data/secrets/napcat-webui.token")
        self.assertEqual(primary.qrcode_path, "/app/napcat-cache/qrcode.png")
        self.assertEqual(secondary.connection_mode, "external")
        self.assertEqual(
            secondary.qrcode_path,
            "data/napcat-cache/mualliance2/qrcode.png",
        )

    def test_stale_external_sidecar_configuration_recovers_bundled_mode(self) -> None:
        settings = Settings.model_validate(
            {
                "qq": {
                    "bots": [
                        {
                            "id": "mualliance1",
                            "name": "Primary",
                            "enabled": True,
                            "connection_mode": "external",
                            "onebot_base_url": "http://qq-bridge:3000",
                            "access_token_file": "data/secrets/napcat-onebot.token",
                            "webhook_secret": "legacy-hmac-secret",
                            "webui_base_url": "http://qq-bridge:6099",
                            "webui_token_file": "data/secrets/napcat-webui.token",
                        }
                    ]
                }
            }
        )

        bot = settings.qq_bot("mualliance1")

        self.assertIsNotNone(bot)
        assert bot is not None
        self.assertEqual(bot.connection_mode, "bundled_napcat")
        self.assertEqual(bot.webhook_secret, "")
        self.assertEqual(bot.access_token_file, "data/secrets/napcat-onebot.token")

    def test_only_one_bot_can_use_the_bundled_napcat_sidecar(self) -> None:
        with self.assertRaisesRegex(ValidationError, "只能绑定一个 QQ Bot"):
            Settings.model_validate(
                {
                    "qq": {
                        "bots": [
                            {"id": "first", "connection_mode": "bundled_napcat"},
                            {"id": "second", "connection_mode": "bundled_napcat"},
                        ]
                    }
                }
            )

    def test_tasks_are_scoped_to_each_bot_group_edge(self) -> None:
        settings = assignment_settings()

        record = settings.qq_group_assignment("worker", "100")
        analyze = settings.qq_group_assignment("worker", "200")

        self.assertIsNotNone(record)
        self.assertIsNotNone(analyze)
        assert record is not None
        assert analyze is not None
        self.assertTrue(record.tasks.message_detection.record)
        self.assertFalse(record.tasks.message_detection.scheduled_analysis)
        self.assertEqual(record.tasks.message_detection.interval_minutes, 60)
        self.assertTrue(analyze.tasks.message_detection.scheduled_analysis)
        self.assertFalse(analyze.tasks.message_detection.record)
        self.assertEqual(analyze.tasks.message_detection.interval_minutes, 15)
        self.assertEqual(analyze.tasks.join_management.minimum_confidence, 0.93)
        self.assertNotIn("tasks", settings.model_dump()["qq"]["bots"][0])
        self.assertNotIn("managed_group_ids", settings.model_dump()["qq"]["bots"][0])

    def test_legacy_bot_tasks_and_groups_migrate_without_loss(self) -> None:
        settings = Settings.model_validate(
            {
                "gui": {"enabled": False},
                "qq": {
                    "bots": [
                        {
                            "id": "legacy",
                            "enabled": True,
                            "managed_group_ids": ["300", "400"],
                            "tasks": {
                                "message_detection": {
                                    "record_only": True,
                                    "interval_minutes": 45,
                                },
                                "announcement_sync": {"auto_sync": True},
                            },
                        }
                    ]
                },
            }
        )

        assignments = settings.qq_group_assignments("legacy")
        self.assertEqual([item.group_id for item in assignments], ["300", "400"])
        self.assertTrue(all(item.tasks.message_detection.record for item in assignments))
        self.assertTrue(all(item.tasks.announcement_sync.enabled for item in assignments))

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "config.yaml"
            settings.save(path)
            saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertNotIn("tasks", saved["qq"]["bots"][0])
        self.assertNotIn("managed_group_ids", saved["qq"]["bots"][0])
        self.assertEqual(len(saved["orchestration"]["edges"]), 2)
        self.assertTrue(
            all(
                edge["tasks"]["message_detection"]["record"]
                for edge in saved["orchestration"]["edges"]
            )
        )
        self.assertTrue(
            all(
                edge["tasks"]["announcement_sync"]["enabled"]
                for edge in saved["orchestration"]["edges"]
            )
        )
        self.assertTrue(
            all(
                "record_only" not in edge["tasks"]["message_detection"]
                and "auto_sync" not in edge["tasks"]["announcement_sync"]
                and "sync_on_startup" not in edge["tasks"]["announcement_sync"]
                for edge in saved["orchestration"]["edges"]
            )
        )

    def test_tasks_are_rejected_on_non_assignment_edges(self) -> None:
        with self.assertRaises(ValidationError):
            Settings.model_validate(
                {
                    "gui": {"enabled": False},
                    "orchestration": {
                        "edges": [
                            {
                                "id": "invalid-tasks",
                                "source": "qq-bot:default",
                                "target": "feishu-bot:default",
                                "relation": "searches",
                                "tasks": {"message_detection": {"record": True}},
                            }
                        ]
                    },
                }
            )

    def test_non_assignment_edges_do_not_serialize_empty_task_blocks(self) -> None:
        settings = Settings.model_validate(
            {
                "gui": {"enabled": False},
                "orchestration": {
                    "edges": [
                        {
                            "id": "search-edge",
                            "source": "qq-bot:default",
                            "target": "feishu-bot:default",
                            "relation": "searches",
                        }
                    ]
                },
            }
        )

        self.assertNotIn("tasks", settings.model_dump()["orchestration"]["edges"][0])

    def test_duplicate_active_assignments_are_rejected(self) -> None:
        config = assignment_settings().model_dump(mode="json")
        duplicate = config["orchestration"]["edges"][0].copy()
        duplicate.update({"id": "worker-record-duplicate", "relation": "manages"})
        config["orchestration"]["edges"].append(duplicate)
        with self.assertRaises(ValidationError):
            Settings.model_validate(config)

    def test_same_platform_identifier_can_back_distinct_resource_nodes(self) -> None:
        config = assignment_settings().model_dump(mode="json")
        config["qq"]["bots"].append(
            {
                "id": "observer",
                "name": "Observer",
                "enabled": True,
                "connection_mode": "external",
            }
        )
        config["orchestration"]["resources"].append(
            {
                "id": "group-record-copy",
                "kind": "qq_group",
                "name": "Record group",
                "external_id": "100",
            }
        )
        config["orchestration"]["edges"].append(
            {
                "id": "observer-record-copy",
                "source": "qq-bot:observer",
                "target": "group-record-copy",
                "relation": "observes",
            }
        )

        settings = Settings.model_validate(config)

        self.assertEqual(
            [item.resource_id for item in settings.qq_group_assignments("observer")],
            ["group-record-copy"],
        )

    def test_same_bot_cannot_have_two_enabled_edges_for_one_group_number(self) -> None:
        config = assignment_settings().model_dump(mode="json")
        config["orchestration"]["resources"].append(
            {
                "id": "group-record-copy",
                "kind": "qq_group",
                "name": "Record group copy",
                "external_id": "100",
            }
        )
        config["orchestration"]["edges"].append(
            {
                "id": "worker-record-copy",
                "source": "qq-bot:worker",
                "target": "group-record-copy",
                "relation": "manages",
            }
        )

        with self.assertRaisesRegex(ValidationError, "同一个 QQ Bot 与同一个群号"):
            Settings.model_validate(config)

    def test_all_bot_nodes_can_be_removed(self) -> None:
        settings = Settings.model_validate(
            {
                "gui": {"enabled": False},
                "qq": {"bots": []},
                "feishu": {"bots": []},
            }
        )

        self.assertEqual(settings.effective_qq_bots(), [])
        self.assertEqual(settings.effective_feishu_bots(), [])


class ConfigurationPathTests(unittest.TestCase):
    def test_gui_uses_page_specific_routes_and_keeps_the_legacy_gui_redirect(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            settings = Settings.model_validate(
                {
                    "app": {
                        "database_path": str(Path(directory) / "gui-routes.db"),
                        "message_archive_path": str(Path(directory) / "messages"),
                    },
                    "gui": {
                        "enabled": True,
                        "bootstrap_password": "temporary-test-password",
                    },
                    "qq": {"bots": []},
                    "feishu": {"bots": []},
                }
            )
            app = create_app(settings)
            with TestClient(app) as client:
                root = client.get("/", follow_redirects=False)
                legacy = client.get("/gui/", follow_redirects=False)
                pages = [
                    client.get(path)
                    for path in ("/dashboard", "/orchestration", "/settings", "/records", "/user")
                ]
                asset = client.get("/assets/app.js")

        self.assertEqual(root.headers["location"], "/dashboard")
        self.assertEqual(legacy.headers["location"], "/dashboard")
        self.assertTrue(all(response.status_code == 200 for response in pages))
        self.assertEqual(asset.status_code, 200)

    def test_gui_receives_the_cli_selected_configuration_path(self) -> None:
        settings = Settings.model_validate({"gui": {"enabled": True}})
        selected = Path("data/custom-config.yaml")
        with (
            patch("neoqbot.app.build_container", return_value=object()),
            patch("neoqbot.app.register_gui") as register,
        ):
            create_app(settings, config_path=selected)

        self.assertEqual(register.call_args.kwargs["config_path"], selected)

    def test_application_builds_without_any_bot_nodes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            settings = Settings.model_validate(
                {
                    "app": {
                        "database_path": str(Path(directory) / "empty.db"),
                        "message_archive_path": str(Path(directory) / "messages"),
                    },
                    "gui": {"enabled": False},
                    "qq": {"bots": []},
                    "feishu": {"bots": []},
                }
            )

            app = create_app(settings)

        self.assertEqual(app.state.container.qq_clients, {})
        self.assertEqual(app.state.container.feishu_clients, {})

    def test_diagnostics_supports_empty_bot_collections(self) -> None:
        settings = Settings.model_validate(
            {
                "qq": {"bots": []},
                "feishu": {"bots": []},
            }
        )

        diagnostics = settings.diagnostics()

        self.assertIn("没有启用任何 QQ Bot", diagnostics["warnings"])

    def test_integration_status_endpoints_support_empty_bot_collections(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            settings = Settings.model_validate(
                {
                    "app": {
                        "database_path": str(Path(directory) / "empty-gui.db"),
                        "message_archive_path": str(Path(directory) / "messages"),
                    },
                    "gui": {
                        "enabled": True,
                        "bootstrap_password": "temporary-test-password",
                    },
                    "qq": {"bots": []},
                    "feishu": {"bots": []},
                }
            )
            app = create_app(settings)
            app.state.container.auth.session = lambda _: GuiSession(
                username="admin",
                csrf_token="test-csrf",
                must_change_password=False,
                role="admin",
                token_hash="test-token-hash",
            )

            with TestClient(app) as client:
                client.cookies.set("neoqbot_session", "test-session")
                qq_response = client.get("/api/gui/integrations/qq")
                feishu_response = client.get("/api/gui/integrations/feishu")

        self.assertEqual(qq_response.status_code, 200)
        self.assertEqual(feishu_response.status_code, 200)
        self.assertEqual(qq_response.json()["bots"], [])
        self.assertEqual(feishu_response.json()["bots"], [])
        self.assertEqual(qq_response.json()["status"]["reason"], "no_bots")
        self.assertEqual(feishu_response.json()["status"]["reason"], "no_bots")


class GuiDomainIsolationTests(unittest.TestCase):
    def test_llm_connection_test_uses_unsaved_form_values_without_persisting_a_key(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = assignment_settings().model_dump(mode="json")
            config["app"].update(
                {
                    "database_path": str(Path(directory) / "llm-test.db"),
                    "message_archive_path": str(Path(directory) / "messages"),
                }
            )
            config["gui"].update({"enabled": True, "bootstrap_password": "temporary-test-password"})
            config["llm"].update(
                {
                    "driver": "openai_compatible",
                    "base_url": "https://saved.example/v1",
                    "api_key": "saved-key",
                    "model": "saved-model",
                }
            )
            settings = Settings.model_validate(config)
            app = create_app(settings)
            app.state.container.auth.session = lambda _: GuiSession(
                username="admin",
                csrf_token="test-csrf",
                must_change_password=False,
                role="admin",
                token_hash="test-token-hash",
            )

            with (
                patch(
                    "neoqbot.gui.OpenAICompatibleDecisionEngine.test_connection",
                    new=AsyncMock(),
                ) as test_connection,
                TestClient(app) as client,
            ):
                client.cookies.set("neoqbot_session", "test-session")
                response = client.post(
                    "/api/gui/settings/llm/test",
                    headers={"X-CSRF-Token": "test-csrf"},
                    json={
                        "driver": "openai_compatible",
                        "base_url": "https://typed.example/v1",
                        "api_key": "typed-key",
                        "model": "typed-model",
                        "timeout_seconds": 15,
                        "json_response_format": True,
                    },
                )
                visible_settings = client.get("/api/gui/settings").json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "typed-model")
        test_connection.assert_awaited_once()
        self.assertEqual(visible_settings["config"]["llm"]["api_key"], "***")
        self.assertEqual(app.state.container.settings.llm.api_key, "saved-key")

    def test_llm_url_and_key_can_be_updated_while_other_sensitive_settings_are_locked(self) -> None:
        config = assignment_settings().model_dump(mode="json")
        config["llm"].update(
            {
                "driver": "openai_compatible",
                "base_url": "https://old.example/v1",
                "api_key": "old-key",
                "model": "old-model",
            }
        )
        current = Settings.model_validate(config)
        submitted = current.redacted_dict()
        submitted["llm"].update(
            {
                "base_url": "https://new.example/v1",
                "api_key": "new-key",
                "model": "new-model",
            }
        )

        merged = _preserve_masked_secrets(submitted, current)

        self.assertEqual(merged["llm"]["base_url"], "https://new.example/v1")
        self.assertEqual(merged["llm"]["api_key"], "new-key")

    def test_platform_settings_cannot_modify_bots_or_orchestration(self) -> None:
        current = assignment_settings()
        submitted = current.model_dump(mode="json")
        submitted["app"]["dry_run"] = False
        submitted["qq"]["bots"] = []
        submitted["orchestration"]["edges"] = []

        merged = _merge_settings_sections(submitted, current, PLATFORM_SETTINGS_SECTIONS)

        self.assertFalse(merged["app"]["dry_run"])
        self.assertEqual(merged["qq"], current.model_dump(mode="json")["qq"])
        self.assertEqual(merged["orchestration"], current.model_dump(mode="json")["orchestration"])

    def test_orchestration_settings_cannot_modify_platform_policy(self) -> None:
        current = assignment_settings()
        submitted = current.model_dump(mode="json")
        submitted["qq"]["bots"][0]["name"] = "Renamed worker"
        submitted["app"]["dry_run"] = False
        submitted["moderation"]["policy"] = "replacement"

        merged = _merge_settings_sections(submitted, current, ORCHESTRATION_SETTINGS_SECTIONS)

        self.assertEqual(merged["qq"]["bots"][0]["name"], "Renamed worker")
        self.assertEqual(merged["app"], current.model_dump(mode="json")["app"])
        self.assertEqual(merged["moderation"], current.model_dump(mode="json")["moderation"])

    def test_existing_bot_nickname_edit_preserves_connection_identity(self) -> None:
        config = assignment_settings().model_dump(mode="json")
        config["qq"]["bots"][0].update(
            {
                "access_token": "stable-token",
                "access_token_file": "data/secrets/qq/worker/onebot.token",
                "webui_token": "stable-webui-token",
                "webui_token_file": "data/secrets/qq/worker/webui.token",
            }
        )
        current = Settings.model_validate(config)
        submitted = current.redacted_dict()
        submitted["qq"]["bots"][0]["name"] = "New nickname"

        merged = _preserve_masked_secrets(submitted, current)

        self.assertEqual(merged["qq"]["bots"][0]["id"], "worker")
        self.assertEqual(merged["qq"]["bots"][0]["name"], "New nickname")
        self.assertEqual(merged["qq"]["bots"][0]["access_token"], "stable-token")
        self.assertEqual(merged["qq"]["bots"][0]["webui_token"], "stable-webui-token")

    def test_locked_new_external_bot_keeps_enabled_state_and_safe_connection(self) -> None:
        current = assignment_settings()
        submitted = current.redacted_dict()
        submitted["qq"]["bots"].append(
            {
                "id": "second",
                "name": "Second",
                "enabled": True,
                "connection_mode": "external",
                "onebot_base_url": "http://unsafe.example",
                "access_token_file": "shared.token",
                "webui_token_file": "shared-webui.token",
            }
        )

        merged = _preserve_masked_secrets(submitted, current)
        new_bot = merged["qq"]["bots"][1]

        self.assertTrue(new_bot["enabled"])
        self.assertEqual(new_bot["onebot_base_url"], "http://127.0.0.1:3000")
        self.assertEqual(new_bot["access_token_file"], "data/secrets/qq/second/onebot.token")
        self.assertEqual(new_bot["webui_token_file"], "data/secrets/qq/second/webui.token")

    def test_locked_first_bot_can_bind_the_bundled_napcat_sidecar(self) -> None:
        current = Settings.model_validate({"qq": {"bots": []}, "feishu": {"bots": []}})
        submitted = current.redacted_dict()
        submitted["qq"]["bots"].append(
            {
                "id": "primary",
                "name": "Primary",
                "enabled": True,
                "connection_mode": "bundled_napcat",
            }
        )

        merged = _preserve_masked_secrets(submitted, current)
        new_bot = Settings.model_validate(merged).qq_bot("primary")

        self.assertIsNotNone(new_bot)
        self.assertTrue(new_bot.enabled)
        self.assertEqual(new_bot.access_token_file, "data/secrets/napcat-onebot.token")
        self.assertEqual(new_bot.webui_token_file, "data/secrets/napcat-webui.token")

    def test_locked_bundled_bot_switches_to_independent_external_paths(self) -> None:
        current = Settings.model_validate(
            {
                "qq": {
                    "bots": [
                        {
                            "id": "primary",
                            "enabled": True,
                            "connection_mode": "bundled_napcat",
                        }
                    ]
                }
            }
        )
        submitted = current.redacted_dict()
        submitted["qq"]["bots"][0]["connection_mode"] = "external"

        merged = _preserve_masked_secrets(submitted, current)
        switched = Settings.model_validate(merged).qq_bot("primary")

        self.assertIsNotNone(switched)
        self.assertTrue(switched.enabled)
        self.assertEqual(switched.connection_mode, "external")
        self.assertEqual(switched.onebot_base_url, "http://127.0.0.1:3000")
        self.assertEqual(switched.access_token_file, "data/secrets/qq/primary/onebot.token")
        self.assertEqual(switched.webui_token_file, "data/secrets/qq/primary/webui.token")
        self.assertEqual(switched.qrcode_path, "data/napcat-cache/primary/qrcode.png")

    def test_bot_id_cannot_be_silently_renamed_through_the_api(self) -> None:
        current = assignment_settings()
        submitted = current.redacted_dict()
        submitted["qq"]["bots"][0]["id"] = "renamed-worker"

        with self.assertRaisesRegex(ValueError, "内部 ID 不可直接修改"):
            _validate_bot_identity_changes(submitted, current, BotIdentityChanges())

    def test_orchestration_endpoint_rejects_silent_bot_id_rename(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = assignment_settings().model_dump(mode="json")
            config["app"]["database_path"] = str(Path(directory) / "identity.db")
            config["app"]["message_archive_path"] = str(Path(directory) / "messages")
            config["gui"].update(
                {
                    "enabled": True,
                    "bootstrap_password": "temporary-test-password",
                }
            )
            settings = Settings.model_validate(config)
            config_path = Path(directory) / "config.yaml"
            settings.save(config_path)
            app = create_app(settings, config_path=config_path)
            app.state.container.auth.session = lambda _: GuiSession(
                username="admin",
                csrf_token="test-csrf",
                must_change_password=False,
                role="admin",
                token_hash="test-token-hash",
            )

            with TestClient(app) as client:
                client.cookies.set("neoqbot_session", "test-session")
                current = client.get("/api/gui/orchestration").json()
                current["config"]["qq"]["bots"][0]["id"] = "renamed-worker"
                response = client.put(
                    "/api/gui/orchestration",
                    headers={"X-CSRF-Token": "test-csrf"},
                    json={
                        "config": current["config"],
                        "revision": current["revision"],
                        "identity_changes": {},
                    },
                )

        self.assertEqual(response.status_code, 422)
        self.assertIn("内部 ID 不可直接修改", response.json()["detail"])

    def test_explicit_bot_creation_is_accepted_by_identity_guard(self) -> None:
        current = assignment_settings()
        submitted = current.redacted_dict()
        submitted["qq"]["bots"].append(
            {
                "id": "second",
                "name": "Second",
                "enabled": False,
            }
        )

        _validate_bot_identity_changes(
            submitted,
            current,
            BotIdentityChanges(qq_created=["second"]),
        )


class NapCatInitializationTests(unittest.TestCase):
    def test_bundled_napcat_posts_to_the_identity_independent_webhook(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            napcat_config = Path(directory) / "napcat-config"
            napcat_config.mkdir()
            legacy_config = {
                "network": {
                    "httpClients": [
                        {
                            "name": "legacy-neoqbot-client",
                            "enable": True,
                            "url": "http://neoqbot:8080/webhooks/onebot/default",
                        },
                        {
                            "name": "another-service",
                            "enable": True,
                            "url": "http://example.test/events",
                        },
                    ]
                }
            }
            (napcat_config / "onebot11.json").write_text(
                json.dumps(legacy_config),
                encoding="utf-8",
            )
            account_config_path = napcat_config / "onebot11_2523026981.json"
            account_config_path.write_text(
                json.dumps(legacy_config),
                encoding="utf-8",
            )
            result = initialize_napcat(
                napcat_config,
                Path(directory) / "secrets",
            )
            onebot = json.loads(Path(result["onebot_config"]).read_text(encoding="utf-8"))
            account_onebot = json.loads(account_config_path.read_text(encoding="utf-8"))
            expected_token = Path(result["onebot_token_file"]).read_text(encoding="utf-8").strip()
            diagnostics = napcat_event_client_diagnostics(napcat_config, expected_token)
            mismatched_account = json.loads(json.dumps(account_onebot))
            account_event_client = next(
                item
                for item in mismatched_account["network"]["httpClients"]
                if item["name"] == "neoqbot-events"
            )
            account_event_client["token"] = "mismatched-token"
            account_config_path.write_text(json.dumps(mismatched_account), encoding="utf-8")
            mismatch = napcat_event_client_diagnostics(napcat_config, expected_token)

        for configured in (onebot, account_onebot):
            event_client = next(
                item
                for item in configured["network"]["httpClients"]
                if item["name"] == "neoqbot-events"
            )
            self.assertEqual(event_client["url"], "http://neoqbot:8080/webhooks/onebot")
            self.assertEqual(
                sum(
                    item.get("name") == "neoqbot-events"
                    for item in configured["network"]["httpClients"]
                ),
                1,
            )
            self.assertEqual(event_client["token"], expected_token)
            self.assertFalse(
                any(
                    item.get("url") == "http://neoqbot:8080/webhooks/onebot/default"
                    for item in configured["network"]["httpClients"]
                )
            )
        self.assertTrue(
            any(item.get("name") == "another-service" for item in onebot["network"]["httpClients"])
        )
        self.assertTrue(diagnostics["ok"])
        self.assertNotIn(expected_token, json.dumps(diagnostics))
        self.assertFalse(mismatch["ok"])
        self.assertTrue(any(not item["token_matches_expected"] for item in mismatch["clients"]))


class WebhookIsolationTests(unittest.TestCase):
    def test_bundled_napcat_hmac_signature_reaches_message_recorder(self) -> None:
        """NapCat HTTP Client signs events; it does not send an Authorization header."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            settings = Settings.model_validate(
                {
                    "app": {
                        "database_path": str(root / "napcat-signature.db"),
                        "message_archive_path": str(root / "messages"),
                    },
                    "gui": {"enabled": False},
                    "qq": {
                        "bots": [
                            {
                                "id": "mualliance1",
                                "name": "Bundled",
                                "enabled": True,
                                "connection_mode": "bundled_napcat",
                            }
                        ]
                    },
                    "feishu": {"bots": []},
                    "orchestration": {
                        "resources": [
                            {
                                "id": "main-group",
                                "kind": "qq_group",
                                "name": "Main",
                                "external_id": "1081589022",
                            }
                        ],
                        "edges": [
                            {
                                "id": "bundled-main",
                                "source": "qq-bot:mualliance1",
                                "target": "main-group",
                                "relation": "observes",
                                "tasks": {"message_detection": {"record": True}},
                            }
                        ],
                    },
                }
            )
            event = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 1081589022,
                "message_id": 39,
                "user_id": 2523026981,
                "sender": {"card": "USTB LYOfficial"},
                "time": 1786012695,
                "message": [{"type": "text", "data": {"text": "signed event"}}],
            }
            body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            token = "test-onebot-token"
            signature = "sha1=" + hmac.new(token.encode("utf-8"), body, hashlib.sha1).hexdigest()
            app = create_app(settings)

            with (
                patch("neoqbot.app.resolve_secret", return_value=token),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/webhooks/onebot",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Signature": signature,
                    },
                )
                deadline = time.monotonic() + 2
                while (
                    app.state.container.database.counts()["group_messages"] == 0
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                records = app.state.container.database.recent_records(
                    "messages", group_id="1081589022", limit=10
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["bot_id"], "mualliance1")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], "signed event")

    def test_identity_independent_webhook_routes_to_the_bundled_bot(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            settings = Settings.model_validate(
                {
                    "app": {
                        "database_path": str(Path(directory) / "bundled-webhook.db"),
                        "message_archive_path": str(Path(directory) / "messages"),
                    },
                    "gui": {"enabled": False},
                    "qq": {
                        "bots": [
                            {
                                "id": "mualliance1",
                                "name": "Bundled",
                                "enabled": True,
                                "connection_mode": "bundled_napcat",
                                "administrator_qq_ids": ["10001"],
                            }
                        ]
                    },
                    "feishu": {"bots": []},
                    "orchestration": {
                        "resources": [
                            {
                                "id": "main-group",
                                "kind": "qq_group",
                                "name": "Main",
                                "external_id": "100",
                            }
                        ],
                        "edges": [
                            {
                                "id": "bundled-main",
                                "source": "qq-bot:mualliance1",
                                "target": "main-group",
                                "relation": "manages",
                            }
                        ],
                    },
                }
            )
            app = create_app(settings)
            submit = AsyncMock()
            app.state.container.runtime.submit = submit

            with (
                patch("neoqbot.app.resolve_secret", return_value="test-onebot-token"),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/webhooks/onebot",
                    headers={"Authorization": "Bearer test-onebot-token"},
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 100,
                        "message_id": 1,
                        "user_id": 2,
                        "message": "bundled event",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["bot_id"], "mualliance1")
        submit.assert_awaited_once()

    def test_bearer_token_works_when_a_legacy_hmac_secret_is_also_configured(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = assignment_settings().model_dump(mode="json")
            config["app"]["database_path"] = str(Path(directory) / "webhook.db")
            config["app"]["message_archive_path"] = str(Path(directory) / "messages")
            config["qq"]["bots"][0].update(
                {
                    "access_token": "test-onebot-token",
                    "access_token_file": "",
                    "webhook_secret": "legacy-hmac-secret",
                }
            )
            settings = Settings.model_validate(config)
            app = create_app(settings)
            submit = AsyncMock()
            app.state.container.runtime.submit = submit

            with TestClient(app) as client:
                accepted = client.post(
                    "/webhooks/onebot/worker",
                    headers={"Authorization": "Bearer test-onebot-token"},
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 100,
                        "message_id": 1,
                        "user_id": 2,
                        "message": "accepted via bearer",
                    },
                )
                rejected = client.post(
                    "/webhooks/onebot/worker",
                    headers={"Authorization": "Bearer wrong-token"},
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 100,
                        "message_id": 2,
                        "user_id": 2,
                        "message": "must not be accepted",
                    },
                )
                audit_records = app.state.container.database.recent_records(
                    "audit", search="onebot_webhook_auth", limit=10
                )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(rejected.status_code, 401)
        submit.assert_awaited_once()
        self.assertEqual(len(audit_records), 1)
        audit_details = audit_records[0]["details_json"]
        self.assertTrue(audit_details["webhook_secret_configured"])
        self.assertTrue(audit_details["access_token_configured"])
        self.assertNotIn("wrong-token", json.dumps(audit_details))
        self.assertNotIn("test-onebot-token", json.dumps(audit_details))

    def test_legacy_hmac_signature_is_accepted_but_an_invalid_signature_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = assignment_settings().model_dump(mode="json")
            config["app"]["database_path"] = str(Path(directory) / "legacy-hmac.db")
            config["app"]["message_archive_path"] = str(Path(directory) / "messages")
            config["qq"]["bots"][0].update(
                {
                    "access_token": "",
                    "access_token_file": "",
                    "webhook_secret": "legacy-hmac-secret",
                }
            )
            settings = Settings.model_validate(config)
            app = create_app(settings)
            submit = AsyncMock()
            app.state.container.runtime.submit = submit
            event = {
                "post_type": "message",
                "message_type": "group",
                "group_id": 100,
                "message_id": 3,
                "user_id": 2,
                "message": "legacy hmac event",
            }
            body = json.dumps(event, separators=(",", ":")).encode("utf-8")
            signature = "sha1=" + hmac.new(b"legacy-hmac-secret", body, hashlib.sha1).hexdigest()

            with TestClient(app) as client:
                accepted = client.post(
                    "/webhooks/onebot/worker",
                    content=body,
                    headers={"Content-Type": "application/json", "X-Signature": signature},
                )
                rejected = client.post(
                    "/webhooks/onebot/worker",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Signature": "sha1=" + "0" * 40,
                    },
                )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(rejected.status_code, 401)
        submit.assert_awaited_once()

    def test_legacy_default_webhook_alias_reaches_the_real_message_recorder(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            settings = Settings.model_validate(
                {
                    "app": {
                        "database_path": str(root / "legacy-webhook.db"),
                        "message_archive_path": str(root / "messages"),
                    },
                    "gui": {"enabled": False},
                    "qq": {
                        "bots": [
                            {
                                "id": "mualliance1",
                                "name": "Bundled",
                                "enabled": True,
                                "connection_mode": "bundled_napcat",
                            }
                        ]
                    },
                    "feishu": {"bots": []},
                    "orchestration": {
                        "resources": [
                            {
                                "id": "main-group",
                                "kind": "qq_group",
                                "name": "Main",
                                "external_id": "1081589022",
                            }
                        ],
                        "edges": [
                            {
                                "id": "bundled-main",
                                "source": "qq-bot:mualliance1",
                                "target": "main-group",
                                "relation": "observes",
                                "tasks": {"message_detection": {"record": True}},
                            }
                        ],
                    },
                }
            )
            app = create_app(settings)

            with (
                patch("neoqbot.app.resolve_secret", return_value="test-onebot-token"),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/webhooks/onebot/default/",
                    headers={"Authorization": "Bearer test-onebot-token"},
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 1081589022,
                        "message_id": 38,
                        "user_id": 2523026981,
                        "sender": {"card": "USTB LYOfficial"},
                        "time": 1786012694,
                        "message": [{"type": "text", "data": {"text": "test2"}}],
                    },
                )
                deadline = time.monotonic() + 2
                while (
                    app.state.container.database.counts()["group_messages"] == 0
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                records = app.state.container.database.recent_records(
                    "messages", group_id="1081589022", limit=10
                )
                alias_audit = app.state.container.database.recent_records(
                    "audit", search="onebot_webhook_alias", limit=10
                )
                unknown = client.post(
                    "/webhooks/onebot/obsolete",
                    headers={"Authorization": "Bearer test-onebot-token"},
                    json={"post_type": "meta_event"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["bot_id"], "mualliance1")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["sender_name"], "USTB LYOfficial")
        self.assertEqual(records[0]["text"], "test2")
        self.assertEqual(len(alias_audit), 1)
        self.assertEqual(alias_audit[0]["details_json"]["alias"], "default")
        self.assertEqual(unknown.status_code, 404)

    def test_unmanaged_group_event_is_not_submitted_to_runtime(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = assignment_settings().model_dump(mode="json")
            config["app"]["database_path"] = str(Path(directory) / "webhook.db")
            config["app"]["message_archive_path"] = str(Path(directory) / "messages")
            config["qq"]["bots"][0]["access_token"] = "test-onebot-token"
            config["qq"]["bots"][0]["access_token_file"] = ""
            settings = Settings.model_validate(config)
            app = create_app(settings)
            submit = AsyncMock()
            app.state.container.runtime.submit = submit

            with TestClient(app) as client:
                response = client.post(
                    "/webhooks/onebot/worker",
                    headers={"Authorization": "Bearer test-onebot-token"},
                    json={
                        "post_type": "message",
                        "message_type": "group",
                        "group_id": 999,
                        "message_id": 1,
                        "user_id": 2,
                        "message": "outside orchestration",
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reason"], "unmanaged_group")
        submit.assert_not_awaited()


class _MessageDatabase:
    def __init__(self) -> None:
        self.saved: list[GroupMessage] = []

    def save_message(self, message: GroupMessage) -> bool:
        if any(
            item.bot_id == message.bot_id
            and item.group_id == message.group_id
            and item.message_id == message.message_id
            for item in self.saved
        ):
            return False
        self.saved.append(message)
        return True


class _Recorder:
    def __init__(self) -> None:
        self.saved: list[GroupMessage] = []

    def append(self, message: GroupMessage) -> None:
        self.saved.append(message)


class _CaptureService:
    def __init__(self) -> None:
        self.message: GroupMessage | None = None

    def capture(self, message: GroupMessage) -> bool:
        self.message = message
        return True


class EventCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_sender_card_is_preserved_with_the_message(self) -> None:
        moderation = _CaptureService()
        handler = EventHandler(
            {"worker": object()},  # type: ignore[arg-type]
            {"worker": moderation},  # type: ignore[arg-type]
            {"worker": object()},  # type: ignore[arg-type]
        )

        result = await handler.handle(
            {
                "post_type": "message",
                "message_type": "group",
                "message_id": 10,
                "group_id": 200,
                "user_id": 300,
                "sender": {"nickname": "Nickname", "card": "Group Card"},
                "message": [{"type": "text", "data": {"text": "hello"}}],
            },
            "worker",
        )

        self.assertEqual(result, "captured")
        assert moderation.message is not None
        self.assertEqual(moderation.message.sender_name, "Group Card")
        self.assertEqual(moderation.message.user_id, "300")
        self.assertEqual(moderation.message.text, "hello")


class ServiceIsolationTests(unittest.TestCase):
    def test_message_capture_uses_the_current_group_assignment(self) -> None:
        settings = assignment_settings()
        database = _MessageDatabase()
        recorder = _Recorder()
        service = ModerationService(
            settings,
            database,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            settings.qq_bot("worker"),
            recorder,  # type: ignore[arg-type]
        )
        sent_at = datetime.now(UTC)

        captured = service.capture(
            GroupMessage(
                bot_id="worker",
                message_id="m1",
                group_id="100",
                user_id="u1",
                text="record me",
                sent_at=sent_at,
            )
        )
        ignored = service.capture(
            GroupMessage(
                bot_id="worker",
                message_id="m2",
                group_id="999",
                user_id="u1",
                text="outside orchestration",
                sent_at=sent_at,
            )
        )

        self.assertTrue(captured)
        self.assertFalse(ignored)
        self.assertEqual([item.message_id for item in database.saved], ["m1"])
        self.assertEqual([item.message_id for item in recorder.saved], ["m1"])

    def test_scheduled_analysis_alone_buffers_messages_without_jsonl_recording(self) -> None:
        settings = assignment_settings()
        database = _MessageDatabase()
        recorder = _Recorder()
        service = ModerationService(
            settings,
            database,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            settings.qq_bot("worker"),
            recorder,  # type: ignore[arg-type]
        )

        captured = service.capture(
            GroupMessage(
                bot_id="worker",
                message_id="analysis-buffer",
                group_id="200",
                user_id="u1",
                text="analyze me later",
                sent_at=datetime.now(UTC),
            )
        )

        self.assertTrue(captured)
        self.assertEqual([item.message_id for item in database.saved], ["analysis-buffer"])
        self.assertEqual(recorder.saved, [])

    def test_record_and_analysis_capture_each_event_only_once(self) -> None:
        raw = assignment_settings().model_dump(mode="json")
        analyze_edge = next(
            edge for edge in raw["orchestration"]["edges"] if edge["id"] == "worker-analyze"
        )
        analyze_edge["tasks"]["message_detection"]["record"] = True
        settings = Settings.model_validate(raw)
        database = _MessageDatabase()
        recorder = _Recorder()
        service = ModerationService(
            settings,
            database,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            settings.qq_bot("worker"),
            recorder,  # type: ignore[arg-type]
        )
        message = GroupMessage(
            bot_id="worker",
            message_id="shared-capture",
            group_id="200",
            user_id="u1",
            text="save once",
            sent_at=datetime.now(UTC),
        )

        self.assertTrue(service.capture(message))
        self.assertFalse(service.capture(message))
        self.assertEqual([item.message_id for item in database.saved], ["shared-capture"])
        self.assertEqual([item.message_id for item in recorder.saved], ["shared-capture"])


class MessageAnalysisReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_reuses_the_captured_sqlite_window(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            raw = assignment_settings().model_dump(mode="json")
            analyze_edge = next(
                edge for edge in raw["orchestration"]["edges"] if edge["id"] == "worker-analyze"
            )
            analyze_edge["tasks"]["message_detection"]["record"] = True
            settings = Settings.model_validate(raw)
            database = Database(Path(directory) / "messages.db")
            database.initialize()
            engine = AsyncMock()
            engine.moderate_messages.return_value = ModerationResult(safe=True, summary="safe")
            recorder = _Recorder()
            service = ModerationService(
                settings,
                database,
                engine,
                object(),  # type: ignore[arg-type]
                settings.qq_bot("worker"),
                recorder,  # type: ignore[arg-type]
            )
            window_end = datetime.now(UTC).replace(microsecond=0)
            message = GroupMessage(
                bot_id="worker",
                message_id="window-message",
                group_id="200",
                user_id="u1",
                text="reuse this record",
                sent_at=window_end,
            )

            self.assertTrue(service.capture(message))
            result = await service.run_group("200", window_end + timedelta(seconds=1))

            self.assertEqual(result, "safe")
            engine.moderate_messages.assert_awaited_once()
            analyzed_messages = engine.moderate_messages.await_args.args[0]
            self.assertEqual([item.message_id for item in analyzed_messages], ["window-message"])
            self.assertEqual([item.message_id for item in recorder.saved], ["window-message"])


class _RuntimeService:
    def __init__(self) -> None:
        self.groups: list[str] = []

    async def run_group(self, group_id: str, _: datetime | None = None) -> str:
        self.groups.append(group_id)
        return "ok"

    async def sync_group(self, group_id: str) -> dict[str, int]:
        self.groups.append(group_id)
        return {"synced": 1}


class _RepeatingAnnouncementService:
    def __init__(self) -> None:
        self.groups: list[str] = []
        self.runtime: Runtime | None = None

    async def sync_group(self, group_id: str) -> dict[str, int]:
        self.groups.append(group_id)
        if len(self.groups) >= 2 and self.runtime is not None:
            self.runtime._stopping.set()
        return {"synced": 1}


class RuntimeIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_jobs_only_run_groups_with_the_matching_task(self) -> None:
        settings = assignment_settings()
        moderation = _RuntimeService()
        announcements = _RuntimeService()
        runtime = Runtime(
            settings,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            {"worker": moderation},  # type: ignore[arg-type]
            {"worker": announcements},  # type: ignore[arg-type]
        )

        moderation_result = await runtime.run_bot_moderation("worker", datetime.now(UTC))
        announcement_result = await runtime.sync_bot_announcements("worker")

        self.assertEqual(moderation_result, {"200": "ok"})
        self.assertEqual(moderation.groups, ["200"])
        self.assertEqual(announcement_result, {"200": {"synced": 1}})
        self.assertEqual(announcements.groups, ["200"])

    async def test_enabled_announcement_sync_runs_immediately_and_then_periodically(self) -> None:
        settings = assignment_settings()
        announcements = _RepeatingAnnouncementService()
        runtime = Runtime(
            settings,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            {},
            {"worker": announcements},  # type: ignore[arg-type]
        )
        announcements.runtime = runtime

        with patch("neoqbot.runtime.asyncio.sleep", new=AsyncMock(return_value=None)) as sleep:
            await runtime._announcement_loop("worker", "200", 30)

        self.assertEqual(announcements.groups, ["200", "200"])
        sleep.assert_awaited_once_with(30 * 60)


if __name__ == "__main__":
    unittest.main()
