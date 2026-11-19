"""End-to-end exit tests for M4 (design section 15.1: E-01 to E-06).

Exit criterion: agents split a task (EC1) and report status (EC2) without the owner relaying (EC3).
"""

from scripted import Driver
from taskkit import act, ok

FAST = {"turns_per_minute": 1000}


async def mine(env, agent):
    return (await ok(env, agent, "list_tasks", assignee="me"))["tasks"]


def coordinator(state):
    """claude: split the request into a parent and two subtasks; finish the parent when all are done.
    `state["private"]` puts grok's subtask in dm:claude:grok instead of the room."""

    async def run(env, wake):
        trigger = next(m for m in wake["messages"] if m["id"] == wake["trigger_message_id"])
        if trigger["from"] == "owner":                                      # the owner's request
            parent = (await ok(env, "claude", "create_task", conversation_id="room:main", title="Build X",
                               assignee="claude"))["task"]
            state["parent"] = parent["id"]
            await ok(env, "claude", "create_task", conversation_id="room:main", title="Tests for X",
                     parent_id=parent["id"], assignee="hermes")
            if state.get("private"):
                await ok(env, "claude", "send_dm", to="grok", body="one subtask for you, see the task")
            await ok(env, "claude", "create_task",
                     conversation_id="dm:claude:grok" if state.get("private") else "room:main",
                     title="Review X", parent_id=parent["id"], assignee="grok")
            return "I split X into 2 subtasks."
        state.setdefault("task_wakes", []).append(trigger)
        parent = await ok(env, "claude", "get_task", task_id=state["parent"])
        for child in parent["children_list"]:
            if child["state"] == "failed" and child["id"] not in state.setdefault("reopened", set()):
                state["reopened"].add(child["id"])
                await act(env, "claude", child["id"], "reopen", note="Please retry; the CI runner was flaky.")
                return "NO_REPLY"
        if parent["state"] != "done" and parent["children"]["open"] == 0 and \
                parent["children"]["total"] == state.get("expected_children", 2):
            summary = "; ".join(f"{c['title']}: {c['result']}" for c in parent["children_list"])
            for artifact in (await ok(env, "claude", "get_task", task_id=parent["children_list"][0]["id"]))[
                    "artifacts"]:
                state.setdefault("read", []).append(
                    (await ok(env, "claude", "read_artifact", artifact_id=artifact["id"]))["content"])
            await act(env, "claude", state["parent"], "finish", result=summary)
            return f"X is done. {summary}"
        return "NO_REPLY"

    return run


def worker(agent, fail_first=False):
    attempts = {"n": 0}

    async def run(env, wake):
        task = wake.get("task")
        if not task or task["assignee"] != agent or task["state"] not in ("assigned",):
            return "NO_REPLY"
        attempts["n"] += 1
        await act(env, agent, task["id"], "start")
        await ok(env, agent, "attach_artifact", task_id=task["id"], name="pytest-report.txt",
                 content=f"{attempts['n']} run: 12 passed")
        if fail_first and attempts["n"] == 1:
            await act(env, agent, task["id"], "fail", result="2 tests failed on the CI runner")
        else:
            await act(env, agent, task["id"], "finish", result="12 tests pass")
        return "NO_REPLY"

    return run


def webhook_worker(agent):
    async def run(env, ping):
        await ok(env, agent, "read_messages", conversation_id=ping["conversation_id"])
        for task in await mine(env, agent):
            if task["state"] == "assigned" and task["conversation_id"] == ping["conversation_id"]:
                await act(env, agent, task["id"], "finish", result="looks good; one nit in naming")
        await ok(env, agent, "post_message", conversation_id=ping["conversation_id"], body="NO_REPLY")

    return run


async def owner_messages(env):
    return [m for m in await env.hub.repo.activity(env.hub.owner, limit=500)
            if m.sender_id == "owner" and m.kind == "chat"]


async def all_tasks(env):
    return (await env.http.get("/api/tasks", params={"limit": 500})).json()["tasks"]


