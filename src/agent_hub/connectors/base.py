"""Shared connector loop: hold the wake stream, run the harness, stream the trace, post the result.

A connector implements `run_turn` and, if it can, `cancel` (design section 9).
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import AgentConfig
from ..core.delivery import render_prompt

log = logging.getLogger("agent_hub.connector")


@dataclass
class Wake:
    turn_id: str
    conversation: dict
    messages: list[dict]
    trigger_message_id: str
    hop: int
    prompt: str                    # the rendered harness prompt
    options: dict = field(default_factory=dict)   # per-turn options from the hub (permission_mode)


@dataclass
class TurnResult:
    text: str                      # the reply; NO_REPLY or empty stays silent
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None


@dataclass
class TraceSink:
    """Buffers trace events and sends them to the hub in small batches."""
    client: httpx.AsyncClient
    turn_id: str
    events: list[dict] = field(default_factory=list)

    async def emit(self, type: str, payload) -> None:
        self.events.append({"type": type, "payload": payload})
        if len(self.events) >= 10 or type in ("tool_call", "usage"):
            await self.flush()

    async def flush(self) -> None:
        events, self.events = self.events, []
        if events:
            try:
                await self.client.post(f"/agent/v1/turns/{self.turn_id}/trace", json={"events": events})
            except httpx.HTTPError as error:
                log.warning("trace not sent: %s", error)


def state_dir() -> Path:
    path = Path(os.environ.get("AGENT_HUB_STATE_DIR", "~/.local/state/agent-hub")).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def scrubbed_env() -> dict:
    """The environment for a harness subprocess, without hub tokens, passwords, or webhook secrets."""
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("HUB_") or "WEBHOOK" in k or k == "HERMES_API_KEY")}


class Connector:
    name = "base"

    def __init__(self, agent: AgentConfig, hub_url: str, token: str):
        self.agent, self.hub_url, self.token = agent, hub_url.rstrip("/"), token
        self.options = agent.extras()
        self.client = httpx.AsyncClient(
            base_url=self.hub_url, headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30, read=60))
        self.turns: dict[str, asyncio.Task] = {}

    async def run_turn(self, wake: Wake, trace: TraceSink) -> TurnResult:
        raise NotImplementedError

    async def cancel(self, turn_id: str) -> None:
        """Stop harness work for a turn. The base loop also cancels the turn's task."""

    # ------------------------------------------------------------------ state kept between turns
    def load_state(self) -> dict:
        path = state_dir() / f"{self.agent.id}.json"
        return json.loads(path.read_text()) if path.is_file() else {}

    def save_state(self, state: dict) -> None:
        path = state_dir() / f"{self.agent.id}.json"
        path.write_text(json.dumps(state))
        path.chmod(0o600)

    # ------------------------------------------------------------------ the loop
    async def serve(self) -> None:
        delay = 1.0
        while True:
            try:
                async with self.client.stream("GET", "/agent/v1/events") as response:
                    if response.status_code in (401, 409):
                        body = (await response.aread()).decode()
                        raise SystemExit(f"the hub refused the connection ({response.status_code}): {body}")
                    response.raise_for_status()
                    log.info("%s connected to %s", self.agent.id, self.hub_url)
                    delay = 1.0
                    if await self._read_events(response):
                        raise SystemExit("another connector connected as this agent; exiting")
            except (httpx.HTTPError, OSError) as error:
                log.warning("hub connection lost (%s); retrying in %.0fs", type(error).__name__, delay)
            for task in list(self.turns.values()):          # the hub failed these turns when the stream dropped
                task.cancel()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _read_events(self, response) -> bool:
        """Returns True when the hub says another connector took over."""
        event, data = None, []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and event:
                payload = json.loads("\n".join(data) or "{}")
                if event == "wake":
                    task = asyncio.create_task(self._turn(payload))
                    self.turns[payload["turn_id"]] = task
                    task.add_done_callback(lambda _t, id=payload["turn_id"]: self.turns.pop(id, None))
                elif event == "cancel":
                    await self.cancel(payload["turn_id"])
                    if payload["turn_id"] in self.turns:
                        self.turns[payload["turn_id"]].cancel()
                elif event == "replaced":
                    return True
                event, data = None, []
        return False

    async def _turn(self, payload: dict) -> None:
        wake = Wake(payload["turn_id"], payload["conversation"], payload["messages"],
                    payload["trigger_message_id"], payload["hop"], render_prompt(self.agent.id, payload),
                    payload.get("options") or {})
        trace = TraceSink(self.client, wake.turn_id)
        base = f"/agent/v1/turns/{wake.turn_id}"
        try:
            await self.client.post(f"{base}/start")
            log.info("turn %s in %s", wake.turn_id, wake.conversation["id"])
            result = await self.run_turn(wake, trace)
            await trace.flush()
            usage = {"cost_usd": result.cost_usd, "tokens_in": result.tokens_in, "tokens_out": result.tokens_out}
            response = await self.client.post(f"{base}/finish", json={"text": result.text, "usage": usage})
            if response.status_code >= 400:
                log.warning("hub did not accept the reply for %s: %s", wake.turn_id, response.text[:300])
        except asyncio.CancelledError:
            log.info("turn %s cancelled", wake.turn_id)
            raise
        except Exception as error:                            # the harness failed; tell the hub why
            log.exception("turn %s failed", wake.turn_id)
            await trace.flush()
            try:
                await self.client.post(f"{base}/fail", json={"error": f"{type(error).__name__}: {error}"})
            except httpx.HTTPError:
                pass
