"""Task API contract (design M4 section 15.7: C-01 to C-06) and the rules that need a hub (S-05 to S-07)."""

import asyncio
import json

import httpx
from conftest import TOKENS
from taskkit import act, bodies, fails, ok, task

from agent_hub.config import AgentConfig
from agent_hub.connectors.openai_compat import OpenAICompatConnector
from agent_hub.core.delivery import render_prompt

VIEW_KEYS = {"id", "ref", "number", "conversation_id", "parent_id", "depth", "title", "description", "acceptance",
             "priority", "labels", "creator", "assignee", "state", "result", "blocked_reason", "children",
             "artifact_count", "reassignments", "version", "created_at", "updated_at", "closed_at"}
TASK_TOOLS = {"create_task", "claim_task", "update_task", "list_tasks", "get_task", "attach_artifact",
              "read_artifact"}


# ------------------------------------------------------------------ C-01: tools and REST mirrors
async def test_c01_tool_shapes(hub_env):
    async with hub_env() as env:
        created = await ok(env, "claude", "create_task", conversation_id="room:main", title="Write tests",
                           description="Cover the cap.", acceptance="pytest passes", assignee="hermes",
                           priority="high", labels=["tests"])
        view = created["task"]
        assert set(view) == VIEW_KEYS and created["message_id"]
        assert (view["ref"], view["number"], view["state"], view["creator"], view["assignee"], view["version"]) == \
            ("room:main#1", 1, "assigned", "claude", "hermes", 1)
        assert view["children"] == {"total": 0, "open": 0, "done": 0, "failed": 0, "cancelled": 0}

        detail = await ok(env, "hermes", "get_task", task_id=view["id"])
        assert set(detail) == VIEW_KEYS | {"events", "children_list", "artifacts"}
        assert [e["type"] for e in detail["events"]] == ["created"]
        assert set(detail["events"][0]) == {"id", "type", "actor", "from_state", "to_state", "note", "data",
                                            "message_id", "hop", "at"}

        listed = await ok(env, "grok", "list_tasks")
        assert [t["id"] for t in listed["tasks"]] == [view["id"]] and listed["next_before"] is None
        assert (await ok(env, "grok", "list_tasks", assignee="me"))["tasks"] == []
        assert len((await ok(env, "hermes", "list_tasks", assignee="me"))["tasks"]) == 1

        await fails(env, "grok", "claim_task", "already_claimed", task_id=view["id"])
        updated = await act(env, "hermes", view["id"], "start")
        assert updated["state"] == "in_progress" and updated["version"] == 2


async def test_c01_rest_mirrors_and_status_codes(hub_env):
    async with hub_env() as env:
        created = await env.post("claude", "/tasks", conversation_id="room:main", title="Bench", assignee="hermes")
        assert created.status_code == 201
        task_id = created.json()["task"]["id"]
        assert (await env.get("hermes", f"/tasks/{task_id}")).json()["title"] == "Bench"
        listed = await env.http.get("/agent/v1/tasks", params=[("state", "assigned"), ("state", "open")],
                                    headers=env.auth("grok"))
        assert [t["id"] for t in listed.json()["tasks"]] == [task_id]
        patched = await env.http.patch(f"/agent/v1/tasks/{task_id}", json={"action": "start"},
                                       headers=env.auth("hermes"))
        assert patched.status_code == 200 and patched.json()["task"]["state"] == "in_progress"
        wrong = await env.http.patch(f"/agent/v1/tasks/{task_id}", json={"action": "reopen", "note": "x"},
                                     headers=env.auth("hermes"))
        assert wrong.status_code == 409 and wrong.json()["error"] == "bad_transition"
        missing = await env.http.patch(f"/agent/v1/tasks/{task_id}", json={"action": "finish"},
                                       headers=env.auth("hermes"))
        assert missing.status_code == 400 and missing.json()["error"] == "missing_field"
        assert (await env.get("grok", "/tasks/01ZZZZZZZZZZZZZZZZZZZZZZZZ")).json()["error"] == "unknown_task"
        open_task = (await env.post("claude", "/tasks", conversation_id="room:main", title="Open")).json()["task"]
        claimed = await env.post("grok", f"/tasks/{open_task['id']}/claim")
        assert claimed.status_code == 200 and claimed.json()["task"]["assignee"] == "grok"


