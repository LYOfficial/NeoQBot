from __future__ import annotations

import unittest

import httpx

from neoqbot.config import QQBotConfig
from neoqbot.napcat import NapCatWebUiClient
from neoqbot.qq_status import classify_qq_connection


class QQLoginRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_relogin_requests_a_new_qrcode(self) -> None:
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request.url.path)
            if request.url.path == "/api/auth/login":
                return httpx.Response(200, json={"data": {"Credential": "credential"}})
            if request.url.path == "/api/QQLogin/RefreshQRcode":
                return httpx.Response(200, json={"data": {"qrcodeurl": "qrcode"}})
            return httpx.Response(404)

        client = NapCatWebUiClient(
            QQBotConfig(id="test", webui_token="webui-secret"),
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.relogin()
        finally:
            await client.close()

        self.assertEqual(result, {"qrcodeurl": "qrcode"})
        self.assertEqual(requests, ["/api/auth/login", "/api/QQLogin/RefreshQRcode"])

    def test_offline_napcat_state_wins_over_stale_onebot_health(self) -> None:
        state, message = classify_qq_connection(
            {"ok": True, "login_info": {"user_id": 123}},
            {"ok": True, "isLogin": True, "isOffline": True},
        )

        self.assertEqual(state, "qq_offline")
        self.assertEqual(message, "QQ 登录态存在，但客户端已离线")
