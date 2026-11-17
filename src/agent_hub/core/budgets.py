"""Loop and cost control: per-agent rate limit and budgets (design section 6)."""

import time
from collections import defaultdict, deque
from datetime import UTC, datetime


class RateLimiter:
    """At most `limit` turns per `window` seconds for each agent."""

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.times: dict[str, deque] = defaultdict(deque)

    def wait_time(self, agent_id: str) -> float:
        """0 when a turn may start now, otherwise the seconds until one may."""
        times, at = self.times[agent_id], self.clock()
        while times and at - times[0] >= self.window:
            times.popleft()
        if self.limit <= 0 or len(times) < self.limit:
            return 0.0
        return self.window - (at - times[0])

    def record(self, agent_id: str) -> None:
        self.times[agent_id].append(self.clock())


def day_start() -> str:
    """Budgets reset at 00:00 UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%dT00:00:00.000Z")


async def exhausted(repo, agent_id: str, budget: dict, conversation_id: str) -> str | None:
    """A sentence naming the first limit that is used up, or None when the agent may take a turn."""
    turns, usd = await repo.usage(agent_id, since=day_start())
    if budget.get("turns_per_day") is not None and turns >= budget["turns_per_day"]:
        return f"daily budget of {budget['turns_per_day']} turns is used up"
    if budget.get("usd_per_day") is not None and usd >= budget["usd_per_day"]:
        return f"daily budget of ${budget['usd_per_day']:.2f} is used up (${usd:.2f} spent)"
    if budget.get("turns_per_conversation") is not None or budget.get("usd_per_conversation") is not None:
        turns, usd = await repo.usage(agent_id, conversation_id=conversation_id)
        if budget.get("turns_per_conversation") is not None and turns >= budget["turns_per_conversation"]:
            return f"budget of {budget['turns_per_conversation']} turns in {conversation_id} is used up"
        if budget.get("usd_per_conversation") is not None and usd >= budget["usd_per_conversation"]:
            return f"budget of ${budget['usd_per_conversation']:.2f} in {conversation_id} is used up"
    return None
