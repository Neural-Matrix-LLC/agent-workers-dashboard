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
