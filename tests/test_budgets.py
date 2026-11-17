"""Loop and cost control (design section 6): hop limit, rate limit, budgets, pause, stop, approval."""

from agent_hub.core.budgets import RateLimiter


def test_rate_limiter():
    clock = [0.0]
    limiter = RateLimiter(2, window=60, clock=lambda: clock[0])
    assert limiter.wait_time("a") == 0
    limiter.record("a")
    clock[0] = 10
    limiter.record("a")
    assert limiter.wait_time("a") == 50 and limiter.wait_time("b") == 0
    clock[0] = 61
    assert limiter.wait_time("a") == 0


async def notices(env, conversation_id="room:main"):
    return [m for m in await env.hub.repo.messages(env.hub.owner, conversation_id) if m.kind == "notice"]


async def ping_pong(env, queues, rounds=30) -> int:
    """claude and hermes answer every wake by mentioning each other. Returns the replies posted."""
    replies = 0
    for _ in range(rounds):
        progressed = False
        for agent, other in (("claude", "hermes"), ("hermes", "claude")):
            while not queues[agent].empty():
                wake = queues[agent].get_nowait()["data"]
                await env.post(agent, f"/turns/{wake['turn_id']}/finish", text=f"@{other} your move")
                replies += 1
                progressed = True
        if not progressed:
            break
    return replies


async def test_ping_pong_is_stopped_at_the_hop_limit_and_continue_resumes_it(hub_env):
    async with hub_env(limits={"max_hops": 4, "turns_per_minute": 100}) as env:
        queues = {a: await env.hub.connect(await env.agent(a)) for a in ("claude", "hermes")}
        await env.owner_post("room:main", "@claude start a debate with hermes")
        assert await ping_pong(env, queues) == 4                       # hops 1..4, then nobody is woken
        found = await notices(env)
        assert len(found) == 1 and "4 hops" in found[0].body
        # The notice is for the owner only.
        seen = (await env.get("claude", "/conversations/room:main/messages?after=0")).text
        assert "Chain paused" not in seen

        await env.login()
        assert (await env.http.post(f"/api/notices/{found[0].id}/continue")).status_code == 200
        assert (await env.http.post(f"/api/notices/{found[0].id}/continue")).status_code == 409
        assert await ping_pong(env, queues) == 4                       # one more chain, then stopped again
        assert len(await notices(env)) == 2


async def test_rate_limit_queues_excess_wakes(hub_env):
    async with hub_env(limits={"turns_per_minute": 2}) as env:
        queue = await env.hub.connect(await env.agent("claude"))
        for n in range(3):
            await env.owner_post("room:main", f"@claude {n}")
            if not queue.empty():
                await env.post("claude", f"/turns/{queue.get_nowait()['data']['turn_id']}/finish", text="NO_REPLY")
        assert [t.state for t in await env.turns("claude")] == ["done", "done", "queued"]
        assert "claude" in env.hub.retry                               # it is retried when the window opens
        # The same limit applies to posts an agent makes on its own initiative.
        for expected in (200, 200, 429):
            response = await env.post("hermes", "/conversations/room:main/messages", body="unprompted")
            assert response.status_code == expected


async def test_turn_budget_pauses_the_agent_and_notifies_the_owner(hub_env):
    async with hub_env() as env:
        await env.login()
        assert (await env.http.put("/api/agents/grok/budget", json={"turns_per_day": 1})).status_code == 200
        await env.owner_post("dm:grok:owner", "one")
        await env.mcp("grok", "read_messages", conversation_id="dm:grok:owner")
        await env.mcp("grok", "post_message", conversation_id="dm:grok:owner", body="done")
        await env.owner_post("dm:grok:owner", "two")
        await env.settle()
        assert (await env.agent("grok")).paused
        assert len(env.pings) == 1                                     # no ping for the second wake
        found = await notices(env, "dm:grok:owner")
        assert len(found) == 1 and "daily budget of 1 turns" in found[0].body
        is_error, text = await env.mcp("grok", "post_message", conversation_id="dm:grok:owner", body="late")
        assert is_error and "paused:" in text

        await env.http.put("/api/agents/grok/budget", json={"turns_per_day": 5})
        await env.http.post("/api/agents/grok/resume")
        await env.settle()
        assert len(env.pings) == 2 and not (await env.agent("grok")).paused


async def test_usd_budget_counts_connector_cost(hub_env):
    agents = [{"id": "claude", "connector": "echo", "budget": {"usd_per_day": 1.0}}]
    async with hub_env(agents=agents) as env:
        queue = await env.hub.connect(await env.agent("claude"))
        await env.owner_post("dm:claude:owner", "one")
        turn_id = queue.get_nowait()["data"]["turn_id"]
        await env.post("claude", f"/turns/{turn_id}/finish", text="ok", usage={"cost_usd": 1.25})
        await env.owner_post("dm:claude:owner", "two")
        assert queue.empty() and (await env.agent("claude")).paused
        await env.login()
        agents = (await env.http.get("/api/agents")).json()
        assert agents[0]["usd_today"] == 1.25 and agents[0]["status"] == "paused"


