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
                await repo.ensure_room(room.id, room.title, room.mode,
                                       {"fanout": room.fanout, "moderator": room.moderator}, ids)
            for agent in cfg.agents:
                await repo.ensure_dm("owner", agent.id)
            self.paused = await repo.get_meta("paused", "0") == "1"
            await repo.deny_stale_permissions()
            # Work that was in flight when the hub stopped cannot be resumed.
            for turn in await repo.turns("state = 'running'"):
                await self._close(turn, "failed", error="hub restarted")
            await self._flush()

    async def close(self) -> None:
        for handle in self.retry.values():
            handle.cancel()
        for task in [*self.timers.values(), *self.tasks]:
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
        return {p.id: p.role for p in await self.repo.participants()}

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
        if owner:
            view.update(kind=message.kind, hop=message.hop, turn_id=message.turn_id, mentions=message.mentions)
        return view

    @staticmethod
    def turn_view(turn: Turn) -> dict:
        return {"id": turn.id, "agent": turn.agent_id, "conversation_id": turn.conversation_id,
                "trigger_message_id": turn.trigger_msg_id, "state": turn.state, "hop": turn.hop,
                "cost_usd": turn.cost_usd, "tokens_in": turn.tokens_in, "tokens_out": turn.tokens_out,
                "created_at": turn.created_at, "started_at": turn.started_at,
                "finished_at": turn.finished_at, "error": turn.error}

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
        silent = body.strip() == "NO_REPLY"

        self_started = False
        turn_id = None
        if sender.is_owner:
            hop = 0
        else:
            if turn is None:
                sender = await self._agent(sender.id)        # fresh pause state
                if sender.paused:
                    raise HubError("paused", "the owner has paused you; stop posting", 403)
                if sender.self_driven:
                    turn = await repo.open_turn(sender.id, conversation.id)
                if turn is None and self.stopped.get((sender.id, conversation.id), 0) > time.monotonic():
                    raise HubError("stopped", "the owner stopped your turn in this conversation", 409)
            if turn is not None:
                hop, turn_id = turn.hop + 1, turn.id
            else:
                last = await repo.last_hop(conversation.id) or 0
                running = await repo.turns("agent_id = ? AND state = 'running'", (sender.id,))
                if running:                                   # a side post made during a turn
                    hop, turn_id = max(running[0].hop, last) + 1, running[0].id
                elif silent:
                    return {"stored": False, "note": "no open turn; nothing to close"}
                else:                                         # the agent starts an exchange itself
                    wait = self.rate.wait_time(sender.id)
                    if wait:
                        raise HubError("rate_limited", f"rate limit reached; retry in {wait:.0f}s", 429)
                    reason = await budgets.exhausted(repo, sender.id, self._budget(sender), conversation.id)
                    if reason:
                        await self._pause_for_budget(sender, conversation.id, reason)
                        raise HubError("paused", f"you are paused: {reason}", 403)
                    hop, self_started = last + 1, True

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
            self.rate.record(sender.id)
            started = await repo.add_turn(sender.id, conversation.id, message.id, state="done", hop=hop - 1)
            await repo.update_turn(started.id, started_at=message.created_at, finished_at=message.created_at)
        if turn is not None:
            await self._close(turn, "done")
        return {"stored": True, "id": message.id}

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
                    rest: list[str] = (), group: str | None = None) -> None:
        self.stopped.pop((agent_id, conversation_id), None)
        if not rest and not group:
            # A turn that has not been delivered yet will already carry this message as unread.
            waiting = await self.repo.turns(
                "agent_id = ? AND conversation_id = ? AND state IN ('queued', 'awaiting_approval')",
                (agent_id, conversation_id))
            if waiting:
                return
        turn = await self.repo.add_turn(agent_id, conversation_id, trigger, state=state, hop=hop,
                                        rest=rest, group_id=group)
        self._emit("turn", self.turn_view(turn))

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
        conversations = await self.repo.conversations(agent)
        return {"id": agent.id, "display_name": agent.display_name,
                "conversations": [c.id for c in conversations], "notice": delivery.NOTICES}

    async def list_conversations(self, viewer: Participant) -> list[dict]:
        unread = await self.repo.unread(viewer)
        pending = {t.conversation_id for t in await self.repo.turns(
            "agent_id = ? AND state IN ('queued','running')", (viewer.id,))}
        return [{"id": c.id, "kind": c.kind, "title": c.title, "members": c.members,
                 "unread": unread.get(c.id, 0), "pending_wake": c.id in pending}
                for c in await self.repo.conversations(viewer)]

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
            sender = await self.repo.participant(blocked.sender_id)
            if sender is None:
                raise HubError("not_continuable", "the sender is no longer in the roster", 404)
            await self._route(blocked, conversation, sender, 0, by_owner=True)
            await self._flush()
