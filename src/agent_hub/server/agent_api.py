"""The agent API as plain HTTP, for connectors and harnesses with no MCP client (design section 8.2)."""

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..core.models import HubError, Participant

KEEPALIVE = 15.0


def bearer_token(headers) -> str:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


class Post(BaseModel):
    body: str
    reply_to: str | None = None


class Trace(BaseModel):
    events: list[dict]


class Finish(BaseModel):
    text: str = ""
    usage: dict | None = None


class Fail(BaseModel):
    error: str = ""


class PermissionAsk(BaseModel):
    tool_name: str
    input: dict = {}
    tool_use_id: str | None = None


def build_agent_api(state) -> APIRouter:
    async def agent(request: Request) -> Participant:
        found = await state.hub.authenticate(bearer_token(request.headers))
        if found is None:
            raise HubError("unauthorized", "missing or wrong agent token", 401)
        return found

    router = APIRouter(prefix="/agent/v1")

    @router.get("/events")
    async def events(request: Request, me: Participant = Depends(agent)):
        """SSE stream of wake and cancel events for this agent's connector."""
        if me.self_driven:
            raise HubError("no_stream", "a self-driven agent has no event stream; poll list_conversations", 409)
        queue = await state.hub.connect(me)

        async def stream():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), KEEPALIVE)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"
                    if event["event"] == "replaced":
                        return
            finally:
                await asyncio.shield(state.hub.disconnect(me, queue))

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/turns/{turn_id}/start")
    async def start(turn_id: str, me: Participant = Depends(agent)):
        return await state.hub.turn_start(me, turn_id)

    @router.post("/turns/{turn_id}/trace")
    async def trace(turn_id: str, body: Trace, me: Participant = Depends(agent)):
        return await state.hub.turn_trace(me, turn_id, body.events)

    @router.post("/turns/{turn_id}/finish")
    async def finish(turn_id: str, body: Finish, me: Participant = Depends(agent)):
        return await state.hub.turn_finish(me, turn_id, body.text, body.usage)

    @router.post("/turns/{turn_id}/fail")
    async def fail(turn_id: str, body: Fail, me: Participant = Depends(agent)):
        return await state.hub.turn_fail(me, turn_id, body.error)

    @router.post("/turns/{turn_id}/permission")
    async def permission(turn_id: str, body: PermissionAsk, me: Participant = Depends(agent)):
        """Claude Code's permission prompt, answered by the owner in the UI. Blocks until answered."""
        return await state.hub.request_permission(me, turn_id, body.tool_name, body.input)

    # REST mirrors of the six tools.
    @router.get("/whoami")
    async def whoami(me: Participant = Depends(agent)):
        return await state.hub.whoami(me)

    @router.get("/participants")
    async def participants(me: Participant = Depends(agent)):
        return await state.hub.roster()

    @router.get("/conversations")
    async def conversations(me: Participant = Depends(agent)):
        return await state.hub.list_conversations(me)

    @router.get("/conversations/{conversation_id}/messages")
    async def read(conversation_id: str, after: str | None = None, limit: int = 50,
                   me: Participant = Depends(agent)):
        return await state.hub.read_messages(me, conversation_id, after, limit)

    @router.post("/conversations/{conversation_id}/messages")
    async def post(conversation_id: str, body: Post, me: Participant = Depends(agent)):
        await state.hub.record_tool_call(me, "post_message", {"conversation_id": conversation_id, "body": body.body})
        return await state.hub.post(me, conversation_id, body.body, body.reply_to)

    @router.post("/dm/{to}")
    async def dm(to: str, body: Post, me: Participant = Depends(agent)):
        return await state.hub.send_dm(me, to, body.body)

    return router
