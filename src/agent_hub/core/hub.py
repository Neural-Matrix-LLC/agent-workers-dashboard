"""The hub service: posting, wake routing, turn lifecycle, pause/stop, and live events.

Public methods take the lock. Methods starting with `_` expect the caller to hold it.
"""

import asyncio
import logging
import os
import time

import httpx

from ..config import CLAUDE_MODES, Config
from ..ids import hash_token, new_id, now
from . import budgets, delivery, router
from .models import Conversation, HubError, Message, Participant, Turn
from .repo import Repo
from .visibility import can_post

HUB = Participant("hub", "Hub", "system", "none", "poll")      # author of task event messages
GC_INTERVAL = 24 * 3600

log = logging.getLogger("agent_hub")

MAX_BODY = 100_000


class Hub:
    def __init__(self, config: Config, repo: Repo, *, http: httpx.AsyncClient | None = None):
        self.config, self.repo = config, repo
        self.http = http or httpx.AsyncClient()
        self.lock = asyncio.Lock()
        self.owner: Participant | None = None
        self.paused = False                               # global pause
        self.approval_mode = config.limits.approval_mode
        self.rate = budgets.RateLimiter(config.limits.turns_per_minute)
        self.streams: dict[str, asyncio.Queue] = {}       # agent id -> connector event queue
        self.active: dict[str, str] = {}                  # agent id -> turn handed to it
        self.timers: dict[str, asyncio.Task] = {}         # turn id -> timeout task
        self.retry: dict[str, asyncio.TimerHandle] = {}   # agent id -> rate-limit retry
        self.stopped: dict[tuple[str, str], float] = {}   # (agent, conversation) -> reject posts until
        self.continued: set[str] = set()
        self.listeners: set[asyncio.Queue] = set()        # owner websockets
        self.tasks: set[asyncio.Task] = set()
        self.permissions: dict[str, dict] = {}            # pending permission request id -> request
        self.gc_task: asyncio.Task | None = None
        from .tasks import TaskService  # imported here: tasks.py imports this module
        self.task_service = TaskService(self)

    # ------------------------------------------------------------------ setup
    async def setup(self) -> None:
        async with self.lock:
            repo, cfg = self.repo, self.config
            await repo.upsert_participant("owner", cfg.hub.owner_name, "owner", None, "sse", {})
            for agent in cfg.agents:
                await repo.upsert_participant(
                    agent.id, agent.display_name, "agent", agent.connector, agent.delivery,
                    {"description": agent.description, "budget": agent.budget.model_dump(exclude_none=True),
                     "mode": agent.mode})
                token = os.environ.get(agent.token_env)
                if token:
                    await repo.set_token_hash(agent.id, hash_token(token))
            ids = ["owner", *(a.id for a in cfg.agents)]
            await repo.deactivate_missing(ids)
            self.owner = await repo.participant("owner")
            for room in cfg.rooms:
                members = ids if room.members is None else ["owner", *room.members]
                await repo.ensure_room(room.id, room.title, room.mode,
                                       {"fanout": room.fanout, "moderator": room.moderator, "source": "config"},
                                       members)
            await self._reconcile_ui_rooms(ids)
            for agent in cfg.agents:
                await repo.ensure_dm("owner", agent.id)
            self.paused = await repo.get_meta("paused", "0") == "1"
            await repo.deny_stale_permissions()
            # Work that was in flight when the hub stopped cannot be resumed.
            for turn in await repo.turns("state = 'running'"):
                await self._close(turn, "failed", error="hub restarted")
            await self.task_service.start()
            await self._flush()
        self.gc_task = asyncio.create_task(self._gc_loop())

    async def _reconcile_ui_rooms(self, ids: list[str]) -> None:
        """Rooms the owner created in the UI survive a restart; members removed from agents.yaml leave them."""
        for room in await self.repo.rooms():
            if room.config.get("source") == "ui":
                await self.repo.set_members(room.id, [m for m in room.members if m in ids])

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(GC_INTERVAL)
            async with self.lock:
                await self.task_service.gc()

    async def close(self) -> None:
        for handle in self.retry.values():
            handle.cancel()
        for task in [*self.timers.values(), *self.tasks, *([self.gc_task] if self.gc_task else [])]:
            task.cancel()
        await self.http.aclose()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ------------------------------------------------------------------ identity
    async def authenticate(self, token: str) -> Participant | None:
        return await self.repo.participant_by_token_hash(hash_token(token)) if token else None

    async def _agent(self, agent_id: str) -> Participant:
        agent = await self.repo.participant(agent_id)
        if agent is None or agent.is_owner:
            raise HubError("unknown_participant", f"unknown agent: {agent_id}", 404)
        return agent

    async def _roles(self) -> dict[str, str]:
        return {"hub": "system", **{p.id: p.role for p in await self.repo.participants()}}

    def _budget(self, agent: Participant) -> dict:
        """agents.yaml budget; the owner's per-day limits from the UI replace its per-day part."""
        budget = self.config.agent(agent.id).budget.model_dump(exclude_none=True)
        if "budget_override" in agent.config:
            budget = {k: v for k, v in budget.items() if not k.endswith("_per_day")}
            budget.update(agent.config["budget_override"])
        return budget

    def _mode(self, agent: Participant) -> str | None:
        """The permission mode of a claude-code agent: the owner's choice, else agents.yaml."""
        if agent.connector != "claude-code":
            return None
        return agent.config.get("mode_override") or self.config.agent(agent.id).mode

    # ------------------------------------------------------------------ views
    @staticmethod
    def message_view(message: Message, roles: dict[str, str], *, owner: bool = False) -> dict:
        view = {"id": message.id, "conversation_id": message.conversation_id, "from": message.sender_id,
                "role": roles.get(message.sender_id, "agent"), "body": message.body,
                "reply_to": message.reply_to, "at": message.created_at}
        if message.kind != "chat":
            view["kind"] = message.kind
        if message.kind == "task":
            view["actor"] = message.actor_id
            view["task"] = task_ref(message)
        if owner:
            view.update(kind=message.kind, hop=message.hop, turn_id=message.turn_id, mentions=message.mentions)
            if message.kind == "task":
                view.update(task_id=message.task_id, meta=message.meta)
        return view

    @staticmethod
    def turn_view(turn: Turn) -> dict:
        return {"id": turn.id, "agent": turn.agent_id, "conversation_id": turn.conversation_id,
                "trigger_message_id": turn.trigger_msg_id, "state": turn.state, "hop": turn.hop,
                "cost_usd": turn.cost_usd, "tokens_in": turn.tokens_in, "tokens_out": turn.tokens_out,
                "created_at": turn.created_at, "started_at": turn.started_at,
                "finished_at": turn.finished_at, "error": turn.error, "task_id": turn.task_id}

    async def _status(self, agent: Participant) -> str:
        if agent.paused:
            return "paused"
        if await self.repo.turns("agent_id = ? AND state = 'running'", (agent.id,)):
            return "working"
        if not agent.self_driven and agent.id not in self.streams:
            return "offline"
        return "idle"

    async def roster(self) -> list[dict]:
        """Room-level view of everyone, safe to show to any participant."""
        out = []
        for p in await self.repo.participants():
            out.append({"id": p.id, "display_name": p.display_name, "role": p.role,
                        "status": "idle" if p.is_owner else await self._status(p),
                        "description": "The human owner." if p.is_owner else p.config.get("description", "")})
        return out

    async def agents_view(self) -> list[dict]:
        """Owner view: status, spend, budget, and the current turn of every agent."""
        out = []
        for p in await self.repo.participants():
            if p.is_owner:
                continue
            turns, usd = await self.repo.usage(p.id, since=budgets.day_start())
            open_turns = await self.repo.turns(
                "agent_id = ? AND state IN ('awaiting_approval','queued','running')", (p.id,))
            current = next((t for t in open_turns if t.state == "running"), None)
            last = current or await self.repo.last_turn(p.id)
            out.append({"id": p.id, "display_name": p.display_name, "connector": p.connector,
                        "delivery": p.delivery, "description": p.config.get("description", ""),
                        "status": await self._status(p), "has_token": await self.repo.has_token(p.id),
                        "turns_today": turns, "usd_today": round(usd, 4), "budget": self._budget(p),
                        "queued": sum(1 for t in open_turns if t.state == "queued"),
                        "awaiting_approval": [self.turn_view(t) for t in open_turns
                                              if t.state == "awaiting_approval"],
                        "mode": self._mode(p), "modes": list(CLAUDE_MODES) if self._mode(p) else [],
                        "pending_permissions": self.permissions_view(p.id),
                        "open_tasks": await self.repo.count_open_assigned(p.id),
                        "task_tools": self.config.agent(p.id).task_tools,
                        "turn_id": last.id if last else None})
        return out

    def settings_view(self) -> dict:
        limits = self.config.limits
        return {"paused": self.paused, "approval_mode": self.approval_mode, "max_hops": limits.max_hops,
                "turns_per_minute": limits.turns_per_minute}

    # ------------------------------------------------------------------ live events for the owner
    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.listeners.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.listeners.discard(queue)

    def _emit(self, type: str, data) -> None:
        for queue in list(self.listeners):
            try:
                queue.put_nowait({"type": type, "data": data})
            except asyncio.QueueFull:
                self.listeners.discard(queue)     # a stuck browser tab; it reloads on reconnect

    async def _emit_message(self, message: Message) -> None:
        self._emit("message", self.message_view(message, await self._roles(), owner=True))

    async def _flush(self) -> None:
        """After any state change: hand out queued turns and refresh the owner's agent panel."""
        for agent in await self.repo.participants():
            if not agent.is_owner:
                await self._pump(agent)
        self._emit("agents", await self.agents_view())

    # ------------------------------------------------------------------ posting
    async def post(self, sender: Participant, conversation_id: str, body: str,
                   reply_to: str | None = None) -> dict:
        async with self.lock:
            result = await self._post(sender, conversation_id, body, reply_to)
            await self._flush()
            return result

    async def send_dm(self, sender: Participant, to: str, body: str) -> dict:
        async with self.lock:
            other = await self.repo.participant(to)
            if other is None or other.id == sender.id:
                raise HubError("unknown_participant", f"unknown participant: {to}", 404)
            conversation_id = await self.repo.ensure_dm(sender.id, other.id)
            result = await self._post(sender, conversation_id, body)
            await self._flush()
            return {**result, "conversation_id": conversation_id}

    async def _post(self, sender: Participant, conversation_id: str, body: str,
                    reply_to: str | None = None, turn: Turn | None = None) -> dict:
        repo = self.repo
        conversation = await repo.conversation(sender, conversation_id)
        if not can_post(sender, conversation):
            raise HubError("read_only", f"you are not a member of {conversation_id}", 403)
        if not isinstance(body, str) or not body.strip():
            raise HubError("empty_body", "the message body is empty")
        if len(body) > MAX_BODY:
            raise HubError("too_long", f"the message is longer than {MAX_BODY} characters")
        if conversation.archived_at:
            raise HubError("archived", f"{conversation.id} is archived", 409)
        silent = body.strip() == "NO_REPLY"

        acting = await self._actor_hop(sender, conversation, turn, silent=silent)
        if acting is None:
            return {"stored": False, "note": "no open turn; nothing to close"}
        sender, hop, turn, turn_id, self_started = acting

        if silent:
            await self._close(turn, "done")
            return {"stored": False, "note": "turn closed without a reply"}

        if reply_to is not None:
            target = await repo.message(sender, reply_to)
            if target is None or target.conversation_id != conversation.id:
                raise HubError("bad_reply_to", "reply_to must be a message in the same conversation")

        if turn is not None and turn.group_id:
            # Parallel fan-out: nobody sees a reply until every reply is in.
            turn = await repo.update_turn(turn.id, held_body=body)
            await self._close(turn, "done")
            return {"stored": True, "held": True, "note": "shown when the other agents have replied"}

        message = await self._store(conversation, sender, body, hop, reply_to, turn_id)
        if self_started:
            await self._record_self_started(sender, conversation.id, message, hop)
        if turn is not None:
            await self._close(turn, "done")
        return {"stored": True, "id": message.id}

    async def _actor_hop(self, sender: Participant, conversation: Conversation, turn: Turn | None = None, *,
                         silent: bool = False) -> tuple[Participant, int, Turn | None, str | None, bool] | None:
        """The hop of something `sender` does in `conversation`: a post or a task event (design section 6,
        M4 section 8.1). Returns (sender with fresh pause state, hop, the open turn this act belongs to,
        the turn id to record, self-started), or None for a silent post with nothing to close.

        Raises paused, stopped, or rate_limited. A self-started act passes the rate limit and budget checks;
        the caller records it with `_record_self_started` once it is stored.
        """
        repo = self.repo
        if sender.is_owner:
            return sender, 0, None, None, False
        if turn is None:
            sender = await self._agent(sender.id)        # fresh pause state
            if sender.paused:
                raise HubError("paused", "the owner has paused you; stop posting", 403)
            if sender.self_driven:
                turn = await repo.open_turn(sender.id, conversation.id)
            if turn is None and self.stopped.get((sender.id, conversation.id), 0) > time.monotonic():
                raise HubError("stopped", "the owner stopped your turn in this conversation", 409)
        if turn is not None:
            return sender, turn.hop + 1, turn, turn.id, False
        last = await repo.last_hop(conversation.id) or 0
        running = await repo.turns("agent_id = ? AND state = 'running'", (sender.id,))
        if running:                                   # a side act made during a turn
            return sender, max(running[0].hop, last) + 1, None, running[0].id, False
        if silent:
            return None
        wait = self.rate.wait_time(sender.id)         # the agent starts an exchange itself
        if wait:
            raise HubError("rate_limited", f"rate limit reached; retry in {wait:.0f}s", 429)
        reason = await budgets.exhausted(repo, sender.id, self._budget(sender), conversation.id)
        if reason:
            await self._pause_for_budget(sender, conversation.id, reason)
            raise HubError("paused", f"you are paused: {reason}", 403)
        return sender, last + 1, None, None, True

    async def _record_self_started(self, sender: Participant, conversation_id: str, message: Message,
                                   hop: int) -> None:
        """A self-started act counts as a finished turn, against the rate limit and the budget."""
        self.rate.record(sender.id)
        started = await self.repo.add_turn(sender.id, conversation_id, message.id, state="done", hop=hop - 1,
                                           task_id=message.task_id)
        await self.repo.update_turn(started.id, started_at=message.created_at, finished_at=message.created_at)

    async def _store(self, conversation: Conversation, sender: Participant, body: str, hop: int,
                     reply_to: str | None, turn_id: str | None) -> Message:
        mentions = router.parse_mentions(body, conversation.members)
        message = await self.repo.add_message(conversation.id, sender.id, body, mentions=mentions,
                                              reply_to=reply_to, hop=hop, turn_id=turn_id)
        await self._emit_message(message)
        await self._route(message, conversation, sender, hop)
        return message

    async def _notice(self, conversation_id: str, body: str, about: str | None = None) -> None:
        """An owner-only line in a conversation. Agents never receive notices."""
        message = await self.repo.add_message(conversation_id, "owner", body, kind="notice", reply_to=about)
        await self._emit_message(message)

    # ------------------------------------------------------------------ routing
    async def _agent_members(self, conversation: Conversation) -> list[str]:
        """Agent members of a conversation, in roster order."""
        return [p.id for p in await self.repo.participants() if not p.is_owner and p.id in conversation.members]

    async def _route(self, message: Message, conversation: Conversation, sender: Participant, hop: int,
                     *, by_owner: bool = False) -> None:
        """`by_owner` is set when the owner continues a stopped chain: no hop check and no approval wait."""
        participants = await self.repo.participants()
        agents = [p.id for p in participants if not p.is_owner and p.id in conversation.members]
        plan = router.decide(
            sender=sender.id, sender_is_owner=sender.is_owner, kind=conversation.kind, mode=conversation.mode,
            members=conversation.members, agents=agents, mentions=message.mentions,
            fanout=conversation.config.get("fanout", "sequential"),
            moderator=conversation.config.get("moderator"), agent_all=self.config.limits.agent_all,
            last_agent_sender=await self.repo.last_agent_sender(conversation.id))
        if not plan.targets:
            return
        limit = self.config.limits.max_hops
        if not sender.is_owner and not by_owner and hop >= limit:
            await self._notice(conversation.id, f"Chain paused at {hop} hops. Nobody was woken.", message.id)
            return
        held = self.approval_mode and not sender.is_owner and not by_owner
        state = "awaiting_approval" if held else "queued"
        if plan.fanout == "sequential":
            await self._wake_next(plan.targets, conversation.id, message.id, hop, state)
        else:
            group = new_id() if plan.fanout == "parallel" else None
            for target in plan.targets:
                await self._wake(target, conversation.id, message.id, hop, state, group=group)

    async def _wake(self, agent_id: str, conversation_id: str, trigger: str, hop: int, state: str = "queued",
                    rest: list[str] = (), group: str | None = None, task_id: str | None = None) -> None:
        self.stopped.pop((agent_id, conversation_id), None)
        if not rest and not group:
            # A turn that has not been delivered yet will already carry this message as unread.
            waiting = await self.repo.turns(
                "agent_id = ? AND conversation_id = ? AND state IN ('queued', 'awaiting_approval')",
                (agent_id, conversation_id))
            if waiting:
                return
        turn = await self.repo.add_turn(agent_id, conversation_id, trigger, state=state, hop=hop,
                                        rest=rest, group_id=group, task_id=task_id)
        self._emit("turn", self.turn_view(turn))

    async def _route_task(self, message: Message, conversation: Conversation, actor: Participant, hop: int,
                          targets: list[str], task_id: str, *, by_owner: bool = False) -> None:
        """Wake the targets of a task event, with the same brakes as `_route` (design M4 section 7.3).
        Room modes do not apply to task messages; each target is a single wake."""
        if not targets or conversation.archived_at:
            return
        limit = self.config.limits.max_hops
        if not actor.is_owner and not by_owner and hop >= limit:
            await self._notice(conversation.id, f"Chain paused at {hop} hops. Nobody was woken.", message.id)
            return
        held = self.approval_mode and not actor.is_owner and not by_owner
        for target in targets:
            await self._wake(target, conversation.id, message.id, hop, "awaiting_approval" if held else "queued",
                             task_id=task_id)

    async def _wake_next(self, candidates: list[str], conversation_id: str, trigger: str, hop: int,
                         state: str = "queued") -> None:
        """Sequential fan-out: wake the first agent that is not paused; the rest follow when it finishes."""
        for index, agent_id in enumerate(candidates):
            agent = await self.repo.participant(agent_id)
            if agent and not agent.paused:
                await self._wake(agent_id, conversation_id, trigger, hop, state, rest=candidates[index + 1:])
                # `rest` may be empty for the last agent; that is a normal single wake.
                return

    # ------------------------------------------------------------------ turn scheduling
    async def _pump(self, agent: Participant) -> None:
        """Hand the agent its next queued turn, if it is free and allowed to take one."""
        if self.paused or agent.paused or agent.id in self.active:
            return
        turn = await self.repo.next_queued(agent.id)
        if turn is None:
            return
        if not agent.self_driven and agent.id not in self.streams:
            return                                            # stays queued until its connector connects
        wait = self.rate.wait_time(agent.id)
        if wait:
            if agent.id not in self.retry:
                loop = asyncio.get_running_loop()
                self.retry[agent.id] = loop.call_later(wait + 0.05, lambda: self._spawn(self._retry(agent.id)))
            return
        reason = await budgets.exhausted(self.repo, agent.id, self._budget(agent), turn.conversation_id)
        if reason:
            await self._pause_for_budget(agent, turn.conversation_id, reason)
            return

        self.rate.record(agent.id)
        self.active[agent.id] = turn.id
        if agent.self_driven:
            timeout = self.config.agent(agent.id).turn_timeout
            self.timers[turn.id] = asyncio.create_task(self._timeout(turn.id, timeout))
            if agent.delivery == "webhook":
                unread = (await self.repo.unread(agent)).get(turn.conversation_id, 0)
                self._spawn(self._ping(agent.id, turn, max(unread, 1)))
        else:
            conversation = await self.repo.conversation(agent, turn.conversation_id)
            messages = await self.repo.unread_messages(agent, conversation.id)
            if not any(m.id == turn.trigger_msg_id for m in messages):
                trigger = await self.repo.message(agent, turn.trigger_msg_id)
                if trigger:
                    messages.insert(0, trigger)
            if messages:
                await self.repo.advance_cursor(agent.id, conversation.id, messages[-1].id)
            turn = await self.repo.update_turn(turn.id, state="running", started_at=now())
            payload = delivery.wake_payload(turn, conversation, messages, await self._roles(),
                                            {"permission_mode": self._mode(agent)} if self._mode(agent) else {})
            if turn.task_id:
                task = await self.repo.task(agent, turn.task_id)
                if task is not None and task.conversation_id == conversation.id:
                    payload["task"] = await self.task_service.view(agent, task)
            self.streams[agent.id].put_nowait({"event": "wake", "data": payload})
            self._emit("turn", self.turn_view(turn))

    async def _retry(self, agent_id: str) -> None:
        async with self.lock:
            self.retry.pop(agent_id, None)
            await self._flush()

    async def _ping(self, agent_id: str, turn: Turn, unread: int) -> None:
        cfg, limits = self.config.agent(agent_id), self.config.limits
        url = os.environ.get(cfg.webhook_url_env or "")
        if not url:
            log.warning("no webhook URL for %s (set %s); the wake stays pending for a poll",
                        agent_id, cfg.webhook_url_env)
            return
        ok = await delivery.send_ping(
            self.http, url, os.environ.get(cfg.webhook_key_env or ""),
            delivery.ping_payload(agent_id, turn.conversation_id, turn.id, unread),
            retries=limits.webhook_retries, backoff=limits.webhook_backoff)
        async with self.lock:
            await self.repo.add_trace(turn.id, "log", {"webhook_ping": "delivered" if ok else "failed"})
            self._emit("trace", {"turn_id": turn.id, "agent": agent_id})

    async def _timeout(self, turn_id: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        async with self.lock:
            turn = await self.repo.turn(turn_id)
            self.timers.pop(turn_id, None)
            if turn and turn.open:
                await self._close(turn, "failed", error="timeout", cancel_timer=False)
                await self._flush()

    async def _close(self, turn: Turn, state: str, *, error: str | None = None, advance: bool = True,
                     cancel_timer: bool = True, **usage) -> Turn:
        fields = {"state": state, "finished_at": now(), "error": error, **usage}
        if turn.started_at is None and state != "cancelled":
            fields["started_at"] = fields["finished_at"]
        turn = await self.repo.update_turn(turn.id, **fields)
        for request_id, request in list(self.permissions.items()):
            if request["turn_id"] == turn.id:
                await self._decide(request_id, False, f"the turn ended ({error or state})")
        timer = self.timers.pop(turn.id, None)
        if timer and cancel_timer:
            timer.cancel()
        if self.active.get(turn.agent_id) == turn.id:
            del self.active[turn.agent_id]
        self._emit("turn", self.turn_view(turn))
        if advance and turn.rest:
            await self._wake_next(turn.rest, turn.conversation_id, turn.trigger_msg_id, turn.hop)
        if turn.group_id:
            await self._release(turn.group_id)
        return turn

    async def _release(self, group_id: str) -> None:
        """Parallel fan-out: once every turn of the group is closed, post the held replies together."""
        group = await self.repo.turns("group_id = ?", (group_id,))
        if any(t.state in ("awaiting_approval", "queued", "running") for t in group):
            return
        for turn in group:
            if turn.held_body is None:
                continue
            await self.repo.update_turn(turn.id, held_body=None)
            sender = await self.repo.participant(turn.agent_id)
            if sender is None:
                continue
            conversation = await self.repo.conversation(sender, turn.conversation_id)
            await self._store(conversation, sender, turn.held_body, turn.hop + 1, None, turn.id)

    async def _pause_for_budget(self, agent: Participant, conversation_id: str, reason: str) -> None:
        await self.repo.set_paused(agent.id, True)
        agent.paused = True
        await self._notice(conversation_id, f"{agent.id} is paused: {reason}. Raise the budget, then resume.")

    # ------------------------------------------------------------------ agent tools (MCP and REST)
    async def whoami(self, agent: Participant) -> dict:
        conversations = [c for c in await self.repo.conversations(agent) if not c.archived_at]
        view = {"id": agent.id, "display_name": agent.display_name,
                "conversations": [c.id for c in conversations], "notice": delivery.NOTICES}
        if self.config.agent(agent.id).task_tools:
            view["open_tasks"] = [{"id": t.id, "ref": t.ref, "title": t.title, "state": t.state,
                                   "conversation_id": t.conversation_id}
                                  for t in await self.repo.open_tasks_of(agent, agent.id)]
        return view

    async def list_conversations(self, viewer: Participant) -> list[dict]:
        unread = await self.repo.unread(viewer)
        pending = {t.conversation_id for t in await self.repo.turns(
            "agent_id = ? AND state IN ('queued','running')", (viewer.id,))}
        open_tasks = await self.repo.open_task_counts(viewer)
        return [{"id": c.id, "kind": c.kind, "title": c.title, "members": c.members,
                 "unread": unread.get(c.id, 0), "pending_wake": c.id in pending,
                 "open_tasks": open_tasks.get(c.id, 0)}
                for c in await self.repo.conversations(viewer) if not c.archived_at]

    async def read_messages(self, viewer: Participant, conversation_id: str, after: str | None = None,
                            limit: int = 50) -> list[dict]:
        """Without `after`: the unread messages. Reading advances the caller's cursor."""
        async with self.lock:
            repo = self.repo
            conversation = await repo.conversation(viewer, conversation_id)
            if after is None:
                after = await repo.cursor(viewer.id, conversation.id)
            messages = await repo.messages(viewer, conversation.id, after=after or None, limit=limit)
            if messages:
                await repo.advance_cursor(viewer.id, conversation.id, messages[-1].id)
            if viewer.self_driven:
                turn = await repo.open_turn(viewer.id, conversation.id)
                if turn and turn.state == "queued":
                    turn = await repo.update_turn(turn.id, state="running", started_at=now())
                    self._emit("turn", self.turn_view(turn))
                    self._emit("agents", await self.agents_view())
            roles = await self._roles()
            return [self.message_view(m, roles) for m in messages]

    async def record_tool_call(self, agent: Participant, tool: str, args: dict) -> None:
        """Trace what a self-driven agent does through the hub tools, on its open turn."""
        if not agent.self_driven:
            return
        async with self.lock:
            turn = None
            if args.get("conversation_id"):
                turn = await self.repo.open_turn(agent.id, args["conversation_id"])
            if turn is None:
                found = await self.repo.turns("agent_id = ? AND state IN ('queued','running')", (agent.id,))
                turn = next((t for t in found if t.id == self.active.get(agent.id)), None)
            if turn is None:
                return
            shown = {k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v) for k, v in args.items()}
            await self.repo.add_trace(turn.id, "tool_call", {"tool": tool, "args": shown})
            self._emit("trace", {"turn_id": turn.id, "agent": agent.id})

    # ------------------------------------------------------------------ connector API
    async def connect(self, agent: Participant) -> asyncio.Queue:
        async with self.lock:
            old = self.streams.get(agent.id)
            if old is not None:
                old.put_nowait({"event": "replaced", "data": {}})
            queue: asyncio.Queue = asyncio.Queue()
            self.streams[agent.id] = queue
            await self._flush()
            return queue

    async def disconnect(self, agent: Participant, queue: asyncio.Queue) -> None:
        async with self.lock:
            if self.streams.get(agent.id) is not queue:
                return
            del self.streams[agent.id]
            for turn in await self.repo.turns("agent_id = ? AND state = 'running'", (agent.id,)):
                await self._close(turn, "failed", error="connector disconnected")
            await self._flush()

    async def _own_turn(self, agent: Participant, turn_id: str) -> Turn:
        turn = await self.repo.turn(turn_id)
        if turn is None or turn.agent_id != agent.id:
            raise HubError("unknown_turn", f"unknown turn: {turn_id}", 404)
        return turn

    async def turn_start(self, agent: Participant, turn_id: str) -> dict:
        async with self.lock:
            turn = await self._own_turn(agent, turn_id)
            if not turn.open:
                raise HubError("turn_closed", f"turn is {turn.state}", 409)
            return {"state": turn.state}

    async def turn_trace(self, agent: Participant, turn_id: str, events: list[dict]) -> dict:
        async with self.lock:
            turn = await self._own_turn(agent, turn_id)
            for event in events[:200]:
                await self.repo.add_trace(turn.id, str(event.get("type", "log"))[:40], event.get("payload"))
            self._emit("trace", {"turn_id": turn.id, "agent": agent.id})
            return {"stored": min(len(events), 200)}

    async def turn_finish(self, agent: Participant, turn_id: str, text: str, usage: dict | None = None) -> dict:
        async with self.lock:
            turn = await self._own_turn(agent, turn_id)
            if not turn.open:
                raise HubError("turn_closed", f"turn is {turn.state}", 409)
            usage = usage or {}
            fields = {k: usage[k] for k in ("cost_usd", "tokens_in", "tokens_out") if usage.get(k) is not None}
            if fields:
                turn = await self.repo.update_turn(turn.id, **fields)
            try:
                result = await self._post(agent, turn.conversation_id, text or "NO_REPLY", turn=turn)
            except HubError as error:
                await self._close(turn, "failed", error=error.message)
                await self._flush()
                raise
            await self._flush()
            return result

    async def turn_fail(self, agent: Participant, turn_id: str, error: str) -> dict:
        async with self.lock:
            turn = await self._own_turn(agent, turn_id)
            if turn.open:
                await self._close(turn, "failed", error=(error or "failed")[:2000])
                await self._flush()
            return {"state": "failed"}

    # ------------------------------------------------------------------ owner controls
    async def pause(self, agent_id: str, paused: bool) -> None:
        async with self.lock:
            agent = await self._agent(agent_id)
            await self.repo.set_paused(agent.id, paused)
            await self._flush()

    async def pause_all(self, paused: bool) -> None:
        async with self.lock:
            self.paused = paused
            await self.repo.set_meta("paused", "1" if paused else "0")
            self._emit("settings", self.settings_view())
            await self._flush()

    async def stop(self, agent_id: str | None = None) -> int:
        """Cancel open turns of one agent, or of every agent. Stopping everything also ends fan-outs."""
        async with self.lock:
            agents = [await self._agent(agent_id)] if agent_id else \
                [p for p in await self.repo.participants() if not p.is_owner]
            count = 0
            for agent in agents:
                for turn in await self.repo.turns(
                        "agent_id = ? AND state IN ('awaiting_approval','queued','running')", (agent.id,)):
                    if agent.self_driven:
                        # The hub cannot cancel work inside a self-driven agent; it rejects the late post.
                        self.stopped[(agent.id, turn.conversation_id)] = \
                            time.monotonic() + self.config.agent(agent.id).turn_timeout
                    elif turn.state == "running" and agent.id in self.streams:
                        self.streams[agent.id].put_nowait({"event": "cancel", "data": {"turn_id": turn.id}})
                    await self._close(turn, "cancelled", error="stopped by the owner", advance=agent_id is not None)
                    count += 1
            await self._flush()
            return count

    async def set_budget(self, agent_id: str, budget: dict) -> None:
        async with self.lock:
            agent = await self._agent(agent_id)
            per_day = {k: v for k, v in budget.items() if k.endswith("_per_day")}
            await self.repo.set_participant_config(agent.id, {
                **agent.config, "budget_override": per_day, "budget_base": agent.config.get("budget")})
            await self._flush()

    # ------------------------------------------------------------------ permission requests
    def permissions_view(self, agent_id: str) -> list[dict]:
        return [request["view"] for request in self.permissions.values() if request["agent_id"] == agent_id]

    async def request_permission(self, agent: Participant, turn_id: str, tool: str, input: dict) -> dict:
        """Claude Code asks to use a tool. Waits for the owner's Allow or Deny, the timeout, or the turn's end.

        Returns the answer in the form `--permission-prompt-tool` expects.
        """
        async with self.lock:
            turn = await self.repo.turn(turn_id)
            if turn is None or turn.agent_id != agent.id or turn.state != "running":
                raise HubError("no_running_turn", "no running turn of yours with that id", 409)
            request_id = await self.repo.add_permission(turn.id, agent.id, tool, input)
            future = asyncio.get_running_loop().create_future()
            self.permissions[request_id] = {
                "turn_id": turn.id, "agent_id": agent.id, "future": future,
                "view": {"id": request_id, "turn_id": turn.id, "conversation_id": turn.conversation_id,
                         "tool": tool, "summary": delivery.permission_summary(tool, input), "at": now()}}
            await self._flush()
        timeout = self.config.agent(agent.id).permission_timeout
        try:
            allowed, reason = await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            async with self.lock:
                await self._decide(request_id, False, f"the owner did not answer within {timeout:.0f}s")
                await self._flush()
            allowed, reason = future.result()
        if allowed:
            return {"behavior": "allow"}                    # the input runs unchanged
        return {"behavior": "deny", "message": f"Denied: {reason}. Do not retry this action; tell the owner "
                                               "what you needed and why."}

    async def decide_permission(self, request_id: str, allow: bool) -> None:
        async with self.lock:
            if request_id not in self.permissions:
                raise HubError("not_pending", "that permission request is not pending", 404)
            await self._decide(request_id, allow, "allowed by the owner" if allow else "the owner said no")
            await self._flush()

    async def _decide(self, request_id: str, allow: bool, reason: str) -> None:
        request = self.permissions.pop(request_id, None)
        if request is None:
            return
        await self.repo.decide_permission(request_id, "allowed" if allow else "denied", reason)
        if not request["future"].done():
            request["future"].set_result((allow, reason))

    async def set_agent_mode(self, agent_id: str, mode: str) -> None:
        """The owner picks the permission mode of a claude-code agent. It applies from the next turn."""
        async with self.lock:
            agent = await self._agent(agent_id)
            if agent.connector != "claude-code":
                raise HubError("no_modes", f"{agent.id} has no permission modes", 400)
            if mode not in CLAUDE_MODES:
                raise HubError("bad_mode", f"mode must be one of {', '.join(CLAUDE_MODES)}", 400)
            await self.repo.set_participant_config(agent.id, {
                **agent.config, "mode_override": mode, "mode_base": agent.config.get("mode")})
            await self._flush()

    async def set_mode(self, conversation_id: str, mode: str, fanout: str | None = None) -> None:
        async with self.lock:
            conversation = await self.repo.conversation(self.owner, conversation_id)
            if conversation.kind != "room":
                raise HubError("not_a_room", "only a room has a mode")
            await self.repo.set_mode(conversation.id, mode)
            if fanout:
                await self.repo.set_conversation_config(conversation.id, {**conversation.config, "fanout": fanout})
            self._emit("conversation", {"id": conversation.id})

    async def set_approval_mode(self, on: bool) -> None:
        async with self.lock:
            self.approval_mode = on
            self._emit("settings", self.settings_view())

    async def decide_turn(self, turn_id: str, approve: bool) -> None:
        async with self.lock:
            turn = await self.repo.turn(turn_id)
            if turn is None or turn.state != "awaiting_approval":
                raise HubError("unknown_turn", "no turn is waiting for approval with that id", 404)
            if approve:
                turn = await self.repo.update_turn(turn.id, state="queued")
                self._emit("turn", self.turn_view(turn))
            else:
                await self._close(turn, "cancelled", error="rejected by the owner")
            await self._flush()

    async def continue_chain(self, notice_id: str) -> None:
        """The Continue button on a hop-limit notice: wake for the blocked message with the hop count reset."""
        async with self.lock:
            notice = await self.repo.message(self.owner, notice_id)
            blocked = await self.repo.message(self.owner, notice.reply_to) if notice and notice.reply_to else None
            if notice is None or notice.kind != "notice" or blocked is None:
                raise HubError("not_continuable", "that is not a hop-limit notice", 404)
            if notice.id in self.continued:
                raise HubError("already_continued", "this chain was already continued", 409)
            self.continued.add(notice.id)
            conversation = await self.repo.conversation(self.owner, blocked.conversation_id)
            if blocked.kind == "task":
                agents = await self._agent_members(conversation)
                targets = [t for t in blocked.meta.get("targets", []) if t in agents]
                await self._route_task(blocked, conversation, HUB, 0, targets, blocked.task_id, by_owner=True)
                await self._flush()
                return
            sender = await self.repo.participant(blocked.sender_id)
            if sender is None:
                raise HubError("not_continuable", "the sender is no longer in the roster", 404)
            await self._route(blocked, conversation, sender, 0, by_owner=True)
            await self._flush()

    # ------------------------------------------------------------------ rooms (design M5d)
    def room_view(self, conversation: Conversation, counts: dict | None = None) -> dict:
        counts = counts or {}
        return {"id": conversation.id, "kind": conversation.kind, "title": conversation.title,
                "mode": conversation.mode, "fanout": conversation.config.get("fanout"),
                "moderator": conversation.config.get("moderator"), "members": conversation.members,
                "observed": self.owner.id not in conversation.members,
                "source": conversation.config.get("source", "config") if conversation.kind == "room" else None,
                "archived_at": conversation.archived_at,
                "messages": counts.get(conversation.id, (0, None))[0],
                "last_message_id": counts.get(conversation.id, (0, None))[1]}

    async def _room(self, room_id: str) -> Conversation:
        conversation = await self.repo.conversation(self.owner, room_id)
        if conversation.kind != "room":
            raise HubError("not_a_room", f"{room_id} is not a room")
        return conversation

    async def _check_members(self, members) -> list[str]:
        if not isinstance(members, list):
            raise HubError("bad_field", "members must be a list of agent ids")
        agents = {p.id for p in await self.repo.participants() if not p.is_owner}
        unknown = [m for m in members if m not in agents]
        if unknown:
            raise HubError("unknown_participant", f"unknown agents: {', '.join(map(str, unknown))}", 404)
        return list(dict.fromkeys(members))

    async def create_room(self, room_id: str, title: str | None, mode: str, fanout: str, moderator: str | None,
                          members: list[str] | None) -> dict:
        from ..config import ROOM_ID
        async with self.lock:
            if not isinstance(room_id, str) or not ROOM_ID.match(room_id):
                raise HubError("bad_room_id", "a room id looks like room:<slug> (a-z, 0-9, -; up to 32 characters)")
            if await self.repo.room_exists(room_id):
                raise HubError("exists", f"{room_id} already exists", 409)
            if members is None:
                members = [p.id for p in await self.repo.participants() if not p.is_owner]
            members = await self._check_members(members)
            if moderator is not None and moderator not in members:
                raise HubError("bad_field", "the moderator must be a member of the room")
            await self.repo.ensure_room(room_id, title, mode, {"fanout": fanout, "moderator": moderator,
                                                               "source": "ui"}, ["owner", *members])
            self._emit("conversation", {"id": room_id})
            return self.room_view(await self._room(room_id))

    async def update_room(self, room_id: str, fields: dict) -> dict:
        async with self.lock:
            room = await self._room(room_id)
            if room.archived_at:
                raise HubError("archived", f"{room_id} is archived", 409)
            if "title" in fields:
                await self.repo.set_room_fields(room.id, title=fields["title"])
            if fields.get("mode"):
                await self.repo.set_mode(room.id, fields["mode"])
            config = dict(room.config)
            if fields.get("fanout"):
                config["fanout"] = fields["fanout"]
            if "moderator" in fields:
                if fields["moderator"] is not None and fields["moderator"] not in room.members:
                    raise HubError("bad_field", "the moderator must be a member of the room")
                config["moderator"] = fields["moderator"]
            await self.repo.set_conversation_config(room.id, config)
            self._emit("conversation", {"id": room.id})
            return self.room_view(await self._room(room.id))

    async def set_room_members(self, room_id: str, members) -> dict:
        """A removed member loses access at once: its turns there are cancelled and its open tasks there are
        unassigned. A new member's read cursor starts at the latest message."""
        async with self.lock:
            room = await self._room(room_id)
            if room.archived_at:
                raise HubError("archived", f"{room_id} is archived", 409)
            if room.config.get("source", "config") != "ui":
                raise HubError("config_room", f"{room_id} is defined in agents.yaml; edit its members there", 409)
            members = await self._check_members(members)
            leaving = [m for m in room.members if m != "owner" and m not in members]
            released = []
            for agent_id in leaving:
                await self._cancel_turns_in(room.id, [agent_id], f"{agent_id} left {room.id}")
                released += await self.task_service.release_for_leaver(room, agent_id)
            added, removed = await self.repo.set_members(room.id, ["owner", *members])
            if room.config.get("moderator") in removed:
                await self.repo.set_conversation_config(room.id, {**room.config, "moderator": None})
            self._emit("conversation", {"id": room.id})
            await self._flush()
            return {"added": added, "removed": removed, "released_tasks": released}

    async def archive_room(self, room_id: str) -> dict:
        async with self.lock:
            room = await self._room(room_id)
            if room.id == "room:main":
                raise HubError("main_room", "room:main cannot be archived", 409)
            if not room.archived_at:
                await self.repo.set_room_fields(room.id, archived_at=now())
                await self._cancel_turns_in(room.id, None, f"{room.id} was archived")
            self._emit("conversation", {"id": room.id})
            await self._flush()
            return {"ok": True}

    async def _cancel_turns_in(self, conversation_id: str, agent_ids: list[str] | None, reason: str) -> None:
        for turn in await self.repo.turns(
                "conversation_id = ? AND state IN ('awaiting_approval','queued','running')", (conversation_id,)):
            if agent_ids is not None and turn.agent_id not in agent_ids:
                continue
            if turn.state == "running" and turn.agent_id in self.streams:
                self.streams[turn.agent_id].put_nowait({"event": "cancel", "data": {"turn_id": turn.id}})
            await self._close(turn, "cancelled", error=reason, advance=False)


def task_ref(message: Message) -> dict:
    """The `task` field of a task message: which task and what happened. Never another conversation's ref."""
    meta = message.meta
    return {"id": message.task_id, "ref": meta.get("ref"), "event": meta.get("event"), "state": meta.get("state")}
