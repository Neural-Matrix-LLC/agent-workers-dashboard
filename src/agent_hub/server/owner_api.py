"""The owner API (design section 8.3). Every route needs the owner's session cookie."""

import asyncio
import hashlib
import hmac
import os
import time
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import Budget
from ..core.models import HubError
from .agent_api import TaskCreate, TaskUpdate, attach_from_request
from .files import download

COOKIE = "hub_session"
SESSION_SECONDS = 30 * 24 * 3600


def sign(secret: str, expires: int) -> str:
    return f"{expires}.{hmac.new(secret.encode(), str(expires).encode(), hashlib.sha256).hexdigest()}"


def valid_session(secret: str, cookie: str | None) -> bool:
    expires, _, _ = (cookie or "").partition(".")
    return expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(sign(secret, int(expires)), cookie)


class Login(BaseModel):
    password: str


class Post(BaseModel):
    body: str
    reply_to: str | None = None


class Mode(BaseModel):
    mode: Literal["mention", "round_robin", "moderated"]
    fanout: Literal["sequential", "parallel"] | None = None


class AgentMode(BaseModel):
    mode: str


class Settings(BaseModel):
    approval_mode: bool


class RoomCreate(BaseModel):
    id: str
    title: str | None = None
    mode: Literal["mention", "round_robin", "moderated"] = "mention"
    fanout: Literal["sequential", "parallel"] = "sequential"
    moderator: str | None = None
    members: list[str] | None = None


class RoomPatch(BaseModel):
    title: str | None = None
    mode: Literal["mention", "round_robin", "moderated"] | None = None
    fanout: Literal["sequential", "parallel"] | None = None
    moderator: str | None = None


class RoomMembers(BaseModel):
    members: list[str]


