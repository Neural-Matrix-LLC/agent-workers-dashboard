"""Row types shared by the hub core."""

import json
from dataclasses import dataclass, field

OPEN_STATES = ("queued", "running")


class HubError(Exception):
    """An error that is safe to show to the caller. `code` is stable; `message` is for people."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def not_found(conversation_id: str) -> HubError:
    # The same error for "does not exist" and "not yours", so an agent cannot probe for DMs.
    return HubError("unknown_conversation", f"unknown conversation: {conversation_id}", 404)


@dataclass
class Participant:
    id: str
    display_name: str
    role: str                      # owner | agent
    connector: str | None = None
    delivery: str = "sse"
    paused: bool = False
    config: dict = field(default_factory=dict)

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def self_driven(self) -> bool:
        return self.role == "agent" and self.delivery != "sse"


@dataclass
class Conversation:
    id: str
    kind: str                      # room | dm
    title: str | None
    mode: str
    config: dict
    created_at: str
    members: list[str]


@dataclass
class Message:
    id: str
    conversation_id: str
    sender_id: str
    kind: str                      # chat | system | notice (notice is owner-only)
    body: str
    mentions: list[str]
    reply_to: str | None
    hop: int
    turn_id: str | None
    created_at: str

    @classmethod
    def from_row(cls, row) -> "Message":
        return cls(row["id"], row["conversation_id"], row["sender_id"], row["kind"], row["body"],
                   json.loads(row["mentions_json"]), row["reply_to"], row["hop"], row["turn_id"],
                   row["created_at"])


@dataclass
class Turn:
    id: str
    agent_id: str
    conversation_id: str
    trigger_msg_id: str
    state: str                     # awaiting_approval | queued | running | done | failed | cancelled
    hop: int                       # a reply made in this turn gets hop + 1
    rest: list[str]                # agents still to wake in a sequential @all fan-out
    group_id: str | None           # parallel @all fan-out: replies are held until all are in
    held_body: str | None
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None

    @property
    def open(self) -> bool:
        return self.state in OPEN_STATES

    @classmethod
    def from_row(cls, row) -> "Turn":
        return cls(row["id"], row["agent_id"], row["conversation_id"], row["trigger_msg_id"],
                   row["state"], row["hop"], json.loads(row["rest_json"]), row["group_id"],
                   row["held_body"], row["cost_usd"], row["tokens_in"], row["tokens_out"],
                   row["created_at"], row["started_at"], row["finished_at"], row["error"])
