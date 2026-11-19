"""Task and artifact leak tests (design M4 section 15.3: V-01 to V-14; V-15 is in test_tasks_api.py).

`seed_tasks` puts one task in every conversation, with a marker naming that conversation in its title,
description, acceptance, note, result, and an attached file, plus a private subtask in dm:claude:grok under a
room task. A participant must never see a marker of a conversation it is not in.
"""

import json

from taskkit import act, ok

AGENTS = ("claude", "hermes", "grok")
FAST = {"turns_per_minute": 1000}         # seeding makes many self-started task actions
CONVERSATIONS = {
    "room:main": {"claude", "hermes", "grok"},
    "dm:claude:hermes": {"claude", "hermes"},
    "dm:claude:grok": {"claude", "grok"},
    "dm:grok:hermes": {"grok", "hermes"},
}


def marker(conversation: str) -> str:
    return "MARK_" + conversation.replace(":", "_").upper()


def forbidden(agent: str) -> list[str]:
    return [marker(c) for c, members in CONVERSATIONS.items() if agent not in members] + ["MARK_PRIVATE_CHILD"]


def assert_clean(agent: str, payload, where: str) -> None:
    text = json.dumps(payload)
    for mark in forbidden(agent):
        if mark == "MARK_PRIVATE_CHILD" and agent in CONVERSATIONS["dm:claude:grok"]:
            continue
        assert mark not in text, f"{agent} saw {mark} in {where}"
    for conversation, members in CONVERSATIONS.items():
        if agent not in members and conversation != "room:main":
            assert f'"{conversation}' not in text and f"{conversation}#" not in text, \
                f"{agent} saw the id {conversation} in {where}"


async def seed_tasks(env) -> dict:
    """Returns {conversation: task id} plus 'parent' and 'child' for the private subtask."""
    await ok(env, "claude", "send_dm", to="hermes", body="hello")
    await ok(env, "claude", "send_dm", to="grok", body="hello")
    await ok(env, "grok", "send_dm", to="hermes", body="hello")
    ids = {}
    for conversation, members in CONVERSATIONS.items():
        creator, assignee = sorted(members)[:2] if len(members) > 1 else (sorted(members)[0],) * 2
        mark = marker(conversation)
        view = (await ok(env, creator, "create_task", conversation_id=conversation, title=f"{mark} title",
                         description=f"{mark} description", acceptance=f"{mark} acceptance",
                         assignee=assignee))["task"]
        await act(env, creator, view["id"], "note", note=f"{mark} note")
        await ok(env, assignee, "attach_artifact", task_id=view["id"], name=f"{mark}.txt", content=f"{mark} file")
        await act(env, assignee, view["id"], "finish", result=f"{mark} result")
        ids[conversation] = view["id"]
    parent = (await ok(env, "claude", "create_task", conversation_id="room:main", title="MARK_ROOM_MAIN parent",
                       assignee="claude"))["task"]
    child = (await ok(env, "claude", "create_task", conversation_id="dm:claude:grok",
                      title="MARK_PRIVATE_CHILD review the cap", parent_id=parent["id"], assignee="grok"))["task"]
    await ok(env, "grok", "attach_artifact", task_id=child["id"], name="private.txt", content="MARK_PRIVATE_CHILD")
    await act(env, "grok", child["id"], "finish", result="MARK_PRIVATE_CHILD result")
    ids.update(parent=parent["id"], child=child["id"])
    return ids


def readable(agent: str) -> set[str]:
    return {c for c, members in CONVERSATIONS.items() if agent in members}


async def test_v01_list_tasks_returns_exactly_the_readable_tasks(hub_env):
    async with hub_env(limits=FAST) as env:
        ids = await seed_tasks(env)
        for agent in AGENTS:
            expected = {ids[c] for c in readable(agent)} | {ids["parent"]}
            if agent in CONVERSATIONS["dm:claude:grok"]:
                expected.add(ids["child"])
            listed = await ok(env, agent, "list_tasks", include_closed=True, limit=200)
            assert {t["id"] for t in listed["tasks"]} == expected, agent
            assert_clean(agent, listed, "list_tasks (MCP)")
            rest = (await env.get(agent, "/tasks?include_closed=true&limit=200")).json()
            assert {t["id"] for t in rest["tasks"]} == expected
            assert_clean(agent, rest, "GET /tasks")
            for task_id in expected:
                detail = await ok(env, agent, "get_task", task_id=task_id)
                assert_clean(agent, detail, f"get_task {task_id}")