def build_owner_api(state) -> APIRouter:
    def owner(request: Request) -> None:
        if not valid_session(state.secret, request.cookies.get(COOKIE)):
            raise HubError("unauthorized", "log in first", 401)

    public = APIRouter(prefix="/api")
    router = APIRouter(prefix="/api", dependencies=[Depends(owner)])

    @public.post("/login")
    async def login(body: Login, request: Request, response: Response):
        password = os.environ.get("HUB_OWNER_PASSWORD", "")
        if not password or not hmac.compare_digest(body.password.encode(), password.encode()):
            await asyncio.sleep(1)                       # slows down guessing
            raise HubError("unauthorized", "wrong password", 401)
        response.set_cookie(COOKIE, sign(state.secret, int(time.time()) + SESSION_SECONDS), httponly=True,
                            samesite="strict", secure=request.url.scheme == "https", max_age=SESSION_SECONDS)
        return {"ok": True}

    @public.post("/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @router.get("/me")
    async def me():
        hub = state.hub
        return {"owner": {"id": hub.owner.id, "display_name": hub.owner.display_name},
                "settings": hub.settings_view(), "participants": await hub.roster()}

    @router.get("/conversations")
    async def conversations():
        hub = state.hub
        counts = await hub.repo.counts(hub.owner)
        return [hub.room_view(c, counts) for c in await hub.repo.conversations(hub.owner)]

    @router.get("/conversations/{conversation_id}/messages")
    async def messages(conversation_id: str, before: str | None = None, limit: int = 100):
        hub = state.hub
        conversation = await hub.repo.conversation(hub.owner, conversation_id)
        found = await hub.repo.messages(hub.owner, conversation.id, before=before, limit=limit, latest=True)
        roles = await hub._roles()
        return [hub.message_view(m, roles, owner=True) for m in found]

    @router.post("/conversations/{conversation_id}/messages")
    async def post(conversation_id: str, body: Post):
        return await state.hub.post(state.hub.owner, conversation_id, body.body, body.reply_to)

    @router.get("/activity")
    async def activity(before: str | None = None, limit: int = 200):
        hub = state.hub
        roles = await hub._roles()
        return [hub.message_view(m, roles, owner=True)
                for m in await hub.repo.activity(hub.owner, before=before, limit=limit)]

    @router.get("/agents")
    async def agents():
        return await state.hub.agents_view()

    @router.get("/turns")
    async def turns(limit: int = 50):
        return [state.hub.turn_view(t) for t in await state.hub.repo.recent_turns(min(limit, 200))]

    @router.get("/turns/{turn_id}/trace")
    async def trace(turn_id: str):
        turn = await state.hub.repo.turn(turn_id)
        if turn is None:
            raise HubError("unknown_turn", f"unknown turn: {turn_id}", 404)
        return {"turn": state.hub.turn_view(turn), "events": await state.hub.repo.trace(turn_id)}

    @router.post("/turns/{turn_id}/{decision}")
    async def decide(turn_id: str, decision: Literal["approve", "reject"]):
        await state.hub.decide_turn(turn_id, decision == "approve")
        return {"ok": True}

    @router.post("/agents/{agent_id}/{action}")
    async def control(agent_id: str, action: Literal["pause", "resume", "stop"]):
        """`all` as the agent id pauses, resumes, or stops every agent."""
        hub, everyone = state.hub, agent_id == "all"
        if action == "stop":
            return {"cancelled": await hub.stop(None if everyone else agent_id)}
        if everyone:
            await hub.pause_all(action == "pause")
        else:
            await hub.pause(agent_id, action == "pause")
        return {"ok": True}

    @router.put("/agents/{agent_id}/budget")
    async def budget(agent_id: str, body: Budget):
        await state.hub.set_budget(agent_id, body.model_dump(exclude_none=True))
        return {"ok": True}

    @router.put("/agents/{agent_id}/mode")
    async def agent_mode(agent_id: str, body: AgentMode):
        await state.hub.set_agent_mode(agent_id, body.mode)
        return {"ok": True}

    @router.post("/permissions/{request_id}/{decision}")
    async def permission(request_id: str, decision: Literal["allow", "deny"]):
        await state.hub.decide_permission(request_id, decision == "allow")
        return {"ok": True}

    @router.put("/conversations/{conversation_id}/mode")
    async def mode(conversation_id: str, body: Mode):
        await state.hub.set_mode(conversation_id, body.mode, body.fanout)
        return {"ok": True}

    @router.post("/notices/{notice_id}/continue")
    async def continue_chain(notice_id: str):
        await state.hub.continue_chain(notice_id)
        return {"ok": True}

    @router.put("/settings")
    async def settings(body: Settings):
        await state.hub.set_approval_mode(body.approval_mode)
        return state.hub.settings_view()

    # ---------------------------------------------------------------- task board (design M4 section 10.3)
    def tasks():
        return state.hub.task_service

    @router.get("/tasks")
    async def list_tasks(conversation_id: str | None = None, assignee: str | None = None,
                         state_: list[str] | None = Query(None, alias="state"), parent_id: str | None = None,
                         include_closed: bool = True, q: str | None = None, limit: int = 200,
                         before: str | None = None):
        hub = state.hub
        page = await tasks().list_tasks(hub.owner, conversation_id=conversation_id, assignee=assignee,
                                        state=state_, parent_id=parent_id, include_closed=include_closed,
                                        limit=limit, before=before, q=q, max_limit=500)
        return {**page, "counts": await hub.repo.task_counts(hub.owner)}

    @router.get("/tasks/{task_id}")
    async def get_task(task_id: str, events_limit: int = 200):
        return await tasks().get(state.hub.owner, task_id, max(1, min(events_limit, 500)))

    @router.post("/tasks", status_code=201)
    async def create_task(body: TaskCreate):
        return await tasks().create(state.hub.owner, body.conversation_id, title=body.title,
                                    description=body.description, acceptance=body.acceptance,
                                    assignee=body.assignee, parent_id=body.parent_id, priority=body.priority,
                                    labels=body.labels)

    @router.patch("/tasks/{task_id}")
    async def update_task(task_id: str, body: TaskUpdate):
        return await tasks().update(state.hub.owner, task_id, body.action, note=body.note, result=body.result,
                                    blocked_reason=body.blocked_reason, assignee=body.assignee, fields=body.fields,
                                    cascade=body.cascade, expected_version=body.expected_version, force=body.force)

    @router.post("/tasks/{task_id}/artifacts")
    async def attach(task_id: str, request: Request) -> JSONResponse:
        return await attach_from_request(tasks(), state.hub.owner, task_id, request)

    @router.get("/artifacts/{artifact_id}")
    async def artifact_meta(artifact_id: str):
        return await tasks().artifact_meta(state.hub.owner, artifact_id)

    @router.get("/artifacts/{artifact_id}/content")
    async def artifact_content(artifact_id: str, request: Request):
        artifact, data = await tasks().artifact_content(state.hub.owner, artifact_id)
        return download(artifact, data, request.headers.get("range"))

    @router.delete("/artifacts/{artifact_id}")
    async def delete_artifact(artifact_id: str):
        return await tasks().delete_artifact(artifact_id)

    # ---------------------------------------------------------------- rooms (design M5d section 21.4)
    @router.post("/rooms", status_code=201)
    async def create_room(body: RoomCreate):
        return await state.hub.create_room(body.id, body.title, body.mode, body.fanout, body.moderator,
                                           body.members)

    @router.patch("/rooms/{room_id}")
    async def update_room(room_id: str, body: RoomPatch):
        # Only the fields the request sent; "moderator": null clears the moderator.
        return await state.hub.update_room(room_id, {k: getattr(body, k) for k in body.model_fields_set})

    @router.put("/rooms/{room_id}/members")
    async def room_members(room_id: str, body: RoomMembers):
        return await state.hub.set_room_members(room_id, body.members)

    @router.post("/rooms/{room_id}/archive")
    async def archive_room(room_id: str):
        return await state.hub.archive_room(room_id)

    return public, router
