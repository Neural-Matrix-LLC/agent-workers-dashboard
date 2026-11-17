"""The Claude coding agent: owner-set permission modes, permission requests answered in the UI, and the
command the claude-code connector builds."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from agent_hub.config import AgentConfig
from agent_hub.connectors.base import Wake
from agent_hub.connectors.claude_code import PERMISSION_TOOL, ClaudeCodeConnector, safety_settings
from agent_hub.core.delivery import render_prompt


def agents(mode="plan", **claude):
    return [{"id": "claude", "connector": "claude-code", "permission_mode": mode, **claude},
            {"id": "hermes", "connector": "echo"}]


async def agent_row(env, agent_id):
    return next(a for a in (await env.http.get("/api/agents")).json() if a["id"] == agent_id)


async def wake_for(env, queue, body="do it"):
    await env.owner_post("dm:claude:owner", body)
    event = await asyncio.wait_for(queue.get(), 2)
    assert event["event"] == "wake"
    return event["data"]


# ------------------------------------------------------------------ modes
async def test_owner_sets_the_mode_and_it_reaches_the_wake(hub_env):
    async with hub_env(agents=agents()) as env:
        await env.login()
        row = await agent_row(env, "claude")
        assert row["mode"] == "plan" and row["modes"] == ["plan", "manual", "acceptEdits", "auto"]
        assert (await agent_row(env, "hermes"))["modes"] == []

        assert (await env.http.put("/api/agents/claude/mode", json={"mode": "manual"})).status_code == 200
        assert (await agent_row(env, "claude"))["mode"] == "manual"
        queue = await env.hub.connect(await env.agent("claude"))
        wake = await wake_for(env, queue)
        assert wake["options"] == {"permission_mode": "manual"}
        assert render_prompt("claude", wake).startswith("Mode: MANUAL.")


async def test_mode_api_refuses_bad_values_and_other_agents(hub_env):
    async with hub_env(agents=agents()) as env:
        await env.login()
        assert (await env.http.put("/api/agents/claude/mode", json={"mode": "bypassPermissions"})).status_code == 400
        assert (await env.http.put("/api/agents/hermes/mode", json={"mode": "plan"})).status_code == 400
        await env.http.post("/api/logout")
        # An agent token does not open the owner API.
        response = await env.http.put("/api/agents/claude/mode", json={"mode": "auto"}, headers=env.auth("claude"))
        assert response.status_code == 401


def test_config_refuses_bypass_permissions():
    with pytest.raises(ValidationError):
        AgentConfig(id="claude", connector="claude-code", permission_mode="bypassPermissions")


async def test_owner_mode_survives_restart_until_agents_yaml_changes(hub_env):
    async with hub_env(agents=agents("plan")) as env:
        await env.login()
        await env.http.put("/api/agents/claude/mode", json={"mode": "auto"})
    async with hub_env(agents=agents("plan")) as env:
        assert env.hub._mode(await env.agent("claude")) == "auto"
    async with hub_env(agents=agents("acceptEdits")) as env:
        assert env.hub._mode(await env.agent("claude")) == "acceptEdits"


# ------------------------------------------------------------------ permission requests
async def ask(env, turn_id, tool="Bash", input=None, agent="claude"):
    return await env.http.post(f"/agent/v1/turns/{turn_id}/permission", headers=env.auth(agent),
                               json={"tool_name": tool, "input": input or {"command": "pytest -q"}})


async def pending(env, agent_id="claude"):
    for _ in range(100):
        rows = (await agent_row(env, agent_id))["pending_permissions"]
        if rows:
            return rows
        await asyncio.sleep(0.01)
    raise AssertionError("no pending permission request")


async def test_permission_waits_for_the_owner(hub_env):
    async with hub_env(agents=agents()) as env:
        await env.login()
        queue = await env.hub.connect(await env.agent("claude"))
        wake = await wake_for(env, queue)

        request = asyncio.create_task(ask(env, wake["turn_id"], input={"command": "pytest -q"}))
        rows = await pending(env)
        assert rows[0]["tool"] == "Bash" and rows[0]["summary"] == "pytest -q"
        assert not request.done()
        assert (await env.http.post(f"/api/permissions/{rows[0]['id']}/allow")).status_code == 200
        answer = (await request).json()
        assert answer == {"behavior": "allow"}
        assert (await agent_row(env, "claude"))["pending_permissions"] == []

        request = asyncio.create_task(ask(env, wake["turn_id"], tool="Edit", input={"file_path": "/x.py"}))
        rows = await pending(env)
        await env.http.post(f"/api/permissions/{rows[0]['id']}/deny")
        answer = (await request).json()
        assert answer["behavior"] == "deny" and "owner said no" in answer["message"]


async def test_permission_times_out_and_stop_denies(hub_env):
    async with hub_env(agents=agents(permission_timeout="0.2s")) as env:
        await env.login()
        queue = await env.hub.connect(await env.agent("claude"))
        wake = await wake_for(env, queue)
        answer = (await ask(env, wake["turn_id"])).json()
        assert answer["behavior"] == "deny" and "did not answer" in answer["message"]

    async with hub_env(agents=agents()) as env:
        await env.login()
        queue = await env.hub.connect(await env.agent("claude"))
        wake = await wake_for(env, queue)
        request = asyncio.create_task(ask(env, wake["turn_id"]))
        await pending(env)
        await env.http.post("/api/agents/claude/stop")
        answer = (await request).json()
        assert answer["behavior"] == "deny" and "turn ended" in answer["message"]


async def test_only_the_turns_own_agent_can_ask(hub_env):
    async with hub_env(agents=agents()) as env:
        queue = await env.hub.connect(await env.agent("claude"))
        wake = await wake_for(env, queue)
        assert (await ask(env, wake["turn_id"], agent="hermes")).status_code == 409
        assert (await ask(env, "no-such-turn")).status_code == 409
        response = await env.http.post(f"/agent/v1/turns/{wake['turn_id']}/permission",
                                       json={"tool_name": "Bash", "input": {}})
        assert response.status_code == 401
        # Deciding is owner-only.
        assert (await env.http.post("/api/permissions/whatever/allow")).status_code == 401


# ------------------------------------------------------------------ the connector's command
def wake(mode):
    return Wake("T1", {"id": "dm:claude:owner", "kind": "dm", "members": []}, [], "M1", 0, "prompt",
                {"permission_mode": mode} if mode else {})


def test_command_per_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_STATE_DIR", str(tmp_path / "state"))
    config = AgentConfig(id="claude", connector="claude-code", workdir=str(tmp_path), command="true",
                         sandbox=False, permission_mode="manual")
    connector = ClaudeCodeConnector(config, "http://hub:8700", "secret-token")
    for mode in ("plan", "manual", "acceptEdits", "auto"):
        command = connector._command(None, wake(mode))
        assert command[command.index("--permission-mode") + 1] == mode
        assert command[command.index("--permission-prompt-tool") + 1] == PERMISSION_TOOL
        assert "--plugin-dir" in command and "--settings" in command
        assert "Edit" not in command[command.index("--allowedTools") + 1]
    command = connector._command("sess", wake(None))                 # no owner choice: agents.yaml
    assert command[command.index("--permission-mode") + 1] == "manual"
    assert command[-2:] == ["--resume", "sess"]

    mcp = json.loads(open(command[command.index("--mcp-config") + 1]).read())["mcpServers"]
    assert mcp["hub_permission"]["env"]["AGENT_HUB_TURN"] == "T1"
    assert mcp["hub_permission"]["args"] == ["-m", "agent_hub.permission_mcp"]


def test_safety_settings():
    settings = safety_settings({"deny": ["Bash(rm -rf *)"]}, ["/srv/hub/.env"])
    deny, ask = settings["permissions"]["deny"], settings["permissions"]["ask"]
    assert "Read(**/.env)" in deny and "Bash(git push --force *)" in deny and "Bash(git push * main)" in deny
    assert "Read(//srv/hub/.env)" in deny and "Bash(rm -rf *)" in deny
    assert ask == ["Bash(git push *)"]
    assert settings["sandbox"]["enabled"] and "/srv/hub/.env" in settings["sandbox"]["filesystem"]["denyRead"]
    assert "sandbox" not in safety_settings({"sandbox": False}, [])


def test_permission_summary_shows_the_risky_end_of_a_long_command():
    from agent_hub.core.delivery import permission_summary
    command = "git add x && git commit -m '" + "long message " * 80 + "' && git push -u origin claude/x"
    summary = permission_summary("Bash", {"command": command})
    assert summary.startswith("[includes: git push, git commit]")
    assert summary.endswith("git push -u origin claude/x") and len(summary) < 600
    assert permission_summary("Write", {"file_path": "/p/hello.py", "content": "x"}) == "/p/hello.py"
    assert permission_summary("Bash", {"command": "ls -la"}) == "ls -la"
