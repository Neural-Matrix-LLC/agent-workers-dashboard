"""R3: the owner sees everything; an agent sees only the room and its own DMs.

The matrix: for every agent and every surface (REST, MCP tool, connector wake, webhook ping),
no payload may contain text or metadata from a conversation that agent cannot read.
"""

import json

import pytest

AGENTS = ["claude", "hermes", "grok"]


async def seed(env) -> dict[str, str]:
    """One message per conversation, each with a marker. Returns conversation id -> marker."""
    hub = env.hub
    claude, hermes, grok = [await env.agent(a) for a in AGENTS]
    markers = {}

    async def say(sender, conversation_id, marker, to=None):
        body = f"marker {marker}"
        if to:
            result = await hub.send_dm(sender, to, body)
            conversation_id = result["conversation_id"]
        else:
            result = await hub.post(sender, conversation_id, body)
        markers[conversation_id] = marker
        return result

    await say(hub.owner, "room:main", "MARK-ROOM")
    await say(hub.owner, "dm:claude:owner", "MARK-OWNER-CLAUDE")
    await say(hub.owner, "dm:hermes:owner", "MARK-OWNER-HERMES")
    await say(hub.owner, "dm:grok:owner", "MARK-OWNER-GROK")
    await say(claude, None, "MARK-CLAUDE-GROK", to="grok")
    await say(hermes, None, "MARK-HERMES-GROK", to="grok")
    await say(claude, None, "MARK-CLAUDE-HERMES", to="hermes")
    async with hub.lock:
        await hub._notice("room:main", "marker MARK-NOTICE (owner-only)")
    markers["notice"] = "MARK-NOTICE"
    await env.settle()
    return markers


def forbidden(agent: str, markers: dict[str, str]) -> list[str]:
    """Strings that must never reach `agent`: markers and ids of conversations it is not in."""
    out = [markers["notice"]]
    for conversation_id, marker in markers.items():
        if conversation_id.startswith("dm:") and agent not in conversation_id.split(":"):
            out += [marker, conversation_id]
    return out


