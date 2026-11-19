"""Multiple rooms with their own members (design M5d section 21.6: RM-01 to RM-06)."""

import json

from taskkit import ok, task

FAST = {"turns_per_minute": 1000}


async def make_room(env, room_id="room:research", members=("claude", "hermes"), **fields):
    response = await env.http.post("/api/rooms", json={"id": room_id, "members": list(members), **fields})
    assert response.status_code == 201, response.text
    return response.json()


async def test_rm01_a_room_is_invisible_to_non_members_on_every_surface(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        room = await make_room(env)
        assert room["members"] == ["owner", "claude", "hermes"] and room["source"] == "ui"
        await env.owner_post("room:research", "MARK_RESEARCH chat")
        view = await task(env, conversation="room:research", title="MARK_RESEARCH task", assignee="hermes")
        await ok(env, "hermes", "attach_artifact", task_id=view["id"], name="r.txt", content="MARK_RESEARCH file")
        payloads = [
            (await env.get("grok", "/conversations")).json(), (await env.get("grok", "/whoami")).json(),
            await ok(env, "grok", "list_tasks", include_closed=True),
            (await env.get("grok", "/conversations/room:research/messages")).json(),
            (await env.get("grok", f"/tasks/{view['id']}")).json(),
        ]
        text = json.dumps(payloads)
        assert "MARK_RESEARCH" not in text
        assert payloads[3]["error"] == "unknown_conversation" and payloads[4]["error"] == "unknown_task"
        assert "room:research" in {c["id"] for c in (await env.get("hermes", "/conversations")).json()}


async def test_rm02_joining_starts_at_the_latest_message(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        await make_room(env, members=["claude", "hermes"])
        for i in range(3):
            await env.owner_post("room:research", f"old message {i}")
        response = await env.http.put("/api/rooms/room:research/members", json={"members": ["claude", "hermes",
                                                                                            "grok"]})
        assert response.json() == {"added": ["grok"], "removed": [], "released_tasks": []}
        rows = {c["id"]: c["unread"] for c in (await env.get("grok", "/conversations")).json()}
        assert rows["room:research"] == 0
        await env.owner_post("room:research", "@grok welcome")
        assert [m["body"] for m in (await env.get("grok", "/conversations/room:research/messages")).json()] == \
            ["@grok welcome"]
        # Full history stays readable (open decision OD-19, option a).
        assert len((await env.get("grok", "/conversations/room:research/messages?after=0")).json()) == 4


async def test_rm03_leaving_cancels_turns_releases_tasks_and_removes_access(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        await make_room(env, members=["claude", "hermes"])
        hermes = await env.hub.connect(await env.agent("hermes"))
        view = await task(env, conversation="room:research", title="research it", assignee="hermes")
        wake = hermes.get_nowait()["data"]
        assert wake["conversation"]["id"] == "room:research"
        response = await env.http.put("/api/rooms/room:research/members", json={"members": ["claude"]})
        assert response.json() == {"added": [], "removed": ["hermes"], "released_tasks": ["room:research#1"]}
        assert hermes.get_nowait() == {"event": "cancel", "data": {"turn_id": wake["turn_id"]}}
        assert (await env.hub.repo.turn(wake["turn_id"])).state == "cancelled"
        released = await env.hub.repo.raw_task(view["id"])
        assert (released.state, released.assignee_id) == ("open", None)
        assert (await env.get("hermes", "/conversations/room:research/messages")).status_code == 404
        assert (await env.get("hermes", f"/tasks/{view['id']}")).status_code == 404
        late = await env.post("hermes", f"/turns/{wake['turn_id']}/finish", text="too late")
        assert late.status_code == 409


async def test_rm04_archive_blocks_posts_task_writes_and_wakes(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        await make_room(env)
        view = await task(env, conversation="room:research", title="t", assignee="hermes")
        assert (await env.http.post("/api/rooms/room:research/archive")).json() == {"ok": True}
        posted = await env.post("claude", "/conversations/room:research/messages", body="@hermes still there?")
        assert posted.status_code == 409 and posted.json()["error"] == "archived"
        owner_post = await env.http.post("/api/conversations/room:research/messages", json={"body": "hi"})
        assert owner_post.json()["error"] == "archived"
        error, text = await env.mcp("hermes", "update_task", task_id=view["id"], action="start")
        assert error and "archived" in text
        assert await env.hub.repo.turns("conversation_id = 'room:research' AND state IN ('queued', 'running')") == []
        assert "room:research" not in {c["id"] for c in (await env.get("hermes", "/conversations")).json()}
        assert len((await env.http.get("/api/conversations/room:research/messages")).json()) >= 1   # owner reads
        archived = next(c for c in (await env.http.get("/api/conversations")).json() if c["id"] == "room:research")
        assert archived["archived_at"]
        main = await env.http.post("/api/rooms/room:main/archive")
        assert main.status_code == 409 and main.json()["error"] == "main_room"


async def test_rm05_ui_rooms_survive_a_restart_and_config_rooms_follow_the_file(hub_env):
    rooms = [{"id": "room:main"}, {"id": "room:ops", "members": ["claude"]}]
    async with hub_env(rooms=rooms) as env:
        await env.login()
        await make_room(env, members=["hermes"])
        ops = await env.http.put("/api/rooms/room:ops/members", json={"members": ["claude", "hermes"]})
        assert ops.status_code == 409 and ops.json()["error"] == "config_room"
    rooms = [{"id": "room:main"}, {"id": "room:ops", "members": ["claude", "grok"]}]
    async with hub_env(rooms=rooms) as env:
        await env.login()
        found = {c["id"]: c for c in (await env.http.get("/api/conversations")).json()}
        assert found["room:research"]["members"] == ["owner", "hermes"]
        assert found["room:ops"]["members"] == ["owner", "claude", "grok"]
        assert found["room:ops"]["source"] == "config"


async def test_rm06_each_room_has_its_own_mode_and_all(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        await make_room(env, members=["claude", "hermes"], mode="round_robin")
        await env.owner_post("room:research", "@all opinions?")
        research = {t.agent_id for t in await env.hub.repo.turns("conversation_id = 'room:research'")}
        assert research == {"claude"}                                       # sequential fan-out: first in order
        await env.owner_post("room:main", "nobody mentioned")                # mention mode: nobody
        assert await env.hub.repo.turns("conversation_id = 'room:main'") == []
        patched = await env.http.patch("/api/rooms/room:research", json={"mode": "mention", "title": "Research"})
        assert (patched.json()["mode"], patched.json()["title"]) == ("mention", "Research")


async def test_room_validation(hub_env):
    async with hub_env() as env:
        await env.login()
        for body, code in (({"id": "Research"}, "bad_room_id"), ({"id": "room:main"}, "exists"),
                           ({"id": "room:x", "members": ["ghost"]}, "unknown_participant"),
                           ({"id": "room:y", "members": ["claude"], "moderator": "hermes"}, "bad_field")):
            response = await env.http.post("/api/rooms", json=body)
            assert response.json()["error"] == code, body
        await make_room(env, members=["claude", "hermes"], moderator="hermes")
        await env.http.put("/api/rooms/room:research/members", json={"members": ["claude"]})
        room = next(c for c in (await env.http.get("/api/conversations")).json() if c["id"] == "room:research")
        assert room["moderator"] is None                                    # the moderator left
        await env.http.post("/api/logout")
        response = await env.http.post("/api/rooms", json={"id": "room:z"}, headers=env.auth("claude"))
        assert response.status_code == 401                                  # owner only
