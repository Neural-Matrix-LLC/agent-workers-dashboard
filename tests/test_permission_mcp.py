"""The permission helper Claude Code calls (`python -m agent_hub.permission_mcp`): its answers, and the shape of
its MCP tool when run as a real stdio server."""

import asyncio
import json
import os
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_claude_worker import agents, pending, wake_for

from agent_hub import permission_mcp


def point_at_hub(monkeypatch, env, turn_id: str, token="tok-claude"):
    real = httpx.AsyncClient

    def make(*args, **kwargs):
        kwargs.setdefault("transport", httpx.ASGITransport(app=env.app))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", make)
    monkeypatch.setenv("AGENT_HUB_URL", "http://hub/")
    monkeypatch.setenv("AGENT_HUB_TOKEN", token)
    monkeypatch.setenv("AGENT_HUB_TURN", turn_id)


async def test_approve_returns_the_owners_answer(hub_env, monkeypatch):
    async with hub_env(agents=agents()) as env:
        await env.login()
        queue = await env.hub.connect(await env.agent("claude"))
        wake = await wake_for(env, queue)
        point_at_hub(monkeypatch, env, wake["turn_id"])

        asking = asyncio.create_task(permission_mcp.approve("Bash", {"command": "git push -u origin claude/x"}, "tu1"))
        rows = await pending(env)
        assert rows[0]["summary"].startswith("[includes: git push]")
        await env.http.post(f"/api/permissions/{rows[0]['id']}/allow")
        assert json.loads(await asking) == {"behavior": "allow"}

        asking = asyncio.create_task(permission_mcp.approve("Write", {"file_path": "/p/x.py", "content": ""}))
        rows = await pending(env)
        await env.http.post(f"/api/permissions/{rows[0]['id']}/deny")
        answer = json.loads(await asking)
        assert answer["behavior"] == "deny" and "owner said no" in answer["message"]


async def test_approve_denies_when_the_hub_refuses(hub_env, monkeypatch):
    async with hub_env(agents=agents()) as env:
        point_at_hub(monkeypatch, env, "no-such-turn")
        answer = json.loads(await permission_mcp.approve("Bash", {"command": "ls"}))
        assert answer["behavior"] == "deny" and "Do not retry" in answer["message"]

        point_at_hub(monkeypatch, env, "no-such-turn", token="wrong")
        answer = json.loads(await permission_mcp.approve("Bash", {"command": "ls"}))
        assert answer["behavior"] == "deny" and "token" in answer["message"]


async def test_approve_denies_when_the_hub_is_unreachable(monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(down), **kw))
    monkeypatch.setenv("AGENT_HUB_URL", "http://hub")
    answer = json.loads(await permission_mcp.approve("Bash", {"command": "ls"}))
    assert answer == {"behavior": "deny",
                      "message": "Denied: the hub could not be reached (ConnectError). Do not retry this action."}


async def test_stdio_server_gives_claude_code_one_text_block():
    """Run the helper the way Claude Code does. A closed port makes it deny without a hub."""
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "agent_hub.permission_mcp"],
        env={**os.environ, "AGENT_HUB_URL": "http://127.0.0.1:9", "AGENT_HUB_TOKEN": "t", "AGENT_HUB_TURN": "T1"})
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        assert [t.name for t in tools] == ["approve"]
        assert set(tools[0].inputSchema["required"]) == {"tool_name", "input"}
        result = await asyncio.wait_for(
            session.call_tool("approve", {"tool_name": "Bash", "input": {"command": "ls"}}), 30)
    assert not result.isError and result.structuredContent is None
    assert len(result.content) == 1 and result.content[0].type == "text"
    assert json.loads(result.content[0].text)["behavior"] == "deny"
