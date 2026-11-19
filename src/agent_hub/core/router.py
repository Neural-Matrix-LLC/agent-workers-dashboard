"""Wake rules (design section 6): who is asked to respond to a message. Pure functions."""

import re
from dataclasses import dataclass, field

_MENTION = re.compile(r"(?<![\w@/.])@([A-Za-z][A-Za-z0-9_-]*)")


def parse_mentions(body: str, known: list[str]) -> list[str]:
    """Participant ids (and 'all') mentioned in the body, in order, without duplicates."""
    found = []
    for name in _MENTION.findall(body):
        name = name.lower()
        if (name == "all" or name in known) and name not in found:
            found.append(name)
    return found


@dataclass
class Plan:
    targets: list[str] = field(default_factory=list)
    fanout: str = "single"        # single | sequential | parallel


def decide(*, sender: str, sender_is_owner: bool, kind: str, mode: str, members: list[str],
           agents: list[str], mentions: list[str], fanout: str = "sequential", moderator: str | None = None,
           agent_all: bool = False, last_agent_sender: str | None = None) -> Plan:
    """`agents` are the agent members of the conversation in roster order."""
    others = [a for a in agents if a != sender]
    if kind == "dm":
        return Plan([m for m in members if m != sender and m in agents])

    if "all" in mentions:
        if sender_is_owner or agent_all:
            return Plan(others, fanout if len(others) > 1 else "single")
        mentions = [m for m in mentions if m != "all"]      # an agent's @all is context only

    named = [m for m in mentions if m in others]
    if named:
        return Plan(named)
    if mentions:
        return Plan()                 # mentioned only itself, the owner, or a denied @all

    if mode == "round_robin" and others:
        previous = sender if sender in agents else last_agent_sender
        if previous in agents:
            start = agents.index(previous) + 1
            order = agents[start:] + agents[:start]
        else:
            order = agents
        return Plan([next(a for a in order if a != sender)])
    if mode == "moderated" and moderator in others:
        return Plan([moderator])
    return Plan()


# Task events (design M4 section 7.2). `decide` is never called on a task message, so text inside a task
# title or result can never wake anyone by accident; only this table decides.
TASK_EVENTS = ("created", "claimed", "assigned", "unassigned", "released", "state", "reopened", "note",
               "edited", "artifact", "children_done")


def decide_task(*, event: str, actor: str, creator: str, assignee: str | None = None,
                previous_assignee: str | None = None, coordinator: str | None = None, to_state: str | None = None,
                note_mentions: list[str] = (), agents: list[str], wake_creator_on: list[str] = (),
                exclude: list[str] = ()) -> Plan:
    """Targets for one task event. `agents` are the agent members of the task's home conversation, so the
    owner and the hub are never woken. The actor (and anyone in `exclude`) is never woken."""
    targets: list[str | None] = []
    if event in ("created", "assigned", "reopened", "children_done"):
        targets = [assignee]
    elif event == "unassigned":
        targets = [previous_assignee]
    elif event == "released":
        targets = [creator] if "released" in wake_creator_on else []
    elif event == "state":
        if to_state in ("blocked", "done", "failed"):
            targets = [creator] if to_state in wake_creator_on else []
            targets.append(coordinator)
        elif to_state == "cancelled":
            targets = [assignee]
    elif event == "note":
        targets = list(note_mentions)
    found = []
    for target in targets:
        if target and target in agents and target != actor and target not in exclude and target not in found:
            found.append(target)
    return Plan(found)
