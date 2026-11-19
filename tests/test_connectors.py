"""The connector side: the shared loop in connectors/base.py, and the hermes and openai-compat connectors run
against a real hub with a stand-in model server."""

import asyncio
import json

import httpx
import pytest
from conftest import TOKENS

from agent_hub.config import AgentConfig
from agent_hub.connectors.base import Connector, TraceSink, TurnResult, scrubbed_env
from agent_hub.connectors.hermes import HermesConnector
from agent_hub.connectors.openai_compat import OpenAICompatConnector
from agent_hub.core.delivery import render_prompt


def route_new_clients(monkeypatch, handler):
    """Every httpx.AsyncClient made without a transport (the connector's model client) goes to `handler`."""
    real = httpx.AsyncClient

    def make(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", make)


def connector_for(env, cls, agent: dict):
    connector = cls(AgentConfig.model_validate(agent), "http://hub", TOKENS[agent["id"]])
    connector.client = env.client(agent["id"])
    return connector


async def run_one_turn(env, connector, conversation: str, body: str):
    """The owner posts; the connector runs the wake it gets. Returns the wake and the finished turn."""
    queue = await env.hub.connect(await env.agent(connector.agent.id))
    await env.owner_post(conversation, body)
    wake = queue.get_nowait()["data"]
    await connector._turn(wake)
    return wake, await env.hub.repo.turn(wake["turn_id"])


async def bodies(env, conversation: str) -> list[tuple[str, str]]:
    return [(m.sender_id, m.body) for m in await env.hub.repo.messages(env.hub.owner, conversation)]


def completion(content="", tool_calls=None, prompt_tokens=10, completion_tokens=2) -> httpx.Response:
    message = {"role": "assistant", "content": content, **({"tool_calls": tool_calls} if tool_calls else {})}
    return httpx.Response(200, json={"choices": [{"message": message}],
                                     "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}})


def call(id: str, name: str, arguments) -> dict:
    return {"id": id, "type": "function",
            "function": {"name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}


# ------------------------------------------------------------------ hermes
HERMES = {"id": "hermes", "connector": "hermes", "api_base": "http://hermes.test/v1/", "description": "Research."}


@pytest.mark.parametrize("session_id", [None, "qa-session"])
async def test_hermes_sends_the_prompt_in_one_session_and_posts_the_reply(hub_env, monkeypatch, session_id):
    monkeypatch.setenv("HERMES_API_KEY", "hermes-key")
    seen = []

    def model(request):
        seen.append(request)
        return completion("Found it.", prompt_tokens=11, completion_tokens=3)

    agent = {**HERMES, **({"session_id": session_id} if session_id else {})}
    async with hub_env(agents=[agent]) as env:
        route_new_clients(monkeypatch, model)
        wake, turn = await run_one_turn(env, connector_for(env, HermesConnector, agent), "dm:hermes:owner", "find x")

        request = seen[0]
        assert str(request.url) == "http://hermes.test/v1/chat/completions"
        assert request.headers["x-hermes-session-id"] == (session_id or "agent-hub-hermes")
        assert request.headers["authorization"] == "Bearer hermes-key"
        sent = json.loads(request.content)
        prompt = render_prompt("hermes", wake)
        assert sent == {"model": "hermes-agent", "messages": [{"role": "user", "content": prompt}]}

        assert (turn.state, turn.tokens_in, turn.tokens_out) == ("done", 11, 3)
        assert (await bodies(env, "dm:hermes:owner"))[-1] == ("hermes", "Found it.")
        assert [e["type"] for e in await env.hub.repo.trace(turn.id)] == ["usage"]


async def test_hermes_empty_answer_stays_silent(hub_env, monkeypatch):
    async with hub_env(agents=[HERMES]) as env:
        route_new_clients(monkeypatch, lambda request: completion(""))
        _, turn = await run_one_turn(env, connector_for(env, HermesConnector, HERMES), "dm:hermes:owner", "hi")
        assert turn.state == "done"
        assert await bodies(env, "dm:hermes:owner") == [("owner", "hi")]


async def test_hermes_api_error_fails_the_turn_with_the_reason(hub_env, monkeypatch):
    async with hub_env(agents=[HERMES]) as env:
        route_new_clients(monkeypatch, lambda request: httpx.Response(503, text="model busy"))
        _, turn = await run_one_turn(env, connector_for(env, HermesConnector, HERMES), "dm:hermes:owner", "hi")
        assert turn.state == "failed"
        assert "Hermes API returned 503" in turn.error and "model busy" in turn.error


async def test_hermes_unreachable_fails_the_turn(hub_env, monkeypatch):
    def down(request):
        raise httpx.ConnectError("connection refused")

    async with hub_env(agents=[HERMES]) as env:
        route_new_clients(monkeypatch, down)
        _, turn = await run_one_turn(env, connector_for(env, HermesConnector, HERMES), "dm:hermes:owner", "hi")
        assert turn.state == "failed" and turn.error.startswith("ConnectError")


# ------------------------------------------------------------------ openai-compat
LOCAL = {"id": "local", "connector": "openai-compat", "api_base": "http://llm.test/v1", "model": "small",
         "description": "A local model."}
CLAUDE = {"id": "claude", "connector": "echo"}


async def test_openai_compat_runs_hub_tools_and_answers(hub_env, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_STATE_DIR", str(tmp_path / "state"))
    requests = []

    def model(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return completion(tool_calls=[
                call("c1", "send_dm", {"to": "claude", "body": "can you check x?"}),
                call("c2", "no_such_tool", {}),
                call("c3", "read_messages", "{not json"),
            ])
        return completion("Asked claude.", prompt_tokens=20, completion_tokens=4)

    async with hub_env(agents=[LOCAL, CLAUDE]) as env:
        route_new_clients(monkeypatch, model)
        connector = connector_for(env, OpenAICompatConnector, LOCAL)
        _, turn = await run_one_turn(env, connector, "dm:local:owner", "ask claude about x")

        assert (turn.state, turn.tokens_in, turn.tokens_out) == ("done", 30, 6)
        assert (await bodies(env, "dm:local:owner"))[-1] == ("local", "Asked claude.")
        assert await bodies(env, "dm:claude:local") == [("local", "can you check x?")]

        first, second = requests
        assert first["model"] == "small"
        assert first["messages"][0]["role"] == "system" and '"local"' in first["messages"][0]["content"]
        assert {t["function"]["name"] for t in first["tools"]} == {
            "whoami", "list_participants", "list_conversations", "read_messages", "post_message", "send_dm",
            "create_task", "claim_task", "update_task", "list_tasks", "get_task", "attach_artifact",
            "read_artifact"}
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in second["messages"] if m["role"] == "tool"}
        assert results["c1"]["conversation_id"] == "dm:claude:local"
        assert "unknown tool" in results["c2"]["error"]
        assert results["c3"]["error"].startswith("JSONDecodeError")
        traced = [e for e in await env.hub.repo.trace(turn.id) if e["type"] == "tool_call"]
        assert [e["payload"]["tool"] for e in traced] == ["send_dm", "no_such_tool", "read_messages"]

        # The history is kept between turns.
        requests.clear()
        model_reply = completion("Still here.")
        route_new_clients(monkeypatch, lambda request: (requests.append(json.loads(request.content)), model_reply)[1])
        queue = await env.hub.connect(await env.agent("local"))
        await env.owner_post("dm:local:owner", "anything else?")
        await connector._turn(queue.get_nowait()["data"])
        contents = [m.get("content") for m in requests[0]["messages"]]
        assert any("ask claude about x" in (c or "") for c in contents)
        assert contents[-1].count("anything else?") == 1


async def test_openai_compat_last_round_must_answer_in_text(hub_env, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_STATE_DIR", str(tmp_path / "state"))
    requests = []

    def model(request):
        sent = json.loads(request.content)
        requests.append(sent)
        if "tools" in sent:
            return completion(tool_calls=[call(f"c{len(requests)}", "whoami", {})])
        return completion("final")

    agent = {**LOCAL, "max_tool_rounds": 1}
    async with hub_env(agents=[agent]) as env:
        route_new_clients(monkeypatch, model)
        _, turn = await run_one_turn(env, connector_for(env, OpenAICompatConnector, agent), "dm:local:owner", "go")
        assert ["tools" in r for r in requests] == [True, False]
        assert (await bodies(env, "dm:local:owner"))[-1] == ("local", "final")


def test_openai_compat_trim_never_starts_with_a_tool_result():
    history = [{"role": "user"}, {"role": "assistant"}, {"role": "tool"}, {"role": "user"}, {"role": "assistant"}]
    assert OpenAICompatConnector._trim(history, 4) == [{"role": "user"}, {"role": "assistant"}]
    assert OpenAICompatConnector._trim(history, 10) == history


# ------------------------------------------------------------------ the shared loop
WAKE = {"turn_id": "T1", "conversation": {"id": "dm:x:owner", "kind": "dm", "members": ["owner", "x"]},
        "messages": [{"id": "M1", "from": "owner", "role": "owner", "body": "hello"}],
        "trigger_message_id": "M1", "hop": 0}


def sse(*events) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class Recorder(Connector):
    name = "recorder"

    def __init__(self, *args, fail=False, block=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail, self.block, self.cancelled, self.started = fail, block, [], asyncio.Event()

    async def run_turn(self, wake, trace):
        self.started.set()
        await trace.emit("tool_call", {"tool": "Read"})
        if self.block:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("harness crashed")
        return TurnResult("hi", 0.25, 5, 6)

    async def cancel(self, turn_id):
        self.cancelled.append(turn_id)


def recorder(handler, **kwargs) -> Recorder:
    connector = Recorder(AgentConfig(id="x"), "http://hub", "tok", **kwargs)
    connector.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://hub")
    return connector


class FakeHub:
    """A scripted /agent/v1/events stream plus a record of the turn calls."""

    def __init__(self, *streams):
        self.streams, self.calls = list(streams), []

    async def __call__(self, request):
        if request.url.path == "/agent/v1/events":
            status, body = self.streams.pop(0)
            return httpx.Response(status, content=body)
        self.calls.append((request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={})


async def wait_for_call(hub: FakeHub, suffix: str):
    for _ in range(200):
        for path, body in hub.calls:
            if path.endswith(suffix):
                return body
        await asyncio.sleep(0.005)
    raise AssertionError(f"no call to {suffix}: {hub.calls}")


async def test_loop_runs_a_wake_and_posts_trace_and_reply():
    connector = recorder(FakeHub())
    hub = connector.client._transport.handler
    await connector._turn(WAKE)
    paths = [path for path, _ in hub.calls]
    assert paths == ["/agent/v1/turns/T1/start", "/agent/v1/turns/T1/trace", "/agent/v1/turns/T1/finish"]
    assert hub.calls[1][1] == {"events": [{"type": "tool_call", "payload": {"tool": "Read"}}]}
    assert hub.calls[2][1] == {"text": "hi", "usage": {"cost_usd": 0.25, "tokens_in": 5, "tokens_out": 6}}


async def test_loop_reports_a_harness_crash_as_a_failed_turn():
    connector = recorder(FakeHub(), fail=True)
    hub = connector.client._transport.handler
    await connector._turn(WAKE)
    assert [path for path, _ in hub.calls][-1] == "/agent/v1/turns/T1/fail"
    assert hub.calls[-1][1] == {"error": "RuntimeError: harness crashed"}
    assert hub.calls[-2][0].endswith("/trace")              # the trace so far is still sent


async def test_serve_exits_when_the_hub_refuses_the_token():
    connector = recorder(FakeHub((401, b'{"code": "unauthorized"}')))
    with pytest.raises(SystemExit, match="refused the connection \\(401\\)"):
        await connector.serve()


async def test_serve_runs_wakes_and_exits_when_replaced():
    async def stream():
        yield sse(("wake", WAKE))
        await asyncio.sleep(0.1)
        yield sse(("replaced", {}))

    hub = FakeHub((200, stream()))
    connector = recorder(hub)
    with pytest.raises(SystemExit, match="another connector"):
        await connector.serve()
    assert (await wait_for_call(hub, "/finish"))["text"] == "hi"


async def test_serve_cancel_event_stops_the_turn():
    connector = None

    async def stream():
        yield sse(("wake", WAKE))
        await connector.started.wait()
        yield sse(("cancel", {"turn_id": "T1"}))
        await asyncio.sleep(0.05)
        yield sse(("replaced", {}))

    hub = FakeHub((200, stream()))
    connector = recorder(hub, block=True)
    with pytest.raises(SystemExit):
        await connector.serve()
    assert connector.cancelled == ["T1"] and connector.turns == {}
    assert not any(path.endswith(("/finish", "/fail")) for path, _ in hub.calls)


async def test_serve_reconnects_after_the_stream_drops(monkeypatch):
    real_sleep, delays = asyncio.sleep, []

    async def fast_sleep(delay, *args):
        delays.append(delay)
        await real_sleep(0)

    hub = FakeHub((200, b": connected\n\n"), (502, b""), (401, b"gone"))
    connector = recorder(hub)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    with pytest.raises(SystemExit, match="401"):
        await connector.serve()
    assert delays == [1.0, 2.0]                             # backs off after each failed attempt


async def test_trace_sink_batches_until_ten_or_a_tool_call():
    hub = FakeHub()
    client = httpx.AsyncClient(transport=httpx.MockTransport(hub), base_url="http://hub")
    sink = TraceSink(client, "T1")
    for i in range(9):
        await sink.emit("text", i)
    assert hub.calls == []
    await sink.emit("text", 9)
    assert len(hub.calls[0][1]["events"]) == 10
    await sink.emit("tool_call", {"tool": "Bash"})
    assert len(hub.calls) == 2
    await sink.flush()
    assert len(hub.calls) == 2                              # nothing left to send


def test_harness_environment_has_no_hub_secrets(monkeypatch):
    for key in ("HUB_CLAUDE_TOKEN", "HUB_OWNER_PASSWORD", "GROK_WEBHOOK_KEY", "HERMES_API_KEY"):
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("ANTHROPIC_MODEL", "keep-me")
    env = scrubbed_env()
    assert not {"HUB_CLAUDE_TOKEN", "HUB_OWNER_PASSWORD", "GROK_WEBHOOK_KEY", "HERMES_API_KEY"} & env.keys()
    assert env["ANTHROPIC_MODEL"] == "keep-me"