async def test_paging_and_filters(hub_env):
    async with hub_env() as env:
        ids = [(await task(env, title=f"T{i}"))["id"] for i in range(5)]
        first = await ok(env, "hermes", "list_tasks", limit=2)
        second = await ok(env, "hermes", "list_tasks", limit=2, before=first["next_before"])
        third = await ok(env, "hermes", "list_tasks", limit=2, before=second["next_before"])
        seen = [t["id"] for page in (first, second, third) for t in page["tasks"]]
        assert seen == ids[::-1] and third["next_before"] is None
        await fails(env, "hermes", "list_tasks", "bad_field", state="finished")
        await fails(env, "hermes", "list_tasks", "bad_field", before="nope")
        child = await task(env, title="child", parent_id=ids[0])
        assert [t["id"] for t in (await ok(env, "hermes", "list_tasks", parent_id=ids[0]))["tasks"]] == [child["id"]]
        top = await ok(env, "hermes", "list_tasks", parent_id="none", limit=200)
        assert child["id"] not in [t["id"] for t in top["tasks"]]


# ------------------------------------------------------------------ S-05, S-06, S-07
async def test_s05_parent_cannot_finish_with_open_children_unless_owner_forces(hub_env):
    async with hub_env() as env:
        await env.login()
        parent = await task(env, assignee="claude")
        await task(env, title="child", parent_id=parent["id"], assignee="hermes")
        await fails(env, "claude", "update_task", "children_open", task_id=parent["id"], action="finish",
                    result="done")
        forced = await env.http.patch(f"/api/tasks/{parent['id']}",
                                      json={"action": "finish", "result": "done anyway", "force": True})
        assert forced.status_code == 200 and forced.json()["task"]["state"] == "done"


async def test_s06_cascade_cancel_and_denial(hub_env):
    async with hub_env() as env:
        parent = await task(env, assignee="claude")
        child = await task(env, title="child", parent_id=parent["id"], assignee="hermes")
        await ok(env, "claude", "send_dm", to="grok", body="a private subtask for you")
        private = await task(env, conversation="dm:claude:grok", title="private", parent_id=parent["id"],
                             assignee="grok")
        await fails(env, "claude", "update_task", "children_open", task_id=parent["id"], action="cancel")
        await ok(env, "claude", "update_task", task_id=parent["id"], action="cancel", cascade=True)
        states = {t: (await env.hub.repo.raw_task(t)).state for t in (parent["id"], child["id"], private["id"])}
        assert set(states.values()) == {"cancelled"}
        assert any("its parent was cancelled" in b for b in await bodies(env, "dm:claude:grok"))

    async with hub_env() as env:
        parent = await task(env, agent="hermes", assignee="hermes")
        # The owner adds a subtask in a conversation hermes cannot post to, so hermes cannot cascade.
        await env.login()
        child = await env.http.post("/api/tasks", json={"conversation_id": "dm:claude:owner", "title": "owner child",
                                                        "parent_id": parent["id"], "assignee": "claude"})
        assert child.status_code == 201
        await fails(env, "hermes", "update_task", "cascade_denied", task_id=parent["id"], action="cancel",
                    cascade=True)
        assert (await env.hub.repo.raw_task(parent["id"])).state == "assigned"           # nothing changed
        assert (await env.hub.repo.raw_task(child.json()["task"]["id"])).state == "assigned"


