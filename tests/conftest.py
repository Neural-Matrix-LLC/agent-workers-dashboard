"""Shared test setup: a hub with three agents, an HTTP client, and a fake webhook receiver."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest

from agent_hub.config import Config
from agent_hub.core.hub import Hub
from agent_hub.server.app import create_app

TOKENS = {"claude": "tok-claude", "hermes": "tok-hermes", "grok": "tok-grok"}
PASSWORD = "owner-pw"


@dataclass
class Env:
    hub: Hub
    http: httpx.AsyncClient                    # talks to the hub app
    pings: list = field(default_factory=list)  # webhook pings the hub sent
    ping_status: list = field(default_factory=list)   # statuses to answer with, then 200

    def auth(self, agent: str) -> dict:
        return {"Authorization": f"Bearer {TOKENS[agent]}"}

    async def agent(self, id: str):
        return await self.hub.repo.participant(id)

    async def owner_post(self, conversation_id: str, body: str) -> dict:
        return await self.hub.post(self.hub.owner, conversation_id, body)

    async def get(self, agent: str, path: str) -> httpx.Response:
        return await self.http.get(f"/agent/v1{path}", headers=self.auth(agent))

    async def post(self, agent: str, path: str, **body) -> httpx.Response:
        return await self.http.post(f"/agent/v1{path}", headers=self.auth(agent), json=body)

    async def mcp(self, agent: str, tool: str, **arguments):
        """Call one MCP tool over /mcp. Returns (is_error, parsed result or error text)."""
        response = await self.http.post(
            "/mcp", headers={**self.auth(agent), "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        text = result["content"][0]["text"] if result.get("content") else ""
        if result.get("isError"):
            return True, text
        structured = result.get("structuredContent")
        if structured is not None:
            return False, structured.get("result", structured)
        return False, json.loads(text) if text else None

    async def login(self) -> None:
        response = await self.http.post("/api/login", json={"password": PASSWORD})
        assert response.status_code == 200

    async def turns(self, agent: str) -> list:
        return await self.hub.repo.turns("agent_id = ?", (agent,))

    async def settle(self) -> None:
        """Let background tasks (webhook pings) finish."""
        import asyncio
        for _ in range(20):
            await asyncio.sleep(0)
        while self.hub.tasks:
            await asyncio.gather(*list(self.hub.tasks), return_exceptions=True)


def make_config(tmp_path, **overrides) -> Config:
    data = {
        "hub": {"db": str(tmp_path / "hub.db")},
        "limits": {"webhook_backoff": 0.01},
        "agents": [
            {"id": "claude", "connector": "echo", "description": "Code."},
            {"id": "hermes", "connector": "echo", "description": "Research."},
            {"id": "grok", "connector": "none", "delivery": "webhook", "webhook_url_env": "GROK_WEBHOOK_URL",
             "webhook_key_env": "GROK_WEBHOOK_KEY", "description": "Critique."},
        ],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return Config.model_validate(data)


@pytest.fixture
def hub_env(tmp_path, monkeypatch):
    """Returns an async context manager: `async with hub_env(limits={...}) as env`."""
    for agent, token in TOKENS.items():
        monkeypatch.setenv(f"HUB_{agent.upper()}_TOKEN", token)
    monkeypatch.setenv("HUB_OWNER_PASSWORD", PASSWORD)
    monkeypatch.setenv("GROK_WEBHOOK_URL", "https://grok.example/hook")
    monkeypatch.setenv("GROK_WEBHOOK_KEY", "sender-key")
    monkeypatch.setenv("AGENT_HUB_WEB_DIST", str(tmp_path / "no-ui"))

    @asynccontextmanager
    async def start(**overrides):
        pings, statuses = [], []

        def webhook(request: httpx.Request) -> httpx.Response:
            pings.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                          "body": json.loads(request.content)})
            return httpx.Response(statuses.pop(0) if statuses else 200)

        app = create_app(make_config(tmp_path, **overrides),
                         http=httpx.AsyncClient(transport=httpx.MockTransport(webhook)))
        async with app.api.router.lifespan_context(app.api):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as http:
                yield Env(app.state.hub, http, pings, statuses)

    return start
