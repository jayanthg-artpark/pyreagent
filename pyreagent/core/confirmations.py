"""Human-in-the-loop gates.

A gated call does not run. It creates a pending confirmation that a human
approves or rejects (in Protocol Studio, or via the operator endpoints). The
caller then repeats the *identical* call with ``confirmation_id``. An approval
is bound to the exact device, capability and arguments, is single-use, and expires.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from .errors import ConfirmationRequired, Forbidden, InvalidArgument, NotFound
from .jobs import utcnow


class ConfirmationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    USED = "used"
    EXPIRED = "expired"


class Confirmation(BaseModel):
    id: str
    device: str
    capability: str
    args: dict[str, Any]
    caller: str
    hazard: str | None
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    created_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    reason: str | None = None


class ConfirmationManager:
    def __init__(self, *, ttl_s: float = 600, retention: int = 500) -> None:
        self._items: dict[str, Confirmation] = {}
        self._ttl = timedelta(seconds=ttl_s)
        self._retention = retention

    def _expire(self) -> None:
        now = utcnow()
        for c in self._items.values():
            if c.status in (ConfirmationStatus.PENDING, ConfirmationStatus.APPROVED) and c.expires_at <= now:
                c.status = ConfirmationStatus.EXPIRED

    def request(self, *, device: str, capability: str, args: dict[str, Any], caller: str, hazard: str | None) -> tuple[Confirmation, bool]:
        """Return the pending confirmation for this exact call, creating it if needed.

        The bool is True when a new confirmation was created.
        """
        self._expire()
        for c in self._items.values():
            if (c.status is ConfirmationStatus.PENDING and c.device == device and c.capability == capability
                    and c.args == args and c.caller == caller):
                return c, False
        now = utcnow()
        c = Confirmation(
            id=uuid.uuid4().hex[:12],
            device=device,
            capability=capability,
            args=args,
            caller=caller,
            hazard=hazard,
            created_at=now,
            expires_at=now + self._ttl,
        )
        self._items[c.id] = c
        self._prune()
        return c, True

    def get(self, confirmation_id: str) -> Confirmation:
        self._expire()
        try:
            return self._items[confirmation_id]
        except KeyError:
            raise NotFound(f"No confirmation with id {confirmation_id!r}.") from None

    def list(self, *, status: ConfirmationStatus | None = None) -> list[Confirmation]:
        self._expire()
        return [c for c in reversed(self._items.values()) if status is None or c.status == status]

    def decide(self, confirmation_id: str, *, approve: bool, operator: str, reason: str | None = None) -> Confirmation:
        c = self.get(confirmation_id)
        if c.status is not ConfirmationStatus.PENDING:
            raise InvalidArgument(f"Confirmation {c.id} is {c.status.value}, not pending.", recoverable=False)
        if operator == c.caller:
            raise Forbidden("The caller that requested an action cannot approve it.")
        c.status = ConfirmationStatus.APPROVED if approve else ConfirmationStatus.REJECTED
        c.decided_by = operator
        c.decided_at = utcnow()
        c.reason = reason
        if approve:
            # Give the agent the full TTL from approval to act on it.
            c.expires_at = c.decided_at + self._ttl
        return c

    def consume(self, confirmation_id: str, *, device: str, capability: str, args: dict[str, Any]) -> Confirmation:
        """Spend an approval on the call it was granted for."""
        c = self.get(confirmation_id)
        if c.device != device or c.capability != capability or c.args != args:
            raise InvalidArgument(
                f"Confirmation {c.id} was granted for {c.device}.{c.capability} with args {c.args}; "
                "it cannot be used for a different call.",
                recoverable=False,
                suggested_action="Repeat the exact call that was approved, or request a new confirmation by calling without confirmation_id.",
            )
        if c.status is ConfirmationStatus.PENDING:
            raise ConfirmationRequired(
                f"Confirmation {c.id} is still waiting for a human.",
                details={"confirmation_id": c.id, "hazard": c.hazard, "expires_at": c.expires_at.isoformat()},
            )
        if c.status is not ConfirmationStatus.APPROVED:
            raise InvalidArgument(
                f"Confirmation {c.id} is {c.status.value}" + (f": {c.reason}" if c.reason else "") + ".",
                recoverable=False,
                suggested_action="Do not retry. Ask the human why it was not approved." if c.status is ConfirmationStatus.REJECTED
                else "Call again without confirmation_id to request a new approval.",
            )
        c.status = ConfirmationStatus.USED
        return c

    def _prune(self) -> None:
        overflow = len(self._items) - self._retention
        if overflow <= 0:
            return
        done = [c.id for c in self._items.values() if c.status not in (ConfirmationStatus.PENDING, ConfirmationStatus.APPROVED)]
        for cid in done[:overflow]:
            del self._items[cid]
