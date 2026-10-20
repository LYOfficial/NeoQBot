from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import QQBotConfig, QQConfig, resolve_secret


class NapCatError(RuntimeError):
    pass


def _write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_onebot_webhook_url(url: str, expected_path: str = "/webhooks/onebot") -> bool:
    """Return whether a URL targets the standard NeoQBot OneBot webhook family."""
    try:
        path = urlsplit(url).path.rstrip("/")
    except ValueError:
        return False
    expected = expected_path.rstrip("/")
    return path == expected or path.startswith(f"{expected}/")


def napcat_event_client_diagnostics(
    config_dir: str | Path,
    expected_token: str,
    webhook_url: str = "http://neoqbot:8080/webhooks/onebot",
) -> dict[str, object]:
    """Safely report whether persisted NapCat event clients match NeoQBot.

    The returned data deliberately contains only file names, URL paths and
    booleans.  It never returns a OneBot token, including a hash or prefix.
    """
    config_path = Path(config_dir)
    expected_path = urlsplit(webhook_url).path.rstrip("/")
    files = sorted(config_path.glob("onebot11*.json"))
    entries: list[dict[str, object]] = []
    errors: list[str] = []

    if not files:
        return {
            "ok": False,
            "config_files": [],
            "clients": [],
            "errors": ["No onebot11 configuration files were found"],
        }

    for path in files:
        network = _load_json(path).get("network")
        raw_clients = network.get("httpClients", []) if isinstance(network, dict) else []
        matches = [
            item
            for item in raw_clients
            if isinstance(item, dict)
            and (
                item.get("name") == "neoqbot-events"
                or _is_onebot_webhook_url(str(item.get("url") or ""))
            )
        ]
        if len(matches) != 1:
            errors.append(
                f"{path.name}: expected exactly one NeoQBot HTTP client, found {len(matches)}"
            )
        for item in matches:
            url = str(item.get("url") or "")
            try:
                url_path = urlsplit(url).path.rstrip("/")
            except ValueError:
                url_path = ""
            token = str(item.get("token") or "")
            entries.append(
                {
                    "file": path.name,
                    "name": str(item.get("name") or ""),
                    "enabled": bool(item.get("enable")),
                    "url_path": url_path,
                    "url_matches_expected": url_path == expected_path,
                    "token_configured": bool(token),
                    "token_matches_expected": bool(expected_token)
                    and hmac.compare_digest(token, expected_token),
                }
            )

    ok = (
        bool(expected_token)
        and bool(entries)
        and not errors
        and all(
            item["enabled"] and item["url_matches_expected"] and item["token_matches_expected"]
            for item in entries
        )
    )
    return {
        "ok": ok,
        "config_files": [path.name for path in files],
        "clients": entries,
        "errors": errors,
    }


def _ensure_secret(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"Refusing to write secret through symbolic link: {path}")
    try:
        current = path.read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    if current:
        os.chmod(path, 0o600)
        return current
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return value


def initialize_secrets(secret_dir: str | Path) -> dict[str, str]:
    """Create the credential files required by a fresh NeoQBot deployment."""
    secrets_path = Path(secret_dir)
    paths = {
        "onebot_token_file": secrets_path / "napcat-onebot.token",
        "webui_token_file": secrets_path / "napcat-webui.token",
        "admin_api_token_file": secrets_path / "admin-api.token",
        "gui_bootstrap_password_file": secrets_path / "gui-bootstrap-password",
    }
    for path in paths.values():
        _ensure_secret(path)
    return {name: str(path) for name, path in paths.items()}


