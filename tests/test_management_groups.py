from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from neoqbot.adapters.onebot import OneBotClient, OneBotError
from neoqbot.config import QQBotConfig, Settings


def bot_config(**overrides: object) -> QQBotConfig:
    data: dict[str, object] = {
        "id": "default",
        "name": "Test Bot",
        "connection_mode": "external",
        "administrator_qq_ids": ["10001"],
        "management_group_ids": [],
    }
    data.update(overrides)
    return QQBotConfig(**data)


class ManagementGroupConfigTests(unittest.TestCase):
    def test_management_group_ids_defaults_to_empty(self) -> None:
        bot = QQBotConfig(id="x", name="X")
        self.assertEqual(bot.management_group_ids, [])

    def test_ids_as_strings_coerces_and_strips(self) -> None:
        bot = QQBotConfig(
            id="x",
            name="X",
            administrator_qq_ids=[" 10001 ", 0],
            management_group_ids=[" 123 ", "", "456"],
        )
        self.assertEqual(bot.administrator_qq_ids, ["10001", "0"])
        self.assertEqual(bot.management_group_ids, ["123", "456"])

    def test_settings_round_trips_management_group_ids(self) -> None:
        settings = Settings.model_validate(
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
                            "management_group_ids": ["20001", "20002"],
                        }
                    ]
                },
            }
        )
        dumped = settings.model_dump(mode="json")
        self.assertEqual(
            dumped["qq"]["bots"][0]["management_group_ids"], ["20001", "20002"]
        )


class SendGroupMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_group_message_calls_send_group_msg(self) -> None:
        client = OneBotClient(bot_config(management_group_ids=["20001"]))
        client._call = AsyncMock(return_value={})
        await client.send_group_message("20001", "hello")
        client._call.assert_awaited_once_with(
            "send_group_msg", {"group_id": 20001, "message": "hello"}
        )

    async def test_send_group_message_respects_dry_run(self) -> None:
        client = OneBotClient(bot_config(), dry_run=True)
        client._call = AsyncMock(return_value={})
        await client.send_group_message("20001", "hello")
        client._call.assert_not_awaited()


class NotifyAdministratorsTests(unittest.IsolatedAsyncioTestCase):
    async def test_notifies_admins_and_management_groups(self) -> None:
        client = OneBotClient(
            bot_config(
                administrator_qq_ids=["10001", "10002"],
                management_group_ids=["20001", "20002"],
            )
        )
        client._call = AsyncMock(return_value={})
        await client.notify_administrators("alert")
        # 2 private + 2 group messages
        self.assertEqual(client._call.await_count, 4)
        actions = [call.args[0] for call in client._call.call_args_list]
        self.assertEqual(actions.count("send_private_msg"), 2)
        self.assertEqual(actions.count("send_group_msg"), 2)

    async def test_only_management_groups_works_without_admins(self) -> None:
        client = OneBotClient(
            bot_config(administrator_qq_ids=[], management_group_ids=["20001"])
        )
        client._call = AsyncMock(return_value={})
        # 不应抛错：配置里没有管理员但配置了管理群
        await client.notify_administrators("alert")
        client._call.assert_awaited_once_with(
            "send_group_msg", {"group_id": 20001, "message": "alert"}
        )

    async def test_raises_when_nothing_configured(self) -> None:
        client = OneBotClient(bot_config(administrator_qq_ids=[], management_group_ids=[]))
        client._call = AsyncMock(return_value={})
        with self.assertRaises(OneBotError):
            await client.notify_administrators("alert")
        client._call.assert_not_awaited()

    async def test_partial_group_failure_still_delivers(self) -> None:
        client = OneBotClient(
            bot_config(
                administrator_qq_ids=["10001"],
                management_group_ids=["20001"],
            )
        )

        async def side_effect(action, params=None):
            if action == "send_group_msg":
                raise OneBotError("boom")
            return {}

        client._call = AsyncMock(side_effect=side_effect)
        # 管理员私聊成功，群消息失败：已成功投递，不应抛出
        await client.notify_administrators("alert")
        self.assertEqual(client._call.await_count, 2)

    async def test_all_failures_raise(self) -> None:
        client = OneBotClient(
            bot_config(
                administrator_qq_ids=["10001"],
                management_group_ids=["20001"],
            )
        )
        client._call = AsyncMock(side_effect=OneBotError("boom"))
        with self.assertRaises(OneBotError):
            await client.notify_administrators("alert")


if __name__ == "__main__":
    unittest.main()