async def test_e01_split_and_report_without_the_owner_relaying(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        state = {}
        driver = Driver(env, {"claude": coordinator(state), "hermes": worker("hermes")},
                        {"grok": webhook_worker("grok")})
        await driver.connect()
        await env.owner_post("room:main", "@claude build feature X, split it up")
        await driver.run()

        assert len(await owner_messages(env)) == 1                                  # EC3
        tasks = await all_tasks(env)
        assert len(tasks) == 3 and {t["state"] for t in tasks} == {"done"}            # EC1, EC2
        parent = next(t for t in tasks if t["id"] == state["parent"])
        assert parent["result"] and parent["children"] == {"total": 2, "open": 0, "done": 2, "failed": 0,
                                                            "cancelled": 0}
        assert state["read"] == ["1 run: 12 passed"]                               # claude read the report
        assert any(w.get("kind") == "task" for w in state["task_wakes"])           # woken by a task event
        events = [m.meta.get("event") for m in await env.hub.repo.messages(env.hub.owner, "room:main", limit=500)
                  if m.kind == "task"]
        assert "children_done" in events
        notices = [m for m in await env.hub.repo.activity(env.hub.owner, limit=500) if m.kind == "notice"]
        assert notices == []
        room = [m.body for m in await env.hub.repo.messages(env.hub.owner, "room:main", limit=500)]
        assert room[-1].startswith("X is done.")


async def test_e02_a_failed_subtask_is_reopened_and_finished(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        state = {}
        driver = Driver(env, {"claude": coordinator(state), "hermes": worker("hermes", fail_first=True)},
                        {"grok": webhook_worker("grok")})
        await driver.connect()
        await env.owner_post("room:main", "@claude build feature X, split it up")
        await driver.run()
        tasks = {t["title"]: t for t in await all_tasks(env)}
        assert tasks["Tests for X"]["state"] == "done" and tasks["Tests for X"]["reassignments"] == 1
        assert tasks["Build X"]["state"] == "done"
        detail = (await env.http.get(f"/api/tasks/{tasks['Tests for X']['id']}")).json()
        assert [e["to_state"] for e in detail["events"] if e["type"] == "state"] == \
            ["in_progress", "failed", "assigned", "in_progress", "done"]
        assert len(await owner_messages(env)) == 1


async def test_e03_block_wakes_the_creator_unblock_does_not(hub_env):
    async with hub_env(limits=FAST) as env:
        claude = await env.hub.connect(await env.agent("claude"))
        hermes = await env.hub.connect(await env.agent("hermes"))
        view = (await ok(env, "claude", "create_task", conversation_id="room:main", title="t",
                         assignee="hermes"))["task"]
        wake = hermes.get_nowait()["data"]
        await act(env, "hermes", view["id"], "start")
        await act(env, "hermes", view["id"], "block", blocked_reason="need the staging password from the owner")
        await env.post("hermes", f"/turns/{wake['turn_id']}/finish", text="NO_REPLY")
        woken = claude.get_nowait()["data"]
        assert "need the staging password" in woken["messages"][-1]["body"]
        assert woken["task"]["state"] == "blocked"
        await env.post("claude", f"/turns/{woken['turn_id']}/finish", text="NO_REPLY")
        await act(env, "hermes", view["id"], "unblock")
        assert claude.empty()


async def test_e04_claim_race(hub_env):
    async with hub_env(limits=FAST) as env:
        view = (await ok(env, "claude", "create_task", conversation_id="room:main", title="open"))["task"]
        import asyncio
        results = await asyncio.gather(env.post("hermes", f"/tasks/{view['id']}/claim"),
                                       env.post("grok", f"/tasks/{view['id']}/claim"))
        assert sorted(r.status_code for r in results) == [200, 409]
        loser = next(r for r in results if r.status_code == 409).json()
        assert loser["error"] == "already_claimed"


async def test_e05_private_subtask_stays_private(hub_env):
    async with hub_env(limits=FAST) as env:
        await env.login()
        state = {"private": True}
        driver = Driver(env, {"claude": coordinator(state), "hermes": worker("hermes")},
                        {"grok": webhook_worker("grok")})
        await driver.connect()
        await env.owner_post("room:main", "@claude build feature X, split it up")
        await driver.run()
        tasks = {t["title"]: t for t in await all_tasks(env)}
        assert {t["state"] for t in tasks.values()} == {"done"}
        assert tasks["Review X"]["conversation_id"] == "dm:claude:grok"
        hub_lines = [m.body for m in await env.hub.repo.messages(env.hub.owner, "room:main", limit=500)
                     if m.sender_id == "hub"]
        # What the hub writes never names the private subtask; claude's own result text is its choice.
        assert not any("dm:claude:grok" in b or "subtasks of" in b for b in hub_lines)
        assert not any('"Review X"' in b for b in hub_lines)
        hermes_view = await ok(env, "hermes", "get_task", task_id=state["parent"])
        assert hermes_view["children"]["total"] == 1                              # only its own subtask
        # claude was woken in the DM, where grok's finish is the trigger.
        dm_wakes = [w for a, w in driver.wakes if a == "claude" and isinstance(w, dict) and
                    w.get("conversation", {}).get("id") == "dm:claude:grok"]
        assert dm_wakes and dm_wakes[-1]["task"]["title"] == "Review X"


async def test_e06_approval_mode_holds_task_wakes_until_the_owner_approves(hub_env):
    async with hub_env(limits={**FAST, "approval_mode": True}) as env:
        await env.login()
        state = {}
        driver = Driver(env, {"claude": coordinator(state), "hermes": worker("hermes")},
                        {"grok": webhook_worker("grok")}, approve=True)
        await driver.connect()
        await env.owner_post("room:main", "@claude build feature X, split it up")
        await driver.run()
        assert {t["state"] for t in await all_tasks(env)} == {"done"}
        # hermes's and grok's subtask wakes and claude's report wakes all waited for the owner.
        assert driver.approvals >= 3
        task_turns = await env.hub.repo.turns("task_id IS NOT NULL AND state = 'cancelled'")
        assert task_turns == []                                                    # none was rejected
        assert len(await owner_messages(env)) == 1                                # approvals relay nothing
