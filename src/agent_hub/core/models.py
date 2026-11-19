"""Row types shared by the hub core."""

import json
from dataclasses import dataclass, field

OPEN_STATES = ("queued", "running")
TASK_STATES = ("open", "assigned", "in_progress", "blocked", "done", "failed", "cancelled")
TERMINAL = ("done", "failed", "cancelled")


class HubError(Exception):
    """An error that is safe to show to the caller. `code` is stable; `message` is for people."""

    def __init__(self, code: str, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.extra = extra or {}         # more fields for the HTTP error body, e.g. the current task


def not_found(conversation_id: str) -> HubError:
    # The same error for "does not exist" and "not yours", so an agent cannot probe for DMs.
    return HubError("unknown_conversation", f"unknown conversation: {conversation_id}", 404)


def task_not_found(task_id: str) -> HubError:
    return HubError("unknown_task", f"unknown task: {task_id}", 404)


def artifact_not_found(artifact_id: str) -> HubError:
    return HubError("unknown_artifact", f"unknown artifact: {artifact_id}", 404)


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
    def is_system(self) -> bool:
        return self.role == "system"

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
    archived_at: str | None = None


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
    task_id: str | None = None     # set on kind='task' messages
    actor_id: str | None = None    # who caused a task message (the sender is 'hub')
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row) -> "Message":
        return cls(row["id"], row["conversation_id"], row["sender_id"], row["kind"], row["body"],
                   json.loads(row["mentions_json"]), row["reply_to"], row["hop"], row["turn_id"],
                   row["created_at"], row["task_id"], row["actor_id"], json.loads(row["meta_json"] or "{}"))


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
    task_id: str | None = None     # the task a task wake was about

    @property
    def open(self) -> bool:
        return self.state in OPEN_STATES

    @classmethod
    def from_row(cls, row) -> "Turn":
        return cls(row["id"], row["agent_id"], row["conversation_id"], row["trigger_msg_id"],
                   row["state"], row["hop"], json.loads(row["rest_json"]), row["group_id"],
                   row["held_body"], row["cost_usd"], row["tokens_in"], row["tokens_out"],
                   row["created_at"], row["started_at"], row["finished_at"], row["error"], row["task_id"])


@dataclass
class Task:
    id: str
    conversation_id: str
    number: int
    parent_id: str | None
    depth: int
    title: str
    description: str
    acceptance: str | None
    priority: str
    labels: list[str]
    creator_id: str
    assignee_id: str | None
    state: str
    result: str | None
    blocked_reason: str | None
    reassignments: int
    version: int
    hop: int
    created_at: str
    updated_at: str
    closed_at: str | None

    @property
    def ref(self) -> str:
        return f"{self.conversation_id}#{self.number}"

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    @classmethod
    def from_row(cls, row) -> "Task":
        return cls(row["id"], row["conversation_id"], row["number"], row["parent_id"], row["depth"], row["title"],
                   row["description"], row["acceptance"], row["priority"], json.loads(row["labels_json"]),
                   row["creator_id"], row["assignee_id"], row["state"], row["result"], row["blocked_reason"],
                   row["reassignments"], row["version"], row["hop"], row["created_at"], row["updated_at"],
                   row["closed_at"])


@dataclass
class TaskEvent:
    id: str
    task_id: str
    actor_id: str
    type: str          # created | claimed | assigned | released | state | note | edited | artifact | children_done
    from_state: str | None
    to_state: str | None
    note: str | None
    data: dict
    message_id: str | None
    hop: int
    at: str

    @classmethod
    def from_row(cls, row) -> "TaskEvent":
        return cls(row["id"], row["task_id"], row["actor_id"], row["type"], row["from_state"], row["to_state"],
                   row["note"], json.loads(row["data_json"]), row["message_id"], row["hop"], row["at"])


@dataclass
class Artifact:
    id: str
    task_id: str
    conversation_id: str
    uploader_id: str
    name: str
    description: str | None
    media_type: str
    size: int
    sha256: str
    supersedes: str | None
    message_id: str | None
    created_at: str
    deleted_at: str | None

    @classmethod
    def from_row(cls, row) -> "Artifact":
        return cls(row["id"], row["task_id"], row["conversation_id"], row["uploader_id"], row["name"],
                   row["description"], row["media_type"], row["size"], row["sha256"], row["supersedes"],
                   row["message_id"], row["created_at"], row["deleted_at"])
