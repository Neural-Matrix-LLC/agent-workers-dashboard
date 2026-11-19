"""Shared tasks and file artifacts (design M4). Every method runs under `hub.lock`.

A task lives in one home conversation and has that conversation's visibility. Every change appends a
`task` message from the system participant `hub` to the home conversation (with the acting participant
in `actor_id`) and a row in `task_events`. Wakes are decided by `router.decide_task`, never by parsing
text, and go through the hub's usual brakes: hop limit, approval mode, rate limit, budgets, one turn at
a time per agent.
"""

import asyncio
import base64
import logging
from typing import TYPE_CHECKING

from ..ids import now
from . import artifact_rules, router, task_rules
from .artifacts import ArtifactStore, TempFile
from .models import (
    TASK_STATES,
    TERMINAL,
    Artifact,
    Conversation,
    HubError,
    Message,
    Participant,
    Task,
    TaskEvent,
    artifact_not_found,
    task_not_found,
)
from .visibility import can_post

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("agent_hub.tasks")

READ_CHUNK = 262_144
MAX_ARTIFACT_DESCRIPTION = 500


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class TaskService:
    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.store = ArtifactStore(hub.config.hub.artifacts_path)

    @property
    def repo(self):
        return self.hub.repo

    @property
    def limits(self):
        return self.hub.config.limits.tasks

    @property
    def file_limits(self):
        return self.hub.config.limits.artifacts

    async def start(self) -> None:
        await asyncio.to_thread(self.store.ensure)
        await self.gc()

    async def gc(self) -> int:
        removed = await asyncio.to_thread(self.store.gc, await self.repo.live_hashes())
        if removed:
            log.info("artifact store: removed %d unreferenced files", removed)
        return removed

    # ------------------------------------------------------------------ views (filtered for the viewer)
    async def view(self, viewer: Participant, task: Task) -> dict:
        parent_id = task.parent_id if task.parent_id and await self.repo.task(viewer, task.parent_id) else None
        kids = await self.repo.children(viewer, task.id)
        children = {"total": len(kids), "open": sum(1 for k in kids if not k.terminal),
                    **{state: sum(1 for k in kids if k.state == state) for state in TERMINAL}}
        return {"id": task.id, "ref": task.ref, "number": task.number, "conversation_id": task.conversation_id,
                "parent_id": parent_id, "depth": task.depth, "title": task.title, "description": task.description,
                "acceptance": task.acceptance, "priority": task.priority, "labels": task.labels,
                "creator": task.creator_id, "assignee": task.assignee_id, "state": task.state,
                "result": task.result, "blocked_reason": task.blocked_reason, "children": children,
                "artifact_count": await self.repo.artifact_count(viewer, task.id),
                "reassignments": task.reassignments, "version": task.version, "created_at": task.created_at,
                "updated_at": task.updated_at, "closed_at": task.closed_at}

    @staticmethod
    def event_view(event: TaskEvent, *, owner: bool = False) -> dict:
        data = event.data if owner else {k: v for k, v in event.data.items() if k != "private"}
        return {"id": event.id, "type": event.type, "actor": event.actor_id, "from_state": event.from_state,
                "to_state": event.to_state, "note": event.note, "data": data, "message_id": event.message_id,
                "hop": event.hop, "at": event.at}

    async def artifact_view(self, artifact: Artifact, *, owner: bool = False) -> dict:
        task = await self.repo.raw_task(artifact.task_id)
        view = {"id": artifact.id, "task_id": artifact.task_id, "task_ref": task.ref if task else None,
                "name": artifact.name, "description": artifact.description, "media_type": artifact.media_type,
                "size": artifact.size, "sha256": artifact.sha256, "uploader": artifact.uploader_id,
                "supersedes": artifact.supersedes, "created_at": artifact.created_at}
        if owner:
            view["deleted_at"] = artifact.deleted_at
        return view

    async def owner_view(self, task: Task) -> dict:
        return await self.view(self.hub.owner, task)

    # ------------------------------------------------------------------ shared checks
    async def _load(self, viewer: Participant, task_id: str) -> Task:
        task = await self.repo.task(viewer, str(task_id))
        if task is None:
            raise task_not_found(task_id)
        return task

    async def _coordinator(self, task: Task) -> str | None:
        if not task.parent_id:
            return None
        parent = await self.repo.raw_task(task.parent_id)
        return parent.assignee_id if parent else None

    async def _home(self, actor: Participant, task: Task) -> Conversation:
        conversation = await self.repo.conversation(actor, task.conversation_id)
        if conversation.archived_at:
            raise HubError("archived", f"{conversation.id} is archived", 409)
        return conversation

    def _check_task_tools(self, actor: Participant) -> None:
        if not actor.is_owner and not self.hub.config.agent(actor.id).task_tools:
            raise HubError("not_enabled", "task tools are turned off for you", 403)

    @staticmethod
    def _moderation_only(actor: Participant, conversation: Conversation) -> bool:
        """The owner is read-only in agent-to-agent DMs, except for moderation actions (OD-3)."""
        return actor.is_owner and actor.id not in conversation.members

    async def _scan_text(self, values: dict) -> None:
        tokens = await self.repo.token_hashes()
        for name, value in values.items():
            if isinstance(value, str) and value:
                hit = artifact_rules.scan(value, tokens)
                if hit:
                    raise HubError("secret_detected", f"{name} looks like it contains a secret (rule: {hit[0]}, "
                                                      f"line {hit[1]}); nothing was stored", 422)

    async def _check_assignee(self, conversation: Conversation, assignee: str) -> None:
        if not isinstance(assignee, str) or assignee not in conversation.members \
                or await self.repo.participant(assignee) is None:
            raise HubError("bad_assignee", f"the assignee must be a member of {conversation.id}")

    async def _check_open_limit(self, assignee: str | None) -> None:
        if not assignee or assignee == "owner":
            return
        if await self.repo.count_open_assigned(assignee) >= self.limits.max_open_per_agent:
            raise HubError("too_many_tasks",
                           f"{assignee} already has {self.limits.max_open_per_agent} open tasks", 429)

    async def _agents_in(self, conversation: Conversation) -> list[str]:
        return await self.hub._agent_members(conversation)

    # ------------------------------------------------------------------ the event: message, row, wakes
    async def _event(self, task: Task, conversation: Conversation, actor: Participant, type: str, hop: int,
                     turn_id: str | None, text: str, *, wake: str | None = None, from_state: str | None = None,
                     to_state: str | None = None, note: str | None = None, data: dict | None = None,
                     previous_assignee: str | None = None, note_mentions: list[str] = (),
                     exclude: list[str] = ()) -> Message:
        """Store the task message and the event row, emit them to the owner, and wake the targets."""
        hub = self.hub
        agents = await self._agents_in(conversation)
        targets = router.decide_task(
            event=wake or type, actor=actor.id, creator=task.creator_id, assignee=task.assignee_id,
            previous_assignee=previous_assignee, coordinator=await self._coordinator(task), to_state=to_state,
            note_mentions=list(note_mentions), agents=agents, wake_creator_on=self.limits.wake_creator_on,
            exclude=list(exclude)).targets
        message = await self.repo.add_message(
            conversation.id, "hub", text, kind="task", hop=hop, turn_id=turn_id, task_id=task.id,
            actor_id=actor.id, meta={"event": wake or type, "state": task.state, "ref": task.ref,
                                     "targets": targets})
        await self.repo.add_task_event(task.id, actor.id, type, hop=hop, from_state=from_state, to_state=to_state,
                                       note=note, data=data, message_id=message.id)
        await hub._emit_message(message)
        hub._emit("task", await self.owner_view(task))
        await hub._route_task(message, conversation, actor, hop, targets, task.id)
        return message

    async def _act(self, actor: Participant, conversation: Conversation):
        """(actor, hop, turn id, self-started) for a task write. Raises paused, stopped, rate_limited."""
        actor, hop, _turn, turn_id, self_started = await self.hub._actor_hop(actor, conversation)
        if not actor.is_owner and not self_started:
            # Design M4 section 8.1: inside an open turn in the home conversation, every task event of
            # that turn gets the turn's hop + 1, like the turn's reply.
            own = await self.repo.open_turn(actor.id, conversation.id)
            if own is not None:
                hop, turn_id = own.hop + 1, own.id
        return actor, hop, turn_id, self_started

    async def _finish_act(self, actor: Participant, conversation: Conversation, message: Message, hop: int,
                          self_started: bool) -> None:
        if self_started:
            await self.hub._record_self_started(actor, conversation.id, message, hop)

    # ------------------------------------------------------------------ create
    async def create(self, actor: Participant, conversation_id: str, *, title, description=None, acceptance=None,
                     assignee=None, parent_id=None, priority="normal", labels=None) -> dict:
        async with self.hub.lock:
            result = await self._create(actor, conversation_id, title=title, description=description,
                                        acceptance=acceptance, assignee=assignee, parent_id=parent_id,
                                        priority=priority, labels=labels)
            await self.hub._flush()
            return result

    async def _create(self, actor, conversation_id, *, title, description, acceptance, assignee, parent_id,
                      priority, labels) -> dict:
        self._check_task_tools(actor)
        conversation = await self.repo.conversation(actor, str(conversation_id))
        if conversation.archived_at:
            raise HubError("archived", f"{conversation.id} is archived", 409)
        if not can_post(actor, conversation):
            raise HubError("read_only", f"you are not a member of {conversation.id}", 403)
        title = task_rules.check_text("title", title, required=True).strip()
        description = task_rules.check_text("description", description) or ""
        acceptance = task_rules.check_text("acceptance", acceptance)
        priority = task_rules.check_priority(priority or "normal")
        labels = task_rules.check_labels(labels)
        await self._scan_text({"title": title, "description": description, "acceptance": acceptance})
        if assignee is not None:
            await self._check_assignee(conversation, assignee)
        depth = 0
        if parent_id:
            parent = await self._load(actor, parent_id)
            if not (actor.is_owner or actor.id in (parent.creator_id, parent.assignee_id)):
                raise HubError("not_allowed", "only the parent's creator, its assignee, or the owner may add "
                                              "subtasks", 403)
            depth = parent.depth + 1
            if depth > self.limits.max_depth:
                raise HubError("depth_exceeded", f"subtasks may be at most {self.limits.max_depth} levels deep")
            if len(await self.repo.raw_children(parent.id)) >= self.limits.max_children:
                raise HubError("too_many_children",
                               f"{parent.ref} already has {self.limits.max_children} subtasks", 409)
            parent_id = parent.id
        if await self.repo.count_open_in(conversation.id) >= self.limits.max_open_per_conversation:
            raise HubError("too_many_tasks", f"{conversation.id} already has "
                                             f"{self.limits.max_open_per_conversation} open tasks", 429)
        await self._check_open_limit(assignee)
        actor, hop, turn_id, self_started = await self._act(actor, conversation)
        if turn_id and await self.repo.count_task_creations(turn_id) >= self.limits.max_creates_per_turn:
            raise HubError("too_many_tasks",
                           f"you may create at most {self.limits.max_creates_per_turn} tasks in one turn", 429)
        task = await self.repo.add_task(
            conversation_id=conversation.id, parent_id=parent_id, depth=depth, title=title, description=description,
            acceptance=acceptance, priority=priority, labels=labels, creator_id=actor.id, assignee_id=assignee,
            state="assigned" if assignee else "open", hop=hop)
        text = f'{actor.id} created {task.ref} "{_clip(title, 120)}"'
        if assignee:
            text += f", assigned to @{assignee}"
        message = await self._event(task, conversation, actor, "created", hop, turn_id, text,
                                    to_state=task.state, data={"assignee": assignee} if assignee else {})
        await self._finish_act(actor, conversation, message, hop, self_started)
        return {"task": await self.view(actor, task), "message_id": message.id}

    # ------------------------------------------------------------------ claim
    async def claim(self, actor: Participant, task_id: str) -> dict:
        async with self.hub.lock:
            self._check_task_tools(actor)
            task = await self._load(actor, task_id)
            conversation = await self._home(actor, task)
            if not can_post(actor, conversation):
                raise HubError("read_only", f"you are not a member of {conversation.id}", 403)
            if task.assignee_id:
                raise HubError("already_claimed", f"{task.ref} is already assigned to {task.assignee_id}", 409)
            if task.state != "open":
                raise HubError("bad_transition", f"cannot claim a task that is {task.state}", 409)
            await self._check_open_limit(actor.id)
            actor, hop, turn_id, self_started = await self._act(actor, conversation)
            if not await self.repo.claim_task_row(task.id, actor.id, hop):
                current = await self.repo.raw_task(task.id)
                raise HubError("already_claimed", f"{task.ref} is already assigned to {current.assignee_id}", 409)
            task = await self.repo.raw_task(task.id)
            message = await self._event(task, conversation, actor, "claimed", hop, turn_id,
                                        f'{actor.id} claimed {task.ref} "{_clip(task.title, 120)}"',
                                        from_state="open", to_state="assigned", data={"assignee": actor.id})
            await self._finish_act(actor, conversation, message, hop, self_started)
            await self.hub._flush()
            return {"task": await self.view(actor, task), "message_id": message.id}

    # ------------------------------------------------------------------ update
    async def update(self, actor: Participant, task_id: str, action: str, *, note=None, result=None,
                     blocked_reason=None, assignee=None, fields=None, cascade=False, expected_version=None,
                     force=False) -> dict:
        async with self.hub.lock:
            out = await self._update(actor, task_id, action, note=note, result=result,
                                     blocked_reason=blocked_reason, assignee=assignee, fields=fields,
                                     cascade=bool(cascade), expected_version=expected_version,
                                     force=bool(force) and actor.is_owner)
            await self.hub._flush()
            return out

    async def _update(self, actor, task_id, action, *, note, result, blocked_reason, assignee, fields, cascade,
                      expected_version, force) -> dict:
        self._check_task_tools(actor)
        task = await self._load(actor, task_id)
        conversation = await self._home(actor, task)
        moderation_only = self._moderation_only(actor, conversation)
        if not actor.is_owner and not can_post(actor, conversation):
            raise HubError("read_only", f"you are not a member of {conversation.id}", 403)
        coordinator = await self._coordinator(task)
        who = task_rules.roles(actor.id, is_owner=actor.is_owner, creator_id=task.creator_id,
                               assignee_id=task.assignee_id, coordinator_id=coordinator)
        task_rules.check(action, task.state, who, actor.id, owner_moderation_only=moderation_only)
        if expected_version is not None and int(expected_version) != task.version:
            raise HubError("conflict", f"{task.ref} changed (version {task.version}, you expected "
                                       f"{expected_version}); read it again", 409,
                           {"task": await self.view(actor, task)})
        task_rules.require(action, {"note": note, "result": result, "blocked_reason": blocked_reason,
                                    "assignee": assignee, "fields": fields})
        note = task_rules.check_text("note", note)
        result = task_rules.check_text("result", result)
        blocked_reason = task_rules.check_text("blocked_reason", blocked_reason)
        await self._scan_text({"note": note, "result": result, "blocked_reason": blocked_reason})
        title = _clip(task.title, 120)

        if action == "edit":
            changes = task_rules.check_edit(fields)
            await self._scan_text({k: v for k, v in changes.items() if isinstance(v, str)})
            changes = {k: v for k, v in changes.items() if getattr(task, k) != v}
            if not changes:
                return {"task": await self.view(actor, task), "message_id": None}
            actor, hop, turn_id, self_started = await self._act(actor, conversation)
            task = await self.repo.update_task_row(task, hop=hop, **changes)
            message = await self._event(task, conversation, actor, "edited", hop, turn_id,
                                        f"{actor.id} edited {task.ref}: {', '.join(changes)}",
                                        data={"fields": list(changes)})
            await self._finish_act(actor, conversation, message, hop, self_started)
            return {"task": await self.view(actor, task), "message_id": message.id}

        if action == "note":
            actor, hop, turn_id, self_started = await self._act(actor, conversation)
            mentions = router.parse_mentions(note, conversation.members)
            message = await self._event(task, conversation, actor, "note", hop, turn_id,
                                        f"{actor.id} on {task.ref}: {_clip(note, 1000)}", note=note,
                                        note_mentions=[m for m in mentions if m != "all"])
            task = await self.repo.update_task_row(task, hop=hop)
            await self._finish_act(actor, conversation, message, hop, self_started)
            return {"task": await self.view(actor, task), "message_id": message.id}

        # State and assignee changes.
        new_state = task_rules.target_state(action, task.state)
        changes: dict = {"state": new_state}
        previous = task.assignee_id
        wake, type, data = "state", "state", {}
        if action == "assign":
            await self._check_assignee(conversation, assignee)
            if assignee == task.assignee_id:
                raise HubError("bad_assignee", f"{task.ref} is already assigned to {assignee}")
            await self._check_open_limit(assignee)
            if task.state == "assigned":                            # reassign or hand off
                await self._check_reassignments(task, conversation)
                changes["reassignments"] = task.reassignments + 1
            changes["assignee_id"] = assignee
            wake, type, data = "assigned", "assigned", {"assignee": assignee, "previous": previous}
            text = f'{actor.id} assigned {task.ref} "{title}" to @{assignee}'
        elif action == "unassign":
            changes["assignee_id"] = None
            wake, type, data = "unassigned", "released", {"previous": previous, "unassign": True}
            text = f'{actor.id} unassigned {previous} from {task.ref} "{title}"'
        elif action == "release":
            changes["assignee_id"] = None
            wake, type, data = "released", "released", {"previous": previous}
            text = f'{actor.id} released {task.ref} "{title}"' + (f": {_clip(note)}" if note else "")
        elif action == "reopen":
            assignee = assignee or previous
            if not assignee:
                raise HubError("missing_field", "reopen needs an assignee; the task never had one")
            await self._check_assignee(conversation, assignee)
            await self._check_open_limit(assignee)
            await self._check_reassignments(task, conversation)
            changes.update(assignee_id=assignee, reassignments=task.reassignments + 1, result=None, closed_at=None)
            wake, data = "reopened", {"assignee": assignee, "previous_result": task.result}
            text = f'{actor.id} reopened {task.ref} "{title}" for @{assignee}: {_clip(note)}'
        elif action == "block":
            changes["blocked_reason"] = blocked_reason
            text = f'{actor.id} blocked {task.ref} "{title}": {_clip(blocked_reason)}'
        elif action == "unblock":
            changes["blocked_reason"] = None
            text = f'{actor.id} unblocked {task.ref} "{title}"'
        elif action == "start":
            text = f'{actor.id} started {task.ref} "{title}"'
        elif action in ("finish", "fail"):
            if action == "finish" and not force:
                open_kids = [k for k in await self.repo.raw_children(task.id) if not k.terminal]
                if open_kids:
                    raise HubError("children_open", f"{task.ref} has {len(open_kids)} unfinished subtasks", 409)
            changes["result"] = result
            text = f'{actor.id} {"finished" if action == "finish" else "failed"} {task.ref} "{title}": {_clip(result)}'
        elif action == "cancel":
            descendants = await self._open_descendants(task)
            if descendants and not cascade:
                raise HubError("children_open", f"{task.ref} has {len(descendants)} unfinished subtasks; pass "
                                                "cascade: true to cancel them too", 409)
            if not actor.is_owner:
                for child in descendants:
                    home = await self.repo.conversation(self.hub.owner, child.conversation_id)
                    if not can_post(actor, home):
                        raise HubError("cascade_denied", "a subtask lives in a conversation you cannot post to; "
                                                         "nothing was cancelled", 403)
            text = f'{actor.id} cancelled {task.ref} "{title}"' + (f": {_clip(note)}" if note else "")
        else:                                                         # pragma: no cover (check() covers it)
            raise HubError("bad_action", f"unknown action {action}")
        actor, hop, turn_id, self_started = await self._act(actor, conversation)
        if new_state in TERMINAL:
            changes["closed_at"] = now()
        from_state = task.state
        task = await self.repo.update_task_row(task, hop=hop, **changes)
        message = await self._event(task, conversation, actor, type, hop, turn_id, text, wake=wake,
                                    from_state=from_state, to_state=new_state,
                                    note=note or result or blocked_reason, data=data, previous_assignee=previous)
        await self._finish_act(actor, conversation, message, hop, self_started)
        if action == "cancel":
            for child in await self._open_descendants(task):
                await self._cascade_cancel(child, actor, hop, turn_id)
        if new_state in TERMINAL and task.parent_id:
            await self._children_done(task, conversation, message, hop, actor.id)
        return {"task": await self.view(actor, task), "message_id": message.id}

    async def _check_reassignments(self, task: Task, conversation: Conversation) -> None:
        if task.reassignments >= self.limits.max_reassignments:
            await self.hub._notice(conversation.id, f"{task.ref} reached the reassignment limit "
                                                    f"({self.limits.max_reassignments}).")
            raise HubError("too_many_reassignments", f"{task.ref} reached the reassignment limit "
                                                     f"({self.limits.max_reassignments})", 409)

    async def _open_descendants(self, task: Task) -> list[Task]:
        found, queue = [], [task.id]
        while queue:
            for child in await self.repo.raw_children(queue.pop(0)):
                if not child.terminal:
                    found.append(child)
                queue.append(child.id)
        return found

    async def _cascade_cancel(self, task: Task, actor: Participant, hop: int, turn_id: str | None) -> None:
        task = await self.repo.raw_task(task.id)
        if task.terminal:
            return
        conversation = await self.repo.conversation(self.hub.owner, task.conversation_id)
        previous = task.state
        task = await self.repo.update_task_row(task, state="cancelled", hop=hop, closed_at=now())
        await self._event(task, conversation, actor, "state", hop, turn_id,
                          f'{actor.id} cancelled {task.ref} "{_clip(task.title, 120)}" (its parent was cancelled)',
                          from_state=previous, to_state="cancelled", data={"cascade": True})

    async def _children_done(self, child: Task, child_conversation: Conversation, child_message: Message, hop: int,
                             finisher: str) -> None:
        """When the last open subtask of a parent ends, tell the parent's assignee (design M4 section 5.3)."""
        parent = await self.repo.raw_task(child.parent_id)
        if parent is None or parent.terminal:
            return
        kids = await self.repo.raw_children(parent.id)
        if any(not k.terminal for k in kids):
            return
        counts = {state: sum(1 for k in kids if k.state == state) for state in TERMINAL}
        summary = ", ".join(f"{n} {state}" for state, n in counts.items() if n)
        parent_conversation = await self.repo.conversation(self.hub.owner, parent.conversation_id)
        readers = set(parent_conversation.members)
        homes = {k.conversation_id: await self.repo.conversation(self.hub.owner, k.conversation_id) for k in kids}
        open_case = all(readers <= set(home.members) for home in homes.values())
        from .hub import HUB
        if open_case:
            text = f"All {len(kids)} subtasks of {parent.ref} are finished: {summary}."
            await self._event(parent, parent_conversation, HUB, "children_done", hop, None, text,
                              data=counts, exclude=[finisher])
            return
        # Private case: some reader of the parent's conversation cannot see a subtask. Nothing is posted
        # there; the event row is owner-only.
        await self.repo.add_task_event(parent.id, "hub", "children_done", hop=hop, data={**counts, "private": True})
        self.hub._emit("task", await self.owner_view(parent))
        assignee = parent.assignee_id
        agents = await self._agents_in(child_conversation)
        if assignee and assignee in agents:
            if assignee != finisher:
                await self.hub._route_task(child_message, child_conversation, HUB, hop, [assignee], child.id)
        elif assignee and assignee != "owner":
            await self.hub._notice(parent_conversation.id, f"{parent.ref}: all subtasks finished; {assignee} "
                                                           f"cannot see {child.ref}, nobody woken.")

    # ------------------------------------------------------------------ reads
    async def list_tasks(self, viewer: Participant, *, conversation_id=None, assignee=None, state=None, parent_id=None,
                   include_closed=False, limit=50, before=None, q=None, max_limit=200) -> dict:
        async with self.hub.lock:
            self._check_task_tools(viewer)
            if conversation_id:
                await self.repo.conversation(viewer, conversation_id)
            states = [state] if isinstance(state, str) else list(state or [])
            for value in states:
                if value not in TASK_STATES:
                    raise HubError("bad_field", f"state must be one of {', '.join(TASK_STATES)}")
            if not states and not include_closed:
                states = list(task_rules.NON_TERMINAL)
            try:
                limit = max(1, min(int(limit), max_limit))
            except (TypeError, ValueError):
                raise HubError("bad_field", "limit must be a number") from None
            cursor = None
            if before:
                cursor = await self.repo.task(viewer, before)
                if cursor is None:
                    raise HubError("bad_field", "before must be the id of a task you can read")
            unassigned = assignee == "unassigned"
            if assignee == "me":
                assignee = viewer.id
            found = await self.repo.tasks(
                viewer, conversation_id=conversation_id, assignee=None if unassigned else assignee,
                unassigned=unassigned, states=states, parent_id=None if parent_id == "none" else parent_id,
                top_level=parent_id == "none", q=q, before=cursor, limit=limit + 1)
            page = found[:limit]
            return {"tasks": [await self.view(viewer, t) for t in page],
                    "next_before": page[-1].id if len(found) > limit else None}

    async def get(self, viewer: Participant, task_id: str, events_limit: int = 50) -> dict:
        async with self.hub.lock:
            self._check_task_tools(viewer)
            task = await self._load(viewer, task_id)
            owner = viewer.is_owner
            return {**await self.view(viewer, task),
                    "events": [self.event_view(e, owner=owner)
                               for e in await self.repo.task_events(viewer, task.id, events_limit)],
                    "children_list": [await self.view(viewer, k) for k in await self.repo.children(viewer, task.id)],
                    "artifacts": [await self.artifact_view(a, owner=owner)
                                  for a in await self.repo.artifacts(viewer, task.id)]}

    # ------------------------------------------------------------------ artifacts
    async def _attach_checks(self, actor: Participant, task_id: str, name: str, description, supersedes):
        self._check_task_tools(actor)
        task = await self._load(actor, task_id)
        conversation = await self._home(actor, task)
        if not can_post(actor, conversation):
            raise HubError("read_only", f"you are not a member of {conversation.id}", 403)
        coordinator = await self._coordinator(task)
        if not (actor.is_owner or actor.id in (task.creator_id, task.assignee_id, coordinator)):
            raise HubError("not_allowed", "only the task's creator, its assignee, the coordinator, or the owner "
                                          "may attach files", 403)
        if not actor.is_owner:
            fresh = await self.hub._agent(actor.id)
            if fresh.paused:
                raise HubError("paused", "the owner has paused you", 403)
        clean = artifact_rules.sanitize_name(name)
        if artifact_rules.forbidden_name(name):
            await self.hub._notice(conversation.id, f"{actor.id} tried to attach a file with a secret to {task.ref} "
                                                    f"(rule: file-name). It was refused.")
            raise HubError("secret_detected", f"files named like {clean!r} may hold secrets and are refused "
                                              "(rule: file-name)", 422)
        if description is not None and (not isinstance(description, str)
                                        or len(description) > MAX_ARTIFACT_DESCRIPTION):
            raise HubError("bad_field", f"description must be text of at most {MAX_ARTIFACT_DESCRIPTION} characters")
        if supersedes:
            old = await self.repo.raw_artifact(supersedes)
            if old is None or old.task_id != task.id or old.deleted_at:
                raise HubError("bad_supersedes", "supersedes must be a live artifact of the same task")
        count, _ = await self.repo.task_artifact_usage(task.id)
        if count >= self.file_limits.max_per_task:
            raise HubError("quota_exceeded", f"{task.ref} already has {self.file_limits.max_per_task} files", 413)
        return task, conversation, clean

    async def attach_bytes(self, actor: Participant, task_id: str, *, name: str, data: bytes,
                           media_type: str | None = None, description: str | None = None,
                           supersedes: str | None = None, inline: bool = True) -> dict:
        """Attach a file whose bytes are in memory (MCP tool or JSON body)."""
        limit = self.file_limits.max_inline_bytes if inline else self.file_limits.max_bytes
        async with self.hub.lock:
            await self._attach_checks(actor, task_id, name, description, supersedes)
        hint = "; use the HTTP route POST /agent/v1/tasks/{id}/artifacts for larger files" if inline else ""
        temp = await asyncio.to_thread(self.store.write_bytes, data, limit, hint)
        return await self._attach_commit(actor, task_id, temp, name, media_type, description, supersedes)

    async def attach_stream(self, actor: Participant, task_id: str, *, name: str, chunks, media_type=None,
                            description=None, supersedes=None) -> dict:
        """Attach a file streamed from an HTTP upload. The hub lock is not held while it streams."""
        async with self.hub.lock:
            await self._attach_checks(actor, task_id, name, description, supersedes)
        temp = await self.store.write_stream(chunks, self.file_limits.max_bytes)
        return await self._attach_commit(actor, task_id, temp, name, media_type, description, supersedes)

    async def _attach_commit(self, actor, task_id, temp: TempFile, name, media_type, description, supersedes):
        committed = False
        try:
            async with self.hub.lock:
                task, conversation, clean = await self._attach_checks(actor, task_id, name, description, supersedes)
                data = await asyncio.to_thread(self.store.read_temp, temp)
                kind, text = artifact_rules.sniff(data, media_type, clean)
                if kind is None or kind not in self.file_limits.media_types:
                    raise HubError("unsupported_media_type", "this kind of file is not accepted; allowed: "
                                                             f"{', '.join(self.file_limits.media_types)}", 415)
                if text is not None and self.file_limits.secret_scan == "reject":
                    hit = artifact_rules.scan(text, await self.repo.token_hashes())
                    if hit:
                        await self.hub._notice(conversation.id, f"{actor.id} tried to attach a file with a secret "
                                                                f"to {task.ref} (rule: {hit[0]}). It was refused.")
                        raise HubError("secret_detected", f"the file looks like it contains a secret (rule: "
                                                          f"{hit[0]}, line {hit[1]}); it was not stored", 422)
                _, used = await self.repo.task_artifact_usage(task.id)
                if used + temp.size > self.file_limits.max_task_bytes:
                    raise HubError("quota_exceeded", f"{task.ref} would exceed its "
                                                     f"{self.file_limits.max_task_bytes} byte quota", 413)
                stored = await self.repo.store_bytes()
                live = await self.repo.live_hashes()
                if temp.sha256 not in live and stored + temp.size > self.file_limits.max_store_bytes:
                    await self.hub._notice("room:main", f"Artifact store is full "
                                                        f"({human_size(self.file_limits.max_store_bytes)}). "
                                                        "Uploads are refused.")
                    raise HubError("quota_exceeded", "the artifact store is full", 507)
                actor, hop, turn_id, self_started = await self._act(actor, conversation)
                await asyncio.to_thread(self.store.commit, temp)
                committed = True
                artifact = await self.repo.add_artifact(
                    task=task, uploader_id=actor.id, name=clean, description=description, media_type=kind,
                    size=temp.size, sha256=temp.sha256, supersedes=supersedes)
                message = await self._event(task, conversation, actor, "artifact", hop, turn_id,
                                            f"{actor.id} attached {clean} ({human_size(temp.size)}) to {task.ref}",
                                            data={"artifact_id": artifact.id, "name": clean})
                await self.repo.set_artifact_message(artifact.id, message.id)
                artifact = await self.repo.raw_artifact(artifact.id)
                await self._finish_act(actor, conversation, message, hop, self_started)
                self.hub._emit("artifact", await self.artifact_view(artifact, owner=True))
                await self.hub._flush()
                return {"artifact": await self.artifact_view(artifact), "message_id": message.id}
        finally:
            if not committed:
                await asyncio.to_thread(self.store.discard, temp)

    async def _readable_artifact(self, viewer: Participant, artifact_id: str) -> Artifact:
        artifact = await self.repo.artifact(viewer, str(artifact_id))
        if artifact is None:
            raise artifact_not_found(artifact_id)
        return artifact

    async def artifact_meta(self, viewer: Participant, artifact_id: str) -> dict:
        async with self.hub.lock:
            self._check_task_tools(viewer)
            artifact = await self._readable_artifact(viewer, artifact_id)
            return await self.artifact_view(artifact, owner=viewer.is_owner)

    async def artifact_content(self, viewer: Participant, artifact_id: str) -> tuple[Artifact, bytes]:
        async with self.hub.lock:
            self._check_task_tools(viewer)
            artifact = await self._readable_artifact(viewer, artifact_id)
        return artifact, await asyncio.to_thread(self.store.read, artifact.sha256)

    async def read_artifact(self, viewer: Participant, artifact_id: str, offset=0, limit=READ_CHUNK) -> dict:
        try:
            offset, limit = int(offset or 0), int(limit or READ_CHUNK)
        except (TypeError, ValueError):
            raise HubError("bad_field", "offset and limit must be numbers") from None
        if offset < 0 or limit < 1:
            raise HubError("bad_field", "offset must be 0 or more and limit 1 or more")
        limit = min(limit, READ_CHUNK)
        artifact, data = await self.artifact_content(viewer, artifact_id)
        if artifact_rules.is_text_type(artifact.media_type):
            whole, encoding = data.decode("utf-8"), "utf8"
            content = whole[offset:offset + limit]
        else:
            whole, encoding = data, "base64"
            content = base64.b64encode(data[offset:offset + limit]).decode()
        end = offset + limit
        return {"artifact": await self.artifact_view(artifact, owner=viewer.is_owner), "encoding": encoding,
                "content": content, "offset": offset, "next_offset": end if end < len(whole) else None}

    async def delete_artifact(self, artifact_id: str) -> dict:
        """Owner soft delete. The file is removed by the next garbage-collection pass if nothing else uses it."""
        async with self.hub.lock:
            owner = self.hub.owner
            artifact = await self.repo.artifact(owner, artifact_id)
            if artifact is None or artifact.deleted_at:
                raise artifact_not_found(artifact_id)
            await self.repo.delete_artifact(artifact.id)
            task = await self.repo.raw_task(artifact.task_id)
            conversation = await self.repo.conversation(owner, task.conversation_id)
            await self._event(task, conversation, owner, "artifact", 0, None,
                              f"artifact {artifact.name} on {task.ref} was removed by the owner",
                              data={"artifact_id": artifact.id, "removed": True})
            artifact = await self.repo.raw_artifact(artifact.id)
            self.hub._emit("artifact", await self.artifact_view(artifact, owner=True))
            await self.gc()
            await self.hub._flush()
            return {"ok": True}

    # ------------------------------------------------------------------ rooms (M5d): a member leaves
    async def release_for_leaver(self, conversation: Conversation, agent_id: str) -> list[str]:
        """Unassign a leaving member's open tasks in a room, as events by `hub`. Returns the refs."""
        from .hub import HUB
        released = []
        for task in await self.repo.tasks(self.hub.owner, conversation_id=conversation.id, assignee=agent_id,
                                          states=list(task_rules.NON_TERMINAL), limit=500):
            task = await self.repo.update_task_row(task, state="open", assignee_id=None)
            await self._event(task, conversation, HUB, "released", 0, None,
                              f'{agent_id} left {conversation.id}, so {task.ref} "{_clip(task.title, 120)}" is '
                              "unassigned", wake="released", data={"previous": agent_id, "left": True})
            released.append(task.ref)
        return released