async def test_v02_hidden_and_missing_tasks_give_the_same_error(hub_env):
    async with hub_env(limits=FAST) as env:
        ids = await seed_tasks(env)
        hidden = await env.mcp("hermes", "get_task", task_id=ids["dm:claude:grok"])
        missing = await env.mcp("hermes", "get_task", task_id="01ZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert hidden[0] and missing[0]
        assert hidden[1].replace(ids["dm:claude:grok"], "ID") == missing[1].replace("01ZZZZZZZZZZZZZZZZZZZZZZZZ", "ID")
        a = (await env.get("hermes", f"/tasks/{ids['dm:claude:grok']}")).json()
        b = (await env.get("hermes", "/tasks/01ZZZZZZZZZZZZZZZZZZZZZZZZ")).json()
        assert a["error"] == b["error"] == "unknown_task"


async def test_v03_v04_parent_and_children_are_filtered(hub_env):
    async with hub_env(limits=FAST) as env:
        ids = await seed_tasks(env)
        for agent, sees_child in (("claude", True), ("grok", True), ("hermes", False)):
            parent = await ok(env, agent, "get_task", task_id=ids["parent"])
            assert parent["children"]["total"] == (1 if sees_child else 0)
            assert len(parent["children_list"]) == (1 if sees_child else 0)
        child = await ok(env, "grok", "get_task", task_id=ids["child"])
        assert child["parent_id"] == ids["parent"]                            # grok is in the room too
    async with hub_env(agents=[{"id": "claude", "connector": "echo"}, {"id": "hermes", "connector": "echo"},
                               {"id": "grok", "connector": "none", "delivery": "poll"}],
                       rooms=[{"id": "room:main", "members": ["claude", "hermes"]}], limits=FAST) as env:
        await ok(env, "claude", "send_dm", to="grok", body="hi")
        parent = (await ok(env, "claude", "create_task", conversation_id="room:main", title="p",
                           assignee="claude"))["task"]
        child = (await ok(env, "claude", "create_task", conversation_id="dm:claude:grok", title="c",
                          parent_id=parent["id"], assignee="grok"))["task"]
        assert (await ok(env, "grok", "get_task", task_id=child["id"]))["parent_id"] is None   # V-03


async def test_v05_task_numbers_are_per_conversation(hub_env):
    async with hub_env(limits=FAST) as env:
        await seed_tasks(env)
        for agent in AGENTS:
            listed = await ok(env, agent, "list_tasks", include_closed=True, limit=200)
            for conversation in readable(agent):
                numbers = sorted(t["number"] for t in listed["tasks"] if t["conversation_id"] == conversation)
                assert numbers == list(range(1, len(numbers) + 1))


async def test_v06_v07_messages_and_private_children_done(hub_env):
    async with hub_env(limits=FAST) as env:
        ids = await seed_tasks(env)
        for agent in AGENTS:
            for conversation in readable(agent):
                messages = (await env.get(agent, f"/conversations/{conversation}/messages?after=0&limit=500")).json()
                assert_clean(agent, messages, f"read_messages {conversation}")
        room = [m["body"] for m in (await env.get("hermes", "/conversations/room:main/messages?after=0&limit=500"))
                .json()]
        assert not any("subtasks" in b for b in room)                          # private case: nothing in the room
        agent_events = (await ok(env, "claude", "get_task", task_id=ids["parent"]))["events"]
        assert "children_done" not in [e["type"] for e in agent_events]
        await env.login()
        owner_events = (await env.http.get(f"/api/tasks/{ids['parent']}")).json()["events"]
        done = [e for e in owner_events if e["type"] == "children_done"]
        assert len(done) == 1 and done[0]["data"]["private"] is True


async def test_v08_hidden_and_deleted_artifacts(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        ids = await seed_tasks(env)
        hidden = (await env.http.get(f"/api/tasks/{ids['dm:claude:grok']}")).json()["artifacts"][0]["id"]
        for path in (f"/artifacts/{hidden}", f"/artifacts/{hidden}/content"):
            assert (await env.get("hermes", path)).json()["error"] == "unknown_artifact"
        error, text = await env.mcp("hermes", "read_artifact", artifact_id=hidden)
        assert error and "unknown_artifact" in text
        visible = (await ok(env, "hermes", "get_task", task_id=ids["room:main"]))["artifacts"][0]["id"]
        await env.http.delete(f"/api/artifacts/{visible}")
        assert (await env.get("hermes", f"/artifacts/{visible}/content")).json()["error"] == "unknown_artifact"
        assert (await ok(env, "hermes", "get_task", task_id=ids["room:main"]))["artifact_count"] == 0


async def test_v09_v10_wakes_for_tasks(hub_env):
    async with hub_env(limits=FAST) as env:
        hermes = await env.hub.connect(await env.agent("hermes"))
        await ok(env, "claude", "send_dm", to="hermes", body="hi")
        first = hermes.get_nowait()["data"]
        await env.post("hermes", f"/turns/{first['turn_id']}/finish", text="NO_REPLY")
        await ok(env, "claude", "send_dm", to="grok", body="hi")
        await ok(env, "grok", "post_message", conversation_id="dm:claude:grok", body="NO_REPLY")   # closes its turn
        await env.settle()
        env.pings.clear()
        view = (await ok(env, "claude", "create_task", conversation_id="dm:claude:hermes", title="MARK_X",
                         assignee="hermes"))["task"]
        wake = hermes.get_nowait()["data"]
        assert wake["task"]["conversation_id"] == wake["conversation"]["id"] == "dm:claude:hermes"
        assert wake["task"]["id"] == view["id"]
        await ok(env, "claude", "create_task", conversation_id="dm:claude:grok", title="MARK_SECRET_TITLE",
                 assignee="grok")
        await env.settle()
        [ping] = env.pings
        assert set(ping["body"]) == {"event", "agent", "conversation_id", "turn_id", "unread"}
        assert "MARK" not in json.dumps(ping["body"])


async def test_v11_open_tasks_are_only_your_own(hub_env):
    async with hub_env(limits=FAST) as env:
        await ok(env, "claude", "send_dm", to="grok", body="hi")
        await ok(env, "claude", "create_task", conversation_id="room:main", title="for hermes", assignee="hermes")
        await ok(env, "claude", "create_task", conversation_id="dm:claude:grok", title="for grok", assignee="grok")
        me = (await env.get("hermes", "/whoami")).json()
        assert [t["title"] for t in me["open_tasks"]] == ["for hermes"]
        rows = (await env.get("hermes", "/conversations")).json()
        assert sum(r["open_tasks"] for r in rows) == 1
        assert "dm:claude:grok" not in {r["id"] for r in rows}


async def test_v12_write_errors_reveal_nothing(hub_env):
    async with hub_env(limits=FAST) as env:
        ids = await seed_tasks(env)
        hidden = ids["dm:claude:grok"]
        for tool, args in (("update_task", {"task_id": hidden, "action": "note", "note": "x"}),
                           ("claim_task", {"task_id": hidden}),
                           ("attach_artifact", {"task_id": hidden, "name": "x.txt", "content": "x"})):
            error, text = await env.mcp("hermes", tool, **args)
            assert error and "unknown_task" in text
            assert "MARK" not in text and "dm:claude:grok" not in text and "grok" not in text.replace(hidden, "")


async def test_v13_assignee_must_be_a_member(hub_env):
    async with hub_env(limits=FAST) as env:
        await ok(env, "claude", "send_dm", to="hermes", body="hi")
        error, text = await env.mcp("claude", "create_task", conversation_id="dm:claude:hermes", title="t",
                                    assignee="grok")
        assert error and "bad_assignee" in text


async def test_v14_owner_task_routes_need_the_owner(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        view = (await ok(env, "claude", "create_task", conversation_id="room:main", title="t",
                         assignee="hermes"))["task"]
        artifact = (await ok(env, "hermes", "attach_artifact", task_id=view["id"], name="r.txt",
                             content="r"))["artifact"]
        await env.http.post("/api/logout")
        paths = [("GET", "/api/tasks"), ("GET", f"/api/tasks/{view['id']}"), ("POST", "/api/tasks"),
                 ("PATCH", f"/api/tasks/{view['id']}"), ("GET", f"/api/artifacts/{artifact['id']}"),
                 ("GET", f"/api/artifacts/{artifact['id']}/content"), ("DELETE", f"/api/artifacts/{artifact['id']}")]
        for method, path in paths:
            for headers in ({}, env.auth("claude")):
                response = await env.http.request(method, path, headers=headers, json={})
                assert response.status_code == 401, (method, path, headers)
