from __future__ import annotations

from typing import Protocol

from .config import JoinApprovalConfig
from .models import (
    Announcement,
    GroupMessage,
    JoinDecision,
    JoinRequest,
    ModerationResult,
    SearchHit,
)


class DecisionEngine(Protocol):
    async def review_join(
        self, request: JoinRequest, policy: JoinApprovalConfig | None = None
    ) -> JoinDecision: ...

    async def moderate_messages(self, messages: list[GroupMessage]) -> ModerationResult: ...


class QQGateway(Protocol):
    async def approve_join(self, request: JoinRequest, approve: bool, reason: str = "") -> None: ...

    async def notify_administrators(self, message: str) -> None: ...

    async def send_private_message(self, user_id: str, message: str) -> None: ...
    async def send_group_message(self, group_id: str, message: str) -> None: ...

    async def fetch_announcements(self, group_id: str) -> list[Announcement]: ...

    async def doctor(self) -> dict[str, object]: ...

    async def close(self) -> None: ...


class FeishuGateway(Protocol):
    async def archive_announcement(self, announcement: Announcement) -> None: ...

    async def search(self, query: str, limit: int) -> list[SearchHit]: ...

    async def doctor(self) -> dict[str, object]: ...

    async def login(self) -> object: ...

    async def logout(self) -> object: ...
