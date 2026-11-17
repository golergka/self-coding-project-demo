"""Types shared by components. All payloads contain JSON-compatible values."""

from dataclasses import dataclass, field
from typing import Any, Literal

JSON = dict[str, Any]


@dataclass(frozen=True)
class Event:
    id: str
    kind: Literal["comment", "dm", "schedule"]
    conversation_id: str
    payload: JSON


@dataclass(frozen=True)
class RunRequest:
    id: str
    workflow_id: str
    project_id: str
    commit: str
    test_session_id: str | None
    event: Event
    attempt: int


@dataclass(frozen=True)
class RunResult:
    status: Literal["waiting", "completed", "failed", "uncertain"]
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    retryable: bool = False
