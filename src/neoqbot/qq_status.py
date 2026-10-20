from __future__ import annotations

from typing import Any


def classify_qq_connection(
    onebot_status: dict[str, Any], napcat_status: dict[str, Any]
) -> tuple[str, str]:
    """Classify QQ connectivity using NapCat's real online state first."""
    if napcat_status.get("isOffline"):
        return "qq_offline", "QQ 登录态存在，但客户端已离线"
    if onebot_status.get("ok"):
        return "connected", "QQ 已登录，OneBot 连接正常"
    if napcat_status.get("isLogin"):
        return "qq_logged_in_onebot_unavailable", "QQ 已登录，但 OneBot HTTP 服务不可用"
    if napcat_status.get("ok"):
        return "waiting_for_scan", "NapCat 已连接，等待扫描二维码"
    return "napcat_unreachable", "NapCat WebUI 与 OneBot 均不可访问"
