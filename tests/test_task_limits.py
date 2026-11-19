"""Loops, budgets, and limits for tasks (design M4 section 15.4: L-01 to L-09)."""

from taskkit import act, fails, ok, task, turns


async def finish_turn(env, agent, queue, text="NO_REPLY"):
    wake = queue.get_nowait()["data"]
    await env.post(agent, f"/turns/{wake['turn_id']}/finish", text=text)
    return wake


async def notices(env, conversation="room:main"):
    return [m for m in await env.hub.repo.messages(env.hub.owner, conversation, limit=500) if m.kind == "notice"]


async def test_l01_reassignment_ping_pong_stops_at_the_hop_limit_and_continue_resumes(hub_env):
    async with hub_env(limits={"max_hops": 3}) as env:
        await env.login()
        claude = await env.hub.connect(await env.agent("claude"))
        hermes = await env.hub.connect(await env.agent("hermes"))
        view = await task(env, assignee="hermes")                                         # hop 1
        wake = hermes.get_nowait()["data"]
        assert wake["hop"] == 1
        await act(env, "hermes", view["id"], "assign", assignee="claude")                       # hop 2
        await env.post("hermes", f"/turns/{wake['turn_id']}/finish", text="NO_REPLY")
        wake = claude.get_nowait()["data"]
        assert wake["hop"] == 2
        await act(env, "claude", view["id"], "assign", assignee="hermes")                       # hop 3: stop
        await env.post("claude", f"/turns/{wake['turn_id']}/finish", text="NO_REPLY")
        assert hermes.empty()
        [notice] = await notices(env)
        assert notice.body == "Chain paused at 3 hops. Nobody was woken."
        assert (await env.http.post(f"/api/notices/{notice.id}/continue")).status_code == 200
        wake = hermes.get_nowait()["data"]
        assert wake["hop"] == 0 and wake["task"]["id"] == view["id"]


async def test_l02_reassignment_limit(hub_env):
    async with hub_env(limits={"tasks": {"max_reassignments": 2}}) as env:
        view = await task(env, assignee="hermes")
        await act(env, "claude", view["id"], "assign", assignee="grok")
        await act(env, "claude", view["id"], "assign", assignee="hermes")
        await fails(env, "claude", "update_task", "too_many_reassignments", task_id=view["id"], action="assign",
                    assignee="grok")
        assert [n.body for n in await notices(env)] == ["room:main#1 reached the reassignment limit (2)."]


async def test_l03_depth_children_and_open_task_limits(hub_env):
    limits = {"tasks": {"max_depth": 1, "max_children": 1, "max_open_per_agent": 1,
                        "max_open_per_conversation": 4}}
    async with hub_env(limits=limits) as env:
        parent = await task(env, assignee="claude")
        child = await task(env, title="child", parent_id=parent["id"])
        await fails(env, "claude", "create_task", "depth_exceeded", conversation_id="room:main", title="grandchild",
                    parent_id=child["id"])
        await fails(env, "claude", "create_task", "too_many_children", conversation_id="room:main", title="second",
                    parent_id=parent["id"])
        await task(env, title="for hermes", assignee="hermes")
        await fails(env, "claude", "create_task", "too_many_tasks", conversation_id="room:main", title="another",
                    assignee="hermes")
        await task(env, title="open one")                                     # 4 open in room:main now
        await fails(env, "claude", "create_task", "too_many_tasks", conversation_id="room:main", title="fifth")
        await fails(env, "hermes", "claim_task", "too_many_tasks", task_id=child["id"])

    async with hub_env(limits={"tasks": {"max_creates_per_turn": 2}}) as env:
        claude = await env.hub.connect(await env.agent("claude"))
        await env.owner_post("room:main", "@claude split it up")
        claude.get_nowait()
        await task(env, title="one")
        await task(env, title="two")
        await fails(env, "claude", "create_task", "too_many_tasks", conversation_id="room:main", title="three")


async def test_l04_task_wakes_count_against_the_budget_and_paused_agents_cannot_write(hub_env):
    agents = [{"id": "claude", "connector": "echo"},
              {"id": "hermes", "connector": "echo", "budget": {"turns_per_day": 1}}]
    async with hub_env(agents=agents) as env:
        hermes = await env.hub.connect(await env.agent("hermes"))
        first = await task(env, assignee="hermes")
        await finish_turn(env, "hermes", hermes)
        assert (await env.hub.repo.usage("hermes"))[0] == 1
        await task(env, title="second", assignee="hermes")
        assert (await env.agent("hermes")).paused                            # the wake found the budget used up
        assert any("hermes is paused" in n.body for n in await notices(env))
        await fails(env, "hermes", "update_task", "paused", task_id=first["id"], action="start")
        assert (await ok(env, "hermes", "get_task", task_id=first["id"]))["state"] == "assigned"   # reads work