def assert_clean(agent: str, payload, markers, where: str, asked: str = "") -> None:
    """`asked` is an id the caller sent itself; an error message may repeat it back."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    for needle in forbidden(agent, markers):
        if needle == asked:
            continue
        assert needle not in text, f"{where} leaked {needle!r} to {agent}"


@pytest.mark.parametrize("agent", AGENTS)
async def test_matrix_rest_and_mcp(hub_env, agent):
    async with hub_env() as env:
        markers = await seed(env)
        conversation_ids = [c for c in markers if c != "notice"]

        # REST mirrors
        for path in ("/whoami", "/participants", "/conversations"):
            response = await env.get(agent, path)
            assert response.status_code == 200
            assert_clean(agent, response.text, markers, f"GET {path}")
        for conversation_id in conversation_ids:
            mine = conversation_id.startswith("room:") or agent in conversation_id.split(":")
            response = await env.get(agent, f"/conversations/{conversation_id}/messages?after=0")
            assert response.status_code == (200 if mine else 404)
            assert_clean(agent, response.text, markers, f"GET messages {conversation_id}", conversation_id)
            if mine:
                assert markers[conversation_id] in response.text
            else:
                # Indistinguishable from a conversation that does not exist.
                missing = await env.get(agent, "/conversations/dm:nobody:nowhere/messages")
                assert response.json()["error"] == missing.json()["error"] == "unknown_conversation"
                denied = await env.post(agent, f"/conversations/{conversation_id}/messages", body="let me in")
                assert denied.status_code == 404

        # MCP tools
        for tool in ("whoami", "list_participants", "list_conversations"):
            is_error, result = await env.mcp(agent, tool)
            assert not is_error
            assert_clean(agent, result, markers, f"mcp {tool}")
        for conversation_id in conversation_ids:
            mine = conversation_id.startswith("room:") or agent in conversation_id.split(":")
            is_error, result = await env.mcp(agent, "read_messages", conversation_id=conversation_id, after="0")
            assert is_error is not mine
            assert_clean(agent, result, markers, f"mcp read_messages {conversation_id}", conversation_id)
            if not mine:
                is_error, result = await env.mcp(agent, "post_message", conversation_id=conversation_id, body="x")
                assert is_error and "unknown_conversation" in result

        listed = {c["id"] for c in (await env.get(agent, "/conversations")).json()}
        assert listed == {c for c in conversation_ids if c.startswith("room:") or agent in c.split(":")}


@pytest.mark.parametrize("agent", ["claude", "hermes"])
async def test_matrix_connector_wake(hub_env, agent):
    """A wake payload holds messages from the one conversation being woken for."""
    async with hub_env() as env:
        markers = await seed(env)
        me = await env.agent(agent)
        queue = await env.hub.connect(me)
        await env.owner_post("room:main", f"@{agent} your turn")
        seen = []
        while not queue.empty():
            event = queue.get_nowait()
            seen.append(event)
            assert_clean(agent, event, markers, "wake event")
            if event["event"] == "wake":
                conversation_id = event["data"]["conversation"]["id"]
                for other, marker in markers.items():
                    if other not in (conversation_id, "notice"):
                        assert marker not in json.dumps(event), "a wake mixed two conversations"
        assert any(e["event"] == "wake" for e in seen)


async def test_matrix_webhook_ping_has_no_message_text(hub_env):
    async with hub_env() as env:
        markers = await seed(env)
        await env.owner_post("room:main", "@grok MARK-PING-TEXT please look")
        await env.settle()
        assert env.pings, "grok was woken, so a ping must have been sent"
        for ping in env.pings:
            assert set(ping["body"]) == {"event", "agent", "conversation_id", "turn_id", "unread"}
            assert ping["body"]["agent"] == "grok"
            conversation_id = ping["body"]["conversation_id"]
            assert "grok" in conversation_id.split(":") or conversation_id == "room:main"
            assert_clean("grok", ping["body"], markers, "webhook ping")
            assert "MARK" not in json.dumps(ping["body"])
            assert ping["url"] == "https://grok.example/hook" and ping["auth"] == "Bearer sender-key"


async def test_owner_sees_everything_and_agent_dms_are_read_only(hub_env):
    async with hub_env() as env:
        markers = await seed(env)
        await env.login()
        activity = (await env.http.get("/api/activity")).text
        for marker in markers.values():
            assert marker in activity
        conversations = {c["id"]: c for c in (await env.http.get("/api/conversations")).json()}
        assert conversations["dm:claude:grok"]["observed"] is True
        assert conversations["dm:claude:owner"]["observed"] is False
        messages = await env.http.get("/api/conversations/dm:claude:grok/messages")
        assert "MARK-CLAUDE-GROK" in messages.text
        denied = await env.http.post("/api/conversations/dm:claude:grok/messages", json={"body": "hello"})
        assert denied.status_code == 403 and denied.json()["error"] == "read_only"


async def test_identity_comes_from_the_token(hub_env):
    async with hub_env() as env:
        response = await env.http.post("/agent/v1/conversations/room:main/messages", headers=env.auth("hermes"),
                                       json={"body": "hi", "sender": "owner", "from": "claude", "sender_id": "claude"})
        assert response.status_code == 200
        message = await env.hub.repo.raw_message(response.json()["id"])
        assert message.sender_id == "hermes"
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "tok-claude"}):
            assert (await env.http.get("/agent/v1/whoami", headers=headers)).status_code == 401
            assert (await env.http.post("/mcp", headers=headers, json={})).status_code == 401


async def test_reply_to_must_be_in_the_same_conversation(hub_env):
    async with hub_env() as env:
        claude, hermes = await env.agent("claude"), await env.agent("hermes")
        secret = await env.hub.send_dm(claude, "grok", "private")
        room = await env.owner_post("room:main", "public")
        # hermes cannot see the DM message; claude can, but it is in another conversation.
        for agent in ("hermes", "claude"):
            response = await env.post(agent, "/conversations/room:main/messages", body="re", reply_to=secret["id"])
            assert response.status_code == 400 and response.json()["error"] == "bad_reply_to"
        ok = await env.post("hermes", "/conversations/room:main/messages", body="re", reply_to=room["id"])
        assert ok.status_code == 200
        assert hermes.id == "hermes"


async def test_owner_api_and_traces_are_owner_only(hub_env):
    async with hub_env() as env:
        await env.owner_post("room:main", "@grok hi")
        turn = (await env.turns("grok"))[0]
        paths = ["/api/me", "/api/conversations", "/api/activity", "/api/agents", "/api/turns",
                 f"/api/turns/{turn.id}/trace", "/api/conversations/room:main/messages"]
        for path in paths:
            for headers in ({}, env.auth("claude"), env.auth("grok")):
                assert (await env.http.get(path, headers=headers)).status_code == 401, path
        assert (await env.http.post("/api/login", json={"password": "nope"})).status_code == 401
        await env.login()
        for path in paths:
            assert (await env.http.get(path)).status_code == 200, path


async def test_unread_counts_cover_only_own_conversations(hub_env):
    async with hub_env() as env:
        await seed(env)
        listed = {c["id"]: c for c in (await env.get("hermes", "/conversations")).json()}
        assert listed["room:main"]["unread"] == 1                    # the notice is not counted
        assert listed["dm:claude:hermes"]["unread"] == 1 and listed["dm:claude:hermes"]["pending_wake"]
        assert listed["dm:hermes:owner"]["unread"] == 1
