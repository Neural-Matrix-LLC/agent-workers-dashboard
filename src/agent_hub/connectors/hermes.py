"""Hermes Agent through its API server (design section 9.2).

Hermes keeps its own memory, skills, and session. The connector sends only the new prompt and
names the session with the X-Hermes-Session-Id header, so one Hermes session spans all turns.
For Hermes to call the hub, add the hub to ~/.hermes/config.yaml under mcp_servers.
"""

import os

import httpx

from .base import Connector, TraceSink, TurnResult, Wake


class HermesConnector(Connector):
    name = "hermes"

    async def run_turn(self, wake: Wake, trace: TraceSink) -> TurnResult:
        options = self.options
        key = os.environ.get(options.get("api_key_env", "HERMES_API_KEY"), "")
        headers = {"X-Hermes-Session-Id": options.get("session_id", f"agent-hub-{self.agent.id}")}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        timeout = httpx.Timeout(30, read=float(options.get("request_timeout", 1800)))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                options.get("api_base", "http://127.0.0.1:8642/v1").rstrip("/") + "/chat/completions",
                headers=headers,
                json={"model": options.get("model", "hermes-agent"),
                      "messages": [{"role": "user", "content": wake.prompt}]})
        if response.status_code >= 400:
            raise RuntimeError(f"Hermes API returned {response.status_code}: {response.text[:500]}")
        data = response.json()
        usage = data.get("usage") or {}
        await trace.emit("usage", usage)
        text = (data["choices"][0]["message"].get("content") or "").strip()
        return TurnResult(text or "NO_REPLY", None, usage.get("prompt_tokens"), usage.get("completion_tokens"))