async def test_l05_self_started_task_actions_are_rate_limited_and_recorded(hub_env):
    async with hub_env(limits={"turns_per_minute": 1}) as env:
        await task(env, agent="grok", title="from grok")
        recorded = await turns(env, "grok")
        assert [(t.state, t.task_id is not None) for t in recorded] == [("done", True)]
        await fails(env, "grok", "create_task", "rate_limited", conversation_id="room:main", title="again")


async def test_l06_room_modes_do_not_apply_to_task_messages(hub_env):
    async with hub_env(rooms=[{"id": "room:main", "mode": "round_robin"}]) as env:
        await task(env, title="nobody assigned")
        assert await turns(env, "hermes") == [] and await turns(env, "grok") == []


async def test_l07_mentions_in_task_text_wake_nobody_but_notes_wake_members(hub_env):
    async with hub_env() as env:
        view = await task(env, title="ask @grok later", description="@hermes too", assignee="claude")
        assert await turns(env, "grok") == [] and await turns(env, "hermes") == []
        await act(env, "claude", view["id"], "finish", result="done, thanks @hermes")
        assert await turns(env, "hermes") == []
        await act(env, "claude", view["id"], "note", note="@hermes please double-check")
        assert [t.state for t in await turns(env, "hermes")] == ["queued"]
        # In a DM, a note can only wake the other member.
        await ok(env, "claude", "send_dm", to="hermes", body="private")
        dm_task = await task(env, conversation="dm:claude:hermes", title="dm task", assignee="claude")
        await act(env, "claude", dm_task["id"], "note", note="@grok can you see this?")
        assert await turns(env, "grok") == []


async def test_l08_hop_of_each_actor_situation(hub_env):
    async with hub_env() as env:
        await env.login()
        owner_task = (await env.http.post("/api/tasks", json={"conversation_id": "room:main", "title": "o",
                                                              "assignee": "hermes"})).json()
        message = await env.hub.repo.raw_message(owner_task["message_id"])
        assert message.hop == 0                                               # owner
        hermes = await env.hub.connect(await env.agent("hermes"))
        wake = hermes.get_nowait()["data"]                                    # hermes woken at hop 0
        started = await ok(env, "hermes", "update_task", task_id=owner_task["task"]["id"], action="start")
        assert (await env.hub.repo.raw_message(started["message_id"])).hop == wake["hop"] + 1   # open turn
        await ok(env, "hermes", "send_dm", to="claude", body="side note")        # hop: max(0, last 0) + 1
        side = await task(env, agent="hermes", conversation="dm:claude:hermes", title="side")
        assert (await env.hub.repo.raw_task(side["id"])).hop == 2              # max(running 0, last 1) + 1
        self_started = await ok(env, "grok", "create_task", conversation_id="room:main", title="g")
        last = 1                                                               # hermes's start event
        assert (await env.hub.repo.raw_message(self_started["message_id"])).hop == last + 1


async def test_l08_children_done_keeps_the_hop_of_the_child_event(hub_env):
    async with hub_env() as env:
        parent = await task(env, assignee="claude")
        child = await task(env, title="child", parent_id=parent["id"], assignee="hermes")
        finished = await ok(env, "hermes", "update_task", task_id=child["id"], action="finish", result="ok")
        child_hop = (await env.hub.repo.raw_message(finished["message_id"])).hop
        events = (await ok(env, "claude", "get_task", task_id=parent["id"]))["events"]
        assert events[-1]["type"] == "children_done" and events[-1]["hop"] == child_hop


async def test_l09_stop_leaves_the_task_state_alone(hub_env):
    async with hub_env() as env:
        await env.login()
        hermes = await env.hub.connect(await env.agent("hermes"))
        view = await task(env, assignee="hermes")
        hermes.get_nowait()
        await act(env, "hermes", view["id"], "start")
        await env.http.post("/api/agents/hermes/stop")
        assert (await env.hub.repo.raw_task(view["id"])).state == "in_progress"