def initialize_napcat(
    config_dir: str | Path,
    secret_dir: str | Path,
    webhook_url: str = "http://neoqbot:8080/webhooks/onebot",
    http_port: int = 3000,
    webui_port: int = 6099,
) -> dict[str, object]:
    """Create persistent secrets and enforce the NapCat endpoints NeoQBot requires."""
    webhook_url = webhook_url.rstrip("/")
    config_path = Path(config_dir)
    secret_files = initialize_secrets(secret_dir)
    onebot_token_path = Path(secret_files["onebot_token_file"])
    webui_token_path = Path(secret_files["webui_token_file"])
    onebot_token = onebot_token_path.read_text(encoding="utf-8").strip()
    webui_token = webui_token_path.read_text(encoding="utf-8").strip()

    webui_config_path = config_path / "webui.json"
    webui_config = _load_json(webui_config_path)
    webui_config.update(
        {
            "host": "0.0.0.0",
            "port": webui_port,
            "token": webui_token,
            "loginRate": int(webui_config.get("loginRate") or 3),
        }
    )
    _write_json(webui_config_path, webui_config)

    def replace_named(
        items: object,
        name: str,
        replacement: dict[str, Any],
        conflicts: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[Any]:
        source = items if isinstance(items, list) else []
        retained = [
            item
            for item in source
            if not (
                isinstance(item, dict)
                and (item.get("name") == name or (conflicts is not None and conflicts(item)))
            )
        ]
        retained.append(replacement)
        return retained

    def conflicts_with_neoqbot_webhook(item: dict[str, Any]) -> bool:
        url = str(item.get("url") or "")
        if url.rstrip("/") == webhook_url:
            return True
        return _is_onebot_webhook_url(url)

    def configure_onebot(path: Path) -> None:
        onebot_config = _load_json(path)
        network = onebot_config.get("network")
        if not isinstance(network, dict):
            network = {}
            onebot_config["network"] = network
        network["httpServers"] = replace_named(
            network.get("httpServers", []),
            "neoqbot-http",
            {
                "name": "neoqbot-http",
                "enable": True,
                "port": http_port,
                "host": "0.0.0.0",
                "enableCors": False,
                "enableWebsocket": False,
                "messagePostFormat": "array",
                "token": onebot_token,
                "debug": False,
            },
            conflicts=lambda item: item.get("port") == http_port,
        )
        network["httpClients"] = replace_named(
            network.get("httpClients", []),
            "neoqbot-events",
            {
                "name": "neoqbot-events",
                "enable": True,
                "url": webhook_url,
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "token": onebot_token,
                "debug": False,
            },
            conflicts=conflicts_with_neoqbot_webhook,
        )
        network.setdefault("httpSseServers", [])
        network.setdefault("websocketServers", [])
        network.setdefault("websocketClients", [])
        network.setdefault("plugins", [])
        _write_json(path, onebot_config)

    onebot_config_path = config_path / "onebot11.json"
    account_configs = set(config_path.glob("onebot11*.json"))
    account_configs.add(onebot_config_path)
    for candidate in sorted(account_configs):
        configure_onebot(candidate)

    event_client_diagnostics = napcat_event_client_diagnostics(
        config_path, onebot_token, webhook_url
    )
    if not event_client_diagnostics["ok"]:
        raise RuntimeError("NapCat OneBot event client initialization verification failed")

    return {
        "onebot_config": str(onebot_config_path),
        "webui_config": str(webui_config_path),
        "onebot_token_file": str(onebot_token_path),
        "webui_token_file": str(webui_token_path),
        "admin_api_token_file": secret_files["admin_api_token_file"],
        "gui_bootstrap_password_file": secret_files["gui_bootstrap_password_file"],
        "event_client_diagnostics": event_client_diagnostics,
    }


class NapCatWebUiClient:
    def __init__(
        self,
        config: QQConfig | QQBotConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._credential = ""
        self._client = httpx.AsyncClient(
            base_url=config.webui_base_url.rstrip("/") + "/",
            timeout=config.request_timeout_seconds,
            transport=transport,
        )

    def token(self) -> str:
        token = resolve_secret(self.config.webui_token, self.config.webui_token_file)
        if not token:
            raise NapCatError("NapCat WebUI Token 尚未初始化")
        return token

    @staticmethod
    def _data(payload: object) -> Any:
        if not isinstance(payload, dict):
            raise NapCatError("NapCat 返回了无效响应")
        code = payload.get("code")
        if code not in (None, 0):
            raise NapCatError(str(payload.get("message") or payload.get("msg") or payload))
        if payload.get("status") in ("failed", "error"):
            raise NapCatError(str(payload.get("message") or payload))
        return payload.get("data")

    async def _login(self) -> str:
        digest = hashlib.sha256((self.token() + ".napcat").encode("utf-8")).hexdigest()
        response = await self._client.post("api/auth/login", json={"hash": digest})
        response.raise_for_status()
        data = self._data(response.json())
        if not isinstance(data, dict):
            raise NapCatError("NapCat WebUI 登录响应缺少凭据")
        if data.get("require2FA"):
            raise NapCatError("NapCat WebUI 已启用 2FA，请先在 6099 控制台完成验证")
        credential = str(data.get("Credential") or data.get("credential") or "").strip()
        if not credential:
            raise NapCatError("NapCat WebUI 登录响应缺少凭据")
        self._credential = credential
        return credential

    async def _post(self, path: str, retry: bool = True) -> Any:
        if not self._credential:
            await self._login()
        response = await self._client.post(
            path.lstrip("/"), headers={"Authorization": f"Bearer {self._credential}"}, json={}
        )
        if response.status_code == 401 and retry:
            self._credential = ""
            await self._login()
            return await self._post(path, retry=False)
        response.raise_for_status()
        try:
            return self._data(response.json())
        except NapCatError as exc:
            message = str(exc).lower()
            if retry and any(
                marker in message
                for marker in ("authorization failed", "token has been revoked", "credential")
            ):
                self._credential = ""
                await self._login()
                return await self._post(path, retry=False)
            raise

    async def check_login_status(self) -> dict[str, Any]:
        data = await self._post("api/QQLogin/CheckLoginStatus")
        if not isinstance(data, dict):
            raise NapCatError("NapCat 登录状态响应无效")
        return data

    async def get_qrcode(self) -> dict[str, Any]:
        data = await self._post("api/QQLogin/GetQQLoginQrcode")
        return data if isinstance(data, dict) else {}

    async def refresh_qrcode(self) -> dict[str, Any]:
        data = await self._post("api/QQLogin/RefreshQRcode")
        return data if isinstance(data, dict) else {}

    async def relogin(self) -> dict[str, Any]:
        """Ask NapCat to leave the current offline login flow and create a QR code."""
        return await self.refresh_qrcode()

    async def close(self) -> None:
        await self._client.aclose()
