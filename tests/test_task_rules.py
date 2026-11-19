"""The task state machine, field limits, and task wake rules (design M4 sections 5.2 and 7.2). Pure: S-01 to
S-04, S-08, S-09. S-05 to S-07 need a hub and are in test_tasks_api.py."""

import pytest

from agent_hub.core import task_rules
from agent_hub.core.models import HubError
from agent_hub.core.router import decide_task

ROLES = ["owner", "creator", "assignee", "coordinator"]
STATES = ["open", "assigned", "in_progress", "blocked", "done", "failed", "cancelled"]

# (action, from state) -> roles allowed besides the owner, from the table in design section 5.2.
TABLE = {
    ("assign", "open"): {"creator", "coordinator"},
    ("assign", "assigned"): {"creator", "coordinator", "assignee"},
    ("unassign", "assigned"): {"creator", "coordinator"},
    ("release", "assigned"): {"assignee"},
    ("release", "in_progress"): {"assignee"},
    ("start", "assigned"): {"assignee"},
    ("block", "in_progress"): {"assignee"},
    ("unblock", "blocked"): {"assignee"},
    **{("finish", s): {"assignee"} for s in ("assigned", "in_progress", "blocked")},
    **{("fail", s): {"assignee"} for s in ("assigned", "in_progress", "blocked")},
    **{("cancel", s): {"creator", "coordinator"} for s in ("open", "assigned", "in_progress", "blocked")},
    ("reopen", "done"): {"creator", "coordinator"},
    ("reopen", "failed"): {"creator", "coordinator"},
    **{("note", s): {"creator", "assignee", "coordinator"} for s in STATES},
    **{("edit", s): {"creator", "coordinator"} for s in ("open", "assigned", "in_progress", "blocked")},
}
TARGET = {"assign": "assigned", "unassign": "open", "release": "open", "start": "in_progress", "block": "blocked",
          "unblock": "in_progress", "finish": "done", "fail": "failed", "cancel": "cancelled", "reopen": "assigned"}


def test_s01_every_allowed_transition_succeeds_for_each_permitted_role():
    for (action, state), roles in TABLE.items():
        for role in roles | {"owner"}:
            task_rules.check(action, state, {role}, "x")
        assert task_rules.target_state(action, state) == TARGET.get(action, state)


@pytest.mark.parametrize("action", task_rules.ACTIONS)
def test_s02_everything_else_is_refused_with_the_allowed_actions(action):
    for state in STATES:
        for role in ["creator", "assignee", "coordinator", "member"]:
            if role in TABLE.get((action, state), set()):
                continue
            with pytest.raises(HubError) as error:
                task_rules.check(action, state, {role} if role != "member" else set(), "hermes")
            assert error.value.code in ("bad_transition", "not_allowed")
            assert "hermes may:" in error.value.message
            if (action, state) not in TABLE:
                assert error.value.code == "bad_transition"


def test_s02_unknown_action_and_listing():
    with pytest.raises(HubError, match="action must be one of") as error:
        task_rules.check("merge", "open", {"owner"}, "owner")
    assert error.value.code == "bad_action"
    assert task_rules.allowed_actions("in_progress", {"assignee"}) == ["release", "block", "finish", "fail", "note"]
    assert task_rules.allowed_actions("assigned", {"owner"}, owner_moderation_only=True) == \
        ["assign", "unassign", "cancel"]


@pytest.mark.parametrize("action,values", [
    ("finish", {}), ("fail", {"result": "  "}), ("block", {}), ("reopen", {}), ("note", {"note": ""}),
    ("assign", {}), ("edit", {"fields": {}}),
])
def test_s03_required_fields(action, values):
    with pytest.raises(HubError) as error:
        task_rules.require(action, values)
    assert error.value.code == "missing_field"


def test_s04_field_limits_and_labels():
    assert task_rules.check_text("title", "x" * 200, required=True)
    for name, limit in task_rules.LIMITS.items():
        with pytest.raises(HubError, match="longer than"):
            task_rules.check_text(name, "x" * (limit + 1))
    with pytest.raises(HubError) as error:
        task_rules.check_text("title", "   ", required=True)
    assert error.value.code == "missing_field"
    assert task_rules.check_labels(["tests", "ui-2", "tests"]) == ["tests", "ui-2"]
    for bad in (["Tests"], ["-x"], ["a" * 33], ["ok"] * 11, "tests"):
        with pytest.raises(HubError):
            task_rules.check_labels(bad)
    with pytest.raises(HubError):
        task_rules.check_priority("urgent")
    assert task_rules.check_edit({"priority": "high", "labels": ["x"]}) == {"priority": "high", "labels": ["x"]}
    with pytest.raises(HubError, match="cannot edit state"):
        task_rules.check_edit({"state": "done"})


def test_s08_cancel_is_final():
    for role in ROLES:
        with pytest.raises(HubError) as error:
            task_rules.check("reopen", "cancelled", {role}, "x")
        assert error.value.code == "bad_transition"


def test_roles():
    assert task_rules.roles("claude", is_owner=False, creator_id="claude", assignee_id="claude",
                            coordinator_id=None) == {"creator", "assignee"}
    assert task_rules.roles("owner", is_owner=True, creator_id="claude", assignee_id=None,
                            coordinator_id="owner") == {"owner", "coordinator"}


# ------------------------------------------------------------------ S-09: decide_task, one case per row of 7.2
AGENTS = ["claude", "hermes", "grok"]


def targets(event, **kwargs):
    base = {"actor": "claude", "creator": "claude", "agents": AGENTS,
            "wake_creator_on": ["blocked", "done", "failed"]}
    return decide_task(event=event, **{**base, **kwargs}).targets


def test_s09_task_wake_rules():
    assert targets("created", assignee="hermes") == ["hermes"]
    assert targets("created") == []
    assert targets("claimed", actor="hermes", assignee="hermes") == []
    assert targets("assigned", assignee="grok") == ["grok"]
    assert targets("unassigned", previous_assignee="hermes") == ["hermes"]
    assert targets("released", actor="hermes") == []
    assert targets("released", actor="hermes", wake_creator_on=["released"]) == ["claude"]
    for state in ("blocked", "done", "failed"):
        assert targets("state", actor="hermes", to_state=state) == ["claude"]
        assert targets("state", actor="hermes", to_state=state, coordinator="grok") == ["claude", "grok"]
    assert targets("state", actor="hermes", to_state="done", wake_creator_on=["failed"]) == []
    assert targets("state", actor="hermes", to_state="in_progress") == []
    assert targets("state", to_state="cancelled", assignee="hermes") == ["hermes"]
    assert targets("reopened", assignee="hermes") == ["hermes"]
    assert targets("note", note_mentions=["grok", "hermes", "owner"]) == ["grok", "hermes"]
    assert targets("edited", assignee="hermes") == targets("artifact", assignee="hermes") == []
    assert targets("children_done", actor="hub", assignee="claude") == ["claude"]


def test_s09_never_the_actor_the_owner_the_hub_or_a_non_member():
    assert targets("created", assignee="claude") == []                     # self-assigned: no self-wake
    assert targets("created", assignee="owner") == []                      # the owner is never woken
    assert targets("assigned", assignee="outsider") == []                  # not a member of the home
    assert targets("children_done", actor="hub", assignee="claude", exclude=["claude"]) == []
    assert targets("state", actor="hermes", to_state="done", coordinator="claude") == ["claude"]   # no duplicate