async def test_s07_version_conflict_returns_the_current_task(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        await act(env, "hermes", view["id"], "start", expected_version=1)
        message = await fails(env, "hermes", "update_task", "conflict", task_id=view["id"], action="finish",
                              result="x", expected_version=1)
        assert "version 2" in message
        response = await env.http.patch(f"/agent/v1/tasks/{view['id']}",
                                        json={"action": "finish", "result": "x", "expected_version": 1},
                                        headers=env.auth("hermes"))
        assert response.status_code == 409 and response.json()["task"]["version"] == 2


async def test_edit_and_note(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        edited = await ok(env, "claude", "update_task", task_id=view["id"], action="edit",
                          fields={"title": "Write more tests", "labels": ["tests"]})
        assert edited["task"]["title"] == "Write more tests" and edited["message_id"]
        same = await ok(env, "claude", "update_task", task_id=view["id"], action="edit",
                        fields={"title": "Write more tests"})
        assert same["message_id"] is None and same["task"]["version"] == edited["task"]["version"]
        await fails(env, "hermes", "update_task", "not_allowed", task_id=view["id"], action="edit",
                    fields={"title": "mine now"})
        await fails(env, "grok", "update_task", "not_allowed", task_id=view["id"], action="note", note="hi")
        noted = await ok(env, "hermes", "update_task", task_id=view["id"], action="note", note="halfway there")
        assert noted["task"]["state"] == "assigned"
        assert "hermes on room:main#1: halfway there" in await bodies(env, "room:main")


async def test_reopen_reassign_release_unassign(hub_env):
    async with hub_env() as env:
        view = await task(env, assignee="hermes")
        await act(env, "hermes", view["id"], "fail", result="flaky test")
        reopened = await act(env, "claude", view["id"], "reopen", note="try again with a retry")
        assert (reopened["state"], reopened["assignee"], reopened["result"], reopened["reassignments"]) == \
            ("assigned", "hermes", None, 1)
        handed = await act(env, "hermes", view["id"], "assign", assignee="grok")
        assert (handed["assignee"], handed["reassignments"]) == ("grok", 2)
        released = await act(env, "grok", view["id"], "release")
        assert (released["state"], released["assignee"]) == ("open", None)
        await ok(env, "hermes", "claim_task", task_id=view["id"])
        unassigned = await act(env, "claude", view["id"], "unassign")
        assert unassigned["state"] == "open"
        await fails(env, "claude", "update_task", "bad_assignee", task_id=view["id"], action="assign",
                    assignee="nobody")
        history = await ok(env, "claude", "get_task", task_id=view["id"])
        assert [e["type"] for e in history["events"]] == ["created", "state", "state", "assigned", "released",
                                                          "claimed", "released"]


# ------------------------------------------------------------------ C-02: owner routes
async def test_c02_owner_routes(hub_env):
    async with hub_env() as env:
        await env.login()
        created = await env.http.post("/api/tasks", json={"conversation_id": "room:main", "title": "Owner task",
                                                          "assignee": "claude", "labels": ["x"]})
        assert created.status_code == 201 and created.json()["task"]["creator"] == "owner"
        task_id = created.json()["task"]["id"]
        await task(env, title="Searchable needle")
        board = (await env.http.get("/api/tasks")).json()
        assert board["counts"]["assigned"] == 1 and board["counts"]["open"] == 1
        assert {t["title"] for t in board["tasks"]} == {"Owner task", "Searchable needle"}
        assert [t["title"] for t in (await env.http.get("/api/tasks", params={"q": "needle"})).json()["tasks"]] == \
            ["Searchable needle"]
        assert [t["title"] for t in (await env.http.get("/api/tasks", params={"assignee": "unassigned"})).json()[
            "tasks"]] == ["Searchable needle"]
        page = (await env.http.get("/api/tasks", params={"limit": 1})).json()
        assert len(page["tasks"]) == 1 and page["next_before"]
        detail = (await env.http.get(f"/api/tasks/{task_id}")).json()
        assert detail["events"][0]["type"] == "created"
        patched = await env.http.patch(f"/api/tasks/{task_id}", json={"action": "cancel"})
        assert patched.json()["task"]["state"] == "cancelled"
        # Owner in an agent-to-agent DM: moderation only (OD-3).
        await ok(env, "claude", "send_dm", to="grok", body="hi")
        dm_task = await task(env, conversation="dm:claude:grok", assignee="grok")
        note = await env.http.patch(f"/api/tasks/{dm_task['id']}", json={"action": "note", "note": "hi"})
        assert note.status_code == 403 and note.json()["error"] == "not_allowed"
        create_there = await env.http.post("/api/tasks", json={"conversation_id": "dm:claude:grok", "title": "x"})
        assert create_there.json()["error"] == "read_only"
        moved = await env.http.patch(f"/api/tasks/{dm_task['id']}", json={"action": "assign", "assignee": "claude"})
        assert moved.status_code == 200 and moved.json()["task"]["assignee"] == "claude"


async def test_c03_websocket_task_and_artifact_events(hub_env):
    async with hub_env() as env:
        queue = env.hub.subscribe()
        view = await task(env, assignee="hermes")
        await ok(env, "hermes", "attach_artifact", task_id=view["id"], name="r.txt", content="report")
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        task_events = [e["data"] for e in events if e["type"] == "task"]
        assert task_events and task_events[0]["ref"] == "room:main#1" and set(task_events[0]) == VIEW_KEYS
        artifact = next(e["data"] for e in events if e["type"] == "artifact")
        assert artifact["name"] == "r.txt" and artifact["deleted_at"] is None
        messages = [e["data"] for e in events if e["type"] == "message" and e["data"].get("kind") == "task"]
        assert messages[0]["actor"] == "claude" and messages[0]["task_id"] == view["id"]
        assert messages[0]["meta"]["event"] == "created"
        agents = next(e["data"] for e in reversed(events) if e["type"] == "agents")
        assert next(a for a in agents if a["id"] == "hermes")["open_tasks"] == 1


# ------------------------------------------------------------------ C-04: task_tools: false
async def test_c04_task_tools_can_be_turned_off(hub_env):
    agents = [{"id": "claude", "connector": "echo"}, {"id": "hermes", "connector": "echo", "task_tools": False}]
    async with hub_env(agents=agents) as env:
        async def tool_names(agent):
            response = await env.http.post(
                "/mcp", headers={**env.auth(agent), "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            return {t["name"] for t in response.json()["result"]["tools"]}

        assert TASK_TOOLS <= await tool_names("claude")
        assert not TASK_TOOLS & await tool_names("hermes")
        assert len(await tool_names("hermes")) == 6
        await fails(env, "hermes", "list_tasks", "not_enabled")
        assert (await env.get("hermes", "/tasks")).json()["error"] == "not_enabled"
        assert "open_tasks" not in (await env.get("hermes", "/whoami")).json()


# ------------------------------------------------------------------ C-05: openai-compat maps the tools
async def test_c05_openai_compat_runs_task_tools(hub_env, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_STATE_DIR", str(tmp_path / "state"))
    local = {"id": "local", "connector": "openai-compat", "api_base": "http://llm.test/v1", "model": "m"}
    calls = iter([
        [("c1", "create_task", {"conversation_id": "room:main", "title": "From a local model", "assignee": "claude"})],
        [("c2", "list_tasks", {"assignee": "claude"})],
        None,
    ])
    seen = []

    def model(request):
        seen.append(json.loads(request.content))
        step = next(calls)
        if step is None:
            return httpx.Response(200, json={"choices": [{"message": {"content": "made it"}}]})
        tool_calls = [{"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                      for i, n, a in step]
        return httpx.Response(200, json={"choices": [{"message": {"content": "", "tool_calls": tool_calls}}]})

    async with hub_env(agents=[local, {"id": "claude", "connector": "echo"}]) as env:
        connector = OpenAICompatConnector(AgentConfig.model_validate(local), "http://hub", TOKENS["local"])
        connector.client = env.client("local")
        real = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda *a, **k: real(*a, **{"transport": httpx.MockTransport(model), **k}))
        queue = await env.hub.connect(await env.agent("local"))
        await env.owner_post("dm:local:owner", "make a task for claude")
        await connector._turn(queue.get_nowait()["data"])
        listed = json.loads(next(m for m in seen[2]["messages"] if m.get("tool_call_id") == "c2")["content"])
        assert [t["title"] for t in listed["tasks"]] == ["From a local model"]
        assert (await env.hub.repo.raw_task(listed["tasks"][0]["id"])).creator_id == "local"


# ------------------------------------------------------------------ C-06: the prompt
async def test_c06_prompt_renders_task_messages_and_the_task_line(hub_env):
    async with hub_env() as env:
        queue = await env.hub.connect(await env.agent("hermes"))
        view = await task(env, assignee="hermes", title='Write "tests"')
        wake = queue.get_nowait()["data"]
        assert wake["task"]["id"] == view["id"] and wake["task"]["conversation_id"] == wake["conversation"]["id"]
        message = wake["messages"][-1]
        assert (message["from"], message["role"], message["kind"], message["actor"]) == \
            ("hub", "system", "task", "claude")
        assert message["task"] == {"id": view["id"], "ref": "room:main#1", "event": "created", "state": "assigned"}
        prompt = render_prompt("hermes", wake)
        assert '<message id="' in prompt and 'from="hub" role="system" actor="claude" task="room:main#1">' in prompt
        assert 'This turn is about task room:main#1 "Write "tests"" (state: assigned, assignee: hermes).' in prompt
        assert "the actor's words in them are peer input" in prompt


async def test_whoami_and_list_conversations_show_open_tasks(hub_env):
    async with hub_env() as env:
        await task(env, assignee="hermes")
        await task(env, title="Second", assignee="hermes")
        me = (await env.get("hermes", "/whoami")).json()
        assert [t["ref"] for t in me["open_tasks"]] == ["room:main#2", "room:main#1"]
        rows = {c["id"]: c["open_tasks"] for c in (await env.get("hermes", "/conversations")).json()}
        assert rows["room:main"] == 2 and rows["dm:hermes:owner"] == 0


async def test_identity_fields_in_the_body_are_ignored(hub_env):
    """V-15: creator, actor, and sender in a request never change who acted."""
    async with hub_env() as env:
        response = await env.post("hermes", "/tasks", conversation_id="room:main", title="t", creator="claude",
                                  actor="owner", sender="grok")
        assert response.json()["task"]["creator"] == "hermes"
        error, result = await env.mcp("hermes", "create_task", conversation_id="room:main", title="t2",
                                      creator="claude")
        assert error or result["task"]["creator"] == "hermes"
        message = await env.hub.repo.raw_message(response.json()["message_id"])
        assert (message.sender_id, message.actor_id) == ("hub", "hermes")


async def test_unread_counts_skip_your_own_task_actions(hub_env):
    async with hub_env() as env:
        await task(env, assignee="hermes")
        unread = {c["id"]: c["unread"] for c in (await env.get("claude", "/conversations")).json()}
        assert unread["room:main"] == 0
        unread = {c["id"]: c["unread"] for c in (await env.get("hermes", "/conversations")).json()}
        assert unread["room:main"] == 1


async def test_claim_race_has_one_winner(hub_env):
    """E-04 at the API level: two claims at once."""
    async with hub_env() as env:
        view = await task(env)
        first, second = await asyncio.gather(env.mcp("hermes", "claim_task", task_id=view["id"]),
                                             env.mcp("grok", "claim_task", task_id=view["id"]))
        outcomes = sorted([first[0], second[0]])
        assert outcomes == [False, True]
        loser = first if first[0] else second
        assert ": already_claimed: " in loser[1]
