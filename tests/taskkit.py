"""Helpers for the task tests: call a tool and expect success or a given error."""


async def ok(env, agent: str, tool: str, **args):
    error, result = await env.mcp(agent, tool, **args)
    assert not error, result
    return result


async def fails(env, agent: str, tool: str, code: str, **args) -> str:
    error, result = await env.mcp(agent, tool, **args)
    assert error, f"{tool} should have failed with {code}: {result}"
    assert f": {code}: " in f": {result}", result          # FastMCP prefixes "Error executing tool X: "
    return result


async def task(env, agent="claude", conversation="room:main", title="Write tests", **args) -> dict:
    return (await ok(env, agent, "create_task", conversation_id=conversation, title=title, **args))["task"]


async def act(env, agent: str, task_id: str, action: str, **args) -> dict:
    return (await ok(env, agent, "update_task", task_id=task_id, action=action, **args))["task"]


async def bodies(env, conversation: str, viewer=None) -> list[str]:
    viewer = viewer or env.hub.owner
    return [m.body for m in await env.hub.repo.messages(viewer, conversation, limit=500)]


async def turns(env, agent: str) -> list:
    return await env.hub.repo.turns("agent_id = ?", (agent,))
