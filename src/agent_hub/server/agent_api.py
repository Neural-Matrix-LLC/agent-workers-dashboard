"""The agent API as plain HTTP, for connectors and harnesses with no MCP client (design section 8.2)."""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..core.models import HubError, Participant
from .files import chunks_of, decode_content, download, read_upload

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


# Task bodies. Unknown fields (including sender, actor, creator) are ignored: identity is the token.
class TaskCreate(BaseModel):
    conversation_id: str
    title: Any = None
    description: Any = None
    acceptance: Any = None
    assignee: str | None = None
    parent_id: str | None = None
    priority: Any = "normal"
    labels: Any = None


class TaskUpdate(BaseModel):
    action: str
    note: Any = None
    result: Any = None
    blocked_reason: Any = None
    assignee: str | None = None
    fields: Any = None
    cascade: bool = False
    expected_version: int | None = None
    force: bool = False


class ArtifactJson(BaseModel):
    name: str
    content: Any
    encoding: str = "utf8"
    media_type: str | None = None
    description: Any = None
    supersedes: str | None = None


async def attach_from_request(service, actor, task_id: str, request: Request) -> JSONResponse:
    """POST .../artifacts: JSON (up to max_inline_bytes) or multipart/form-data (up to max_bytes)."""
    limits = service.file_limits
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        fields, upload = await read_upload(request, limits.max_bytes)
        try:
            result = await service.attach_stream(
                actor, task_id, name=fields.get("name") or upload.filename or "artifact", chunks=chunks_of(upload),
                media_type=fields.get("media_type") or upload.content_type, description=fields.get("description"),
                supersedes=fields.get("supersedes") or None)
        finally:
            await upload.close()
    else:
        try:
            body = ArtifactJson.model_validate(await request.json())
        except ValueError:                                  # bad JSON or missing fields
            raise HubError("bad_field", "send JSON with name and content, or a multipart upload") from None
        result = await service.attach_bytes(
            actor, task_id, name=body.name, data=decode_content(body.content, body.encoding),
            media_type=body.media_type, description=body.description, supersedes=body.supersedes)
    return JSONResponse(result, status_code=201)


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

    # ---------------------------------------------------------------- shared tasks (design M4 section 10.2)
    def tasks():
        return state.hub.task_service

    @router.get("/tasks")
    async def list_tasks(conversation_id: str | None = None, assignee: str | None = None,
                         state_: list[str] | None = Query(None, alias="state"), parent_id: str | None = None,
                         include_closed: bool = False, limit: int = 50, before: str | None = None,
                         me: Participant = Depends(agent)):
        return await tasks().list_tasks(me, conversation_id=conversation_id, assignee=assignee, state=state_,
                                         parent_id=parent_id, include_closed=include_closed, limit=limit,
                                         before=before)

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str, events_limit: int = 50, me: Participant = Depends(agent)):
        return await tasks().get(me, task_id, max(1, min(events_limit, 500)))

    @router.post("/tasks", status_code=201)
    async def create_task(body: TaskCreate, me: Participant = Depends(agent)):
        await state.hub.record_tool_call(me, "create_task", {"conversation_id": body.conversation_id})
        return await tasks().create(me, body.conversation_id, title=body.title, description=body.description,
                                    acceptance=body.acceptance, assignee=body.assignee, parent_id=body.parent_id,
                                    priority=body.priority, labels=body.labels)

    @router.post("/tasks/{task_id}/claim")
    async def claim_task(task_id: str, me: Participant = Depends(agent)):
        await state.hub.record_tool_call(me, "claim_task", {"task_id": task_id})
        return await tasks().claim(me, task_id)

    @router.patch("/tasks/{task_id}")
    async def update_task(task_id: str, body: TaskUpdate, me: Participant = Depends(agent)):
        await state.hub.record_tool_call(me, "update_task", {"task_id": task_id, "action": body.action})
        return await tasks().update(me, task_id, body.action, note=body.note, result=body.result,
                                    blocked_reason=body.blocked_reason, assignee=body.assignee, fields=body.fields,
                                    cascade=body.cascade, expected_version=body.expected_version)

    @router.post("/tasks/{task_id}/artifacts")
    async def attach(task_id: str, request: Request, me: Participant = Depends(agent)):
        await state.hub.record_tool_call(me, "attach_artifact", {"task_id": task_id})
        return await attach_from_request(tasks(), me, task_id, request)

    @router.get("/artifacts/{artifact_id}")
    async def artifact_meta(artifact_id: str, me: Participant = Depends(agent)):
        return await tasks().artifact_meta(me, artifact_id)

    @router.get("/artifacts/{artifact_id}/content")
    async def artifact_content(artifact_id: str, request: Request, me: Participant = Depends(agent)):
        artifact, data = await tasks().artifact_content(me, artifact_id)
        return download(artifact, data, request.headers.get("range"))

    return router
