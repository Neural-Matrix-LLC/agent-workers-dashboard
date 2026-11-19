"""The task state machine and field limits (design M4 section 5). Pure: no I/O, no hub state.

"Creator" is the task's creator. "Coordinator" is the assignee of the parent task, if any. The owner may
perform every transition, subject to the owner-in-agent-DM rule that the task service applies.
"""

import re

from .models import TERMINAL, HubError

NON_TERMINAL = ("open", "assigned", "in_progress", "blocked")

# action -> (states it may start from, roles that may take it, the state it leads to or None for "same")
TRANSITIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...], str | None]] = {
    "assign": (("open", "assigned"), ("creator", "coordinator", "assignee"), "assigned"),
    "unassign": (("assigned",), ("creator", "coordinator"), "open"),
    "release": (("assigned", "in_progress"), ("assignee",), "open"),
    "start": (("assigned",), ("assignee",), "in_progress"),
    "block": (("in_progress",), ("assignee",), "blocked"),
    "unblock": (("blocked",), ("assignee",), "in_progress"),
    "finish": (("assigned", "in_progress", "blocked"), ("assignee",), "done"),
    "fail": (("assigned", "in_progress", "blocked"), ("assignee",), "failed"),
    "cancel": (NON_TERMINAL, ("creator", "coordinator"), "cancelled"),
    "reopen": (("done", "failed"), ("creator", "coordinator"), "assigned"),
    "note": (NON_TERMINAL + TERMINAL, ("creator", "assignee", "coordinator"), None),
    "edit": (NON_TERMINAL, ("creator", "coordinator"), None),
}
ACTIONS = tuple(TRANSITIONS)

REQUIRED = {"finish": "result", "fail": "result", "block": "blocked_reason", "reopen": "note", "note": "note",
            "assign": "assignee", "edit": "fields"}

LIMITS = {"title": 200, "description": 20_000, "acceptance": 5_000, "note": 5_000, "result": 20_000,
          "blocked_reason": 2_000}
LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_LABELS = 10
PRIORITIES = ("low", "normal", "high")
EDITABLE = ("title", "description", "acceptance", "priority", "labels")

# The owner's actions on a task whose home is an agent-to-agent DM (open decision OD-3, option a).
OWNER_MODERATION = ("cancel", "unassign", "assign")


def roles(actor_id: str, *, is_owner: bool, creator_id: str, assignee_id: str | None,
          coordinator_id: str | None) -> set[str]:
    found = set()
    if is_owner:
        found.add("owner")
    if actor_id == creator_id:
        found.add("creator")
    if assignee_id and actor_id == assignee_id:
        found.add("assignee")
    if coordinator_id and actor_id == coordinator_id:
        found.add("coordinator")
    return found


def allowed(action: str, state: str, who: set[str], *, owner_moderation_only: bool = False) -> bool:
    if action not in TRANSITIONS:
        return False
    states, permitted, _ = TRANSITIONS[action]
    if state not in states:
        return False
    if owner_moderation_only and action not in OWNER_MODERATION:
        return False
    if action == "assign" and state == "open":
        permitted = ("creator", "coordinator")     # only an assigned task can be handed off by its assignee
    return "owner" in who or bool(who & set(permitted))


def allowed_actions(state: str, who: set[str], *, owner_moderation_only: bool = False) -> list[str]:
    return [a for a in ACTIONS if allowed(a, state, who, owner_moderation_only=owner_moderation_only)]


def check(action: str, state: str, who: set[str], actor_id: str, *, owner_moderation_only: bool = False) -> None:
    """Raise bad_action, bad_transition (wrong state), or not_allowed (wrong role)."""
    if action not in TRANSITIONS:
        raise HubError("bad_action", f"action must be one of {', '.join(ACTIONS)}")
    if allowed(action, state, who, owner_moderation_only=owner_moderation_only):
        return
    may = allowed_actions(state, who, owner_moderation_only=owner_moderation_only)
    listing = f"{actor_id} may: {', '.join(may) if may else 'nothing'}"
    if state not in TRANSITIONS[action][0]:
        raise HubError("bad_transition", f"cannot {action} a task that is {state}; {listing}", 409)
    raise HubError("not_allowed", f"you may not {action} this task; {listing}", 403)


def target_state(action: str, state: str) -> str:
    return TRANSITIONS[action][2] or state


def require(action: str, values: dict) -> None:
    field = REQUIRED.get(action)
    if field is None:
        return
    value = values.get(field)
    if value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, dict) and not value):
        raise HubError("missing_field", f"{action} needs {field}")


def check_text(name: str, value, *, required: bool = False) -> str | None:
    """Validate one text field against LIMITS. Returns it unchanged (Markdown is stored as given)."""
    if value is None:
        if required:
            raise HubError("missing_field", f"{name} is required")
        return None
    if not isinstance(value, str):
        raise HubError("bad_field", f"{name} must be text")
    if required and not value.strip():
        raise HubError("missing_field", f"{name} is required")
    if len(value) > LIMITS[name]:
        raise HubError("bad_field", f"{name} is longer than {LIMITS[name]} characters")
    return value


def check_priority(value) -> str:
    if value not in PRIORITIES:
        raise HubError("bad_field", f"priority must be one of {', '.join(PRIORITIES)}")
    return value


def check_labels(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_LABELS:
        raise HubError("bad_field", f"labels must be a list of at most {MAX_LABELS}")
    for label in value:
        if not isinstance(label, str) or not LABEL.match(label):
            raise HubError("bad_field", f"label {label!r} must match {LABEL.pattern}")
    return list(dict.fromkeys(value))


def check_edit(fields) -> dict:
    if not isinstance(fields, dict) or not fields:
        raise HubError("missing_field", "edit needs fields")
    unknown = [k for k in fields if k not in EDITABLE]
    if unknown:
        raise HubError("bad_field", f"cannot edit {', '.join(unknown)}; editable: {', '.join(EDITABLE)}")
    clean = {}
    for key, value in fields.items():
        if key == "priority":
            clean[key] = check_priority(value)
        elif key == "labels":
            clean[key] = check_labels(value)
        elif key == "description":
            clean[key] = check_text(key, value) or ""
        else:
            clean[key] = check_text(key, value, required=key == "title")
    return clean