async def test_pause_holds_wakes_and_blocks_posts(hub_env):
    async with hub_env() as env:
        await env.login()
        await env.http.post("/api/agents/grok/pause")
        await env.owner_post("dm:grok:owner", "hello")
        await env.settle()
        assert env.pings == []
        for tool, args in (("post_message", {"conversation_id": "dm:grok:owner", "body": "x"}),
                           ("send_dm", {"to": "claude", "body": "x"})):
            is_error, text = await env.mcp("grok", tool, **args)
            assert is_error and "paused:" in text
        await env.http.post("/api/agents/grok/resume")
        await env.settle()
        assert len(env.pings) == 1

        # Global pause.
        await env.http.post("/api/agents/all/pause")
        queue = await env.hub.connect(await env.agent("claude"))
        await env.owner_post("room:main", "@claude hello")
        assert queue.empty()
        await env.http.post("/api/agents/all/resume")
        assert queue.get_nowait()["event"] == "wake"


async def test_stop_cancels_the_turn_and_rejects_a_late_post(hub_env):
    async with hub_env() as env:
        await env.login()
        queue = await env.hub.connect(await env.agent("claude"))
        await env.owner_post("room:main", "@claude go")
        await env.owner_post("dm:grok:owner", "go")
        turn_id = queue.get_nowait()["data"]["turn_id"]
        await env.mcp("grok", "read_messages", conversation_id="dm:grok:owner")

        assert (await env.http.post("/api/agents/all/stop")).json() == {"cancelled": 2}
        assert queue.get_nowait() == {"event": "cancel", "data": {"turn_id": turn_id}}
        assert (await env.post("claude", f"/turns/{turn_id}/finish", text="late")).status_code == 409
        is_error, text = await env.mcp("grok", "post_message", conversation_id="dm:grok:owner", body="late")
        assert is_error and "stopped:" in text
        assert [m.body for m in await env.hub.repo.messages(env.hub.owner, "dm:grok:owner")] == ["go"]
        # A new wake in that conversation lets grok post again.
        await env.owner_post("dm:grok:owner", "try again")
        is_error, _ = await env.mcp("grok", "post_message", conversation_id="dm:grok:owner", body="ok")
        assert not is_error


async def test_approval_mode_holds_agent_to_agent_wakes(hub_env):
    async with hub_env() as env:
        await env.login()
        assert (await env.http.put("/api/settings", json={"approval_mode": True})).json()["approval_mode"]
        await env.hub.send_dm(await env.agent("claude"), "grok", "psst")
        await env.hub.send_dm(await env.agent("hermes"), "grok", "psst")
        await env.owner_post("room:main", "@hermes owner wakes are not held")
        await env.settle()
        assert env.pings == []
        assert [t.state for t in await env.turns("grok")] == ["awaiting_approval", "awaiting_approval"]
        assert (await env.turns("hermes"))[-1].state == "queued"         # [0] records its own unprompted DM
        listed = (await env.get("grok", "/conversations")).json()
        assert not any(c["pending_wake"] for c in listed)

        first, second = await env.turns("grok")
        await env.http.post(f"/api/turns/{first.id}/approve")
        await env.http.post(f"/api/turns/{second.id}/reject")
        await env.settle()
        assert [t.state for t in await env.turns("grok")] == ["queued", "cancelled"]
        assert len(env.pings) == 1 and env.pings[0]["body"]["conversation_id"] == "dm:claude:grok"


async def test_room_mode_can_be_changed_by_the_owner(hub_env):
    async with hub_env() as env:
        await env.login()
        queue = await env.hub.connect(await env.agent("claude"))
        await env.owner_post("room:main", "nobody is mentioned")
        assert queue.empty()
        assert (await env.http.put("/api/conversations/room:main/mode", json={"mode": "round_robin"})).status_code == 200
        await env.owner_post("room:main", "still nobody mentioned")
        assert queue.get_nowait()["event"] == "wake"
        assert (await env.http.put("/api/conversations/dm:claude:owner/mode", json={"mode": "mention"})).status_code == 400


async def test_ui_budget_survives_restart_until_agents_yaml_changes(hub_env):
    def agents(budget):
        return [{"id": "claude", "connector": "echo", "budget": budget}]

    yaml_budget = {"usd_per_day": 1.0, "turns_per_conversation": 3}
    async with hub_env(agents=agents(yaml_budget)) as env:
        await env.login()
        await env.http.put("/api/agents/claude/budget", json={"usd_per_day": 7.0})
        # The UI changes the per-day limit and keeps the per-conversation limit from agents.yaml.
        assert env.hub._budget(await env.agent("claude")) == {"usd_per_day": 7.0, "turns_per_conversation": 3}

    async with hub_env(agents=agents(yaml_budget)) as env:           # restart, same agents.yaml
        assert env.hub._budget(await env.agent("claude"))["usd_per_day"] == 7.0

    async with hub_env(agents=agents({"usd_per_day": 2.0})) as env:  # restart, budget edited in agents.yaml
        assert env.hub._budget(await env.agent("claude")) == {"usd_per_day": 2.0}
