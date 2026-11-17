"""SQLite storage. The only module that runs SQL.

Reads that return conversations or messages take a `viewer` and apply the filter from
`visibility`. Functions without a viewer are for the hub core's own bookkeeping (turns,
cursors, budgets) and never hand rows to an API caller directly.
"""

import json

import aiosqlite

from ..ids import new_id, now
from .models import Conversation, Message, Participant, Turn, not_found
from .visibility import conversation_filter, message_filter

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
  id            TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('owner','agent')),
  connector     TEXT,
  delivery      TEXT NOT NULL DEFAULT 'sse',
  token_hash    TEXT,
  status        TEXT NOT NULL DEFAULT 'offline',
  config_json   TEXT NOT NULL DEFAULT '{}',
  active        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS conversations (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('room','dm')),
  title       TEXT,
  mode        TEXT NOT NULL DEFAULT 'mention',
  config_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  participant_id  TEXT NOT NULL REFERENCES participants(id),
  read_cursor     TEXT,
  PRIMARY KEY (conversation_id, participant_id)
);
CREATE TABLE IF NOT EXISTS messages (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  sender_id       TEXT NOT NULL REFERENCES participants(id),
  kind            TEXT NOT NULL DEFAULT 'chat',
  body            TEXT NOT NULL,
  mentions_json   TEXT NOT NULL DEFAULT '[]',
  reply_to        TEXT REFERENCES messages(id),
  hop             INTEGER NOT NULL DEFAULT 0,
  turn_id         TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS turns (
  id              TEXT PRIMARY KEY,
  agent_id        TEXT NOT NULL REFERENCES participants(id),
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  trigger_msg_id  TEXT NOT NULL REFERENCES messages(id),
  state           TEXT NOT NULL,
  hop             INTEGER NOT NULL DEFAULT 0,
  rest_json       TEXT NOT NULL DEFAULT '[]',
  group_id        TEXT,
  held_body       TEXT,
  cost_usd        REAL, tokens_in INTEGER, tokens_out INTEGER,
  created_at      TEXT NOT NULL,
  started_at      TEXT, finished_at TEXT,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_agent ON turns(agent_id, state);
CREATE TABLE IF NOT EXISTS trace_events (
  id       TEXT PRIMARY KEY,
  turn_id  TEXT NOT NULL REFERENCES turns(id),
  type     TEXT NOT NULL,
  payload  TEXT NOT NULL,
  at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_turn ON trace_events(turn_id, id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS permission_requests (
  id         TEXT PRIMARY KEY,
  turn_id    TEXT NOT NULL REFERENCES turns(id),
  agent_id   TEXT NOT NULL REFERENCES participants(id),
  tool       TEXT NOT NULL,
  input_json TEXT NOT NULL,
  state      TEXT NOT NULL DEFAULT 'pending',   -- pending | allowed | denied
  reason     TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
"""


def dm_id(a: str, b: str) -> str:
    return "dm:" + ":".join(sorted((a, b)))


class Repo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    @classmethod
    async def open(cls, path: str) -> "Repo":
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA)
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self.db.close()

    async def _all(self, sql: str, params=()) -> list:
        async with self.db.execute(sql, params) as cursor:
            return await cursor.fetchall()

    async def _one(self, sql: str, params=()):
        async with self.db.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def _run(self, sql: str, params=()) -> None:
        await self.db.execute(sql, params)
        await self.db.commit()

    # --- meta -------------------------------------------------------------------------
    async def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = await self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_meta(self, key: str, value: str) -> None:
        await self._run("INSERT INTO meta(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # --- permission requests (Claude Code asking the owner) -----------------------------
    async def add_permission(self, turn_id: str, agent_id: str, tool: str, input: dict) -> str:
        id = new_id()
        await self._run("INSERT INTO permission_requests(id, turn_id, agent_id, tool, input_json, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (id, turn_id, agent_id, tool, json.dumps(input), now()))
        return id

    async def decide_permission(self, id: str, state: str, reason: str | None) -> None:
        await self._run("UPDATE permission_requests SET state = ?, reason = ?, decided_at = ? "
                        "WHERE id = ? AND state = 'pending'", (state, reason, now(), id))

    async def deny_stale_permissions(self) -> None:
        """Requests left pending when the hub stopped can never be answered."""
        await self._run("UPDATE permission_requests SET state = 'denied', reason = 'hub restarted', "
                        "decided_at = ? WHERE state = 'pending'", (now(),))

    # --- participants -----------------------------------------------------------------
    @staticmethod
    def _participant(row) -> Participant:
        return Participant(row["id"], row["display_name"], row["role"], row["connector"],
                           row["delivery"], row["status"] == "paused", json.loads(row["config_json"]))

    async def upsert_participant(self, id: str, display_name: str, role: str,
                                 connector: str | None, delivery: str, config: dict) -> None:
        # A budget or mode the owner set in the UI survives a restart, until the same setting in
        # agents.yaml changes.
        row = await self._one("SELECT config_json FROM participants WHERE id = ?", (id,))
        if row:
            saved = json.loads(row["config_json"])
            for key in ("budget", "mode"):
                if f"{key}_override" in saved and saved.get(f"{key}_base") == config.get(key):
                    config = {**config, f"{key}_override": saved[f"{key}_override"],
                              f"{key}_base": saved[f"{key}_base"]}
        await self._run(
            "INSERT INTO participants(id, display_name, role, connector, delivery, config_json, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name, "
            "role = excluded.role, connector = excluded.connector, delivery = excluded.delivery, "
            "config_json = excluded.config_json, active = 1",
            (id, display_name, role, connector, delivery, json.dumps(config)))

    async def deactivate_missing(self, keep: list[str]) -> None:
        marks = ",".join("?" * len(keep))
        await self._run(f"UPDATE participants SET active = 0, token_hash = NULL WHERE id NOT IN ({marks})", keep)

    async def participants(self) -> list[Participant]:
        rows = await self._all("SELECT * FROM participants WHERE active = 1 ORDER BY role DESC, rowid")
        return [self._participant(r) for r in rows]

    async def participant(self, id: str) -> Participant | None:
        row = await self._one("SELECT * FROM participants WHERE id = ? AND active = 1", (id,))
        return self._participant(row) if row else None

    async def participant_by_token_hash(self, token_hash: str) -> Participant | None:
        row = await self._one(
            "SELECT * FROM participants WHERE token_hash = ? AND role = 'agent' AND active = 1", (token_hash,))
        return self._participant(row) if row else None

    async def set_token_hash(self, id: str, token_hash: str) -> None:
        await self._run("UPDATE participants SET token_hash = ? WHERE id = ?", (token_hash, id))

    async def has_token(self, id: str) -> bool:
        row = await self._one("SELECT token_hash FROM participants WHERE id = ?", (id,))
        return bool(row and row["token_hash"])

    async def set_paused(self, id: str, paused: bool) -> None:
        await self._run("UPDATE participants SET status = ? WHERE id = ?", ("paused" if paused else "idle", id))

    async def set_participant_config(self, id: str, config: dict) -> None:
        await self._run("UPDATE participants SET config_json = ? WHERE id = ?", (json.dumps(config), id))

    # --- conversations ----------------------------------------------------------------
    async def _conversation(self, row) -> Conversation:
        members = await self._all(
            "SELECT participant_id FROM members WHERE conversation_id = ? ORDER BY rowid", (row["id"],))
        return Conversation(row["id"], row["kind"], row["title"], row["mode"], json.loads(row["config_json"]),
                            row["created_at"], [m["participant_id"] for m in members])

    async def ensure_room(self, id: str, title: str | None, mode: str, config: dict, members: list[str]) -> None:
        row = await self._one("SELECT config_json FROM conversations WHERE id = ?", (id,))
        if row is None:
            await self._run("INSERT INTO conversations(id, kind, title, mode, config_json, created_at) "
                            "VALUES (?, 'room', ?, ?, ?, ?)", (id, title, mode, json.dumps(config), now()))
        else:
            # The owner can change the mode at run time, so the stored mode is kept.
            await self._run("UPDATE conversations SET title = ?, config_json = ? WHERE id = ?",
                            (title, json.dumps(config), id))
        for member in members:
            await self.db.execute("INSERT OR IGNORE INTO members(conversation_id, participant_id) VALUES (?, ?)",
                                  (id, member))
        marks = ",".join("?" * len(members))
        await self.db.execute(
            f"DELETE FROM members WHERE conversation_id = ? AND participant_id NOT IN ({marks})", [id, *members])
        await self.db.commit()

    async def ensure_dm(self, a: str, b: str) -> str:
        id = dm_id(a, b)
        if await self._one("SELECT 1 FROM conversations WHERE id = ?", (id,)) is None:
            await self.db.execute("INSERT INTO conversations(id, kind, mode, created_at) VALUES (?, 'dm', 'dm', ?)",
                                  (id, now()))
            for member in sorted((a, b)):
                await self.db.execute("INSERT INTO members(conversation_id, participant_id) VALUES (?, ?)",
                                      (id, member))
            await self.db.commit()
        return id

    async def conversation(self, viewer: Participant, id: str) -> Conversation:
        """The conversation if the viewer may read it. Raises the same error as for a missing one."""
        clause, params = conversation_filter(viewer)
        row = await self._one(f"SELECT c.* FROM conversations c WHERE c.id = ? AND {clause}", [id, *params])
        if row is None:
            raise not_found(id)
        return await self._conversation(row)

    async def conversations(self, viewer: Participant) -> list[Conversation]:
        clause, params = conversation_filter(viewer)
        rows = await self._all(f"SELECT c.* FROM conversations c WHERE {clause} ORDER BY c.kind DESC, c.id", params)
        return [await self._conversation(r) for r in rows]

    async def set_mode(self, id: str, mode: str) -> None:
        await self._run("UPDATE conversations SET mode = ? WHERE id = ?", (mode, id))

    async def set_conversation_config(self, id: str, config: dict) -> None:
        await self._run("UPDATE conversations SET config_json = ? WHERE id = ?", (json.dumps(config), id))

    # --- messages ---------------------------------------------------------------------
    async def add_message(self, conversation_id: str, sender_id: str, body: str, *, kind: str = "chat",
                          mentions: list[str] = (), reply_to: str | None = None, hop: int = 0,
                          turn_id: str | None = None) -> Message:
        message = Message(new_id(), conversation_id, sender_id, kind, body, list(mentions), reply_to, hop,
                          turn_id, now())
        await self._run(
            "INSERT INTO messages(id, conversation_id, sender_id, kind, body, mentions_json, reply_to, hop, "
            "turn_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.id, conversation_id, sender_id, kind, body, json.dumps(message.mentions), reply_to, hop,
             turn_id, message.created_at))
        return message

    async def messages(self, viewer: Participant, conversation_id: str, *, after: str | None = None,
                       before: str | None = None, limit: int = 50, latest: bool = False) -> list[Message]:
        """Messages of one conversation in id order. `latest` returns the newest `limit` instead of the oldest."""
        clause, params = message_filter(viewer)
        sql = f"SELECT m.* FROM messages m WHERE m.conversation_id = ? AND {clause}"
        params = [conversation_id, *params]
        if after:
            sql += " AND m.id > ?"
            params.append(after)
        if before:
            sql += " AND m.id < ?"
            params.append(before)
        sql += f" ORDER BY m.id {'DESC' if latest else 'ASC'} LIMIT ?"
        rows = await self._all(sql, [*params, max(1, min(int(limit), 500))])
        found = [Message.from_row(r) for r in rows]
        return found[::-1] if latest else found

    async def message(self, viewer: Participant, id: str) -> Message | None:
        clause, params = message_filter(viewer)
        row = await self._one(f"SELECT m.* FROM messages m WHERE m.id = ? AND {clause}", [id, *params])
        return Message.from_row(row) if row else None

    async def activity(self, viewer: Participant, *, before: str | None = None, limit: int = 200) -> list[Message]:
        """Every message the viewer may read, across conversations, oldest first."""
        clause, params = message_filter(viewer)
        sql = f"SELECT m.* FROM messages m WHERE {clause}"
        if before:
            sql += " AND m.id < ?"
            params = [*params, before]
        rows = await self._all(sql + " ORDER BY m.id DESC LIMIT ?", [*params, max(1, min(int(limit), 500))])
        return [Message.from_row(r) for r in rows][::-1]

    async def counts(self, viewer: Participant) -> dict[str, tuple[int, str | None]]:
        """conversation id -> (message count, last message id), for readable conversations only."""
        clause, params = message_filter(viewer)
        rows = await self._all(f"SELECT m.conversation_id AS c, COUNT(*) AS n, MAX(m.id) AS last "
                               f"FROM messages m WHERE {clause} GROUP BY m.conversation_id", params)
        return {r["c"]: (r["n"], r["last"]) for r in rows}

    async def unread(self, viewer: Participant) -> dict[str, int]:
        """conversation id -> messages after the viewer's read cursor (members only)."""
        clause, params = message_filter(viewer)
        rows = await self._all(
            f"SELECT m.conversation_id AS c, COUNT(*) AS n FROM messages m JOIN members mb "
            f"ON mb.conversation_id = m.conversation_id AND mb.participant_id = ? "
            f"WHERE {clause} AND m.sender_id != ? AND (mb.read_cursor IS NULL OR m.id > mb.read_cursor) "
            f"GROUP BY m.conversation_id", [viewer.id, *params, viewer.id])
        return {r["c"]: r["n"] for r in rows}

    async def unread_messages(self, viewer: Participant, conversation_id: str, limit: int = 200) -> list[Message]:
        return await self.messages(viewer, conversation_id, after=await self.cursor(viewer.id, conversation_id),
                                   limit=limit)

    async def cursor(self, participant_id: str, conversation_id: str) -> str | None:
        row = await self._one("SELECT read_cursor FROM members WHERE conversation_id = ? AND participant_id = ?",
                              (conversation_id, participant_id))
        return row["read_cursor"] if row else None

    async def advance_cursor(self, participant_id: str, conversation_id: str, message_id: str) -> None:
        await self._run(
            "UPDATE members SET read_cursor = ? WHERE conversation_id = ? AND participant_id = ? "
            "AND (read_cursor IS NULL OR read_cursor < ?)", (message_id, conversation_id, participant_id, message_id))

    async def last_hop(self, conversation_id: str) -> int | None:
        row = await self._one("SELECT hop FROM messages WHERE conversation_id = ? AND kind = 'chat' "
                              "ORDER BY id DESC LIMIT 1", (conversation_id,))
        return row["hop"] if row else None

    async def last_agent_sender(self, conversation_id: str) -> str | None:
        row = await self._one(
            "SELECT m.sender_id FROM messages m JOIN participants p ON p.id = m.sender_id "
            "WHERE m.conversation_id = ? AND m.kind = 'chat' AND p.role = 'agent' ORDER BY m.id DESC LIMIT 1",
            (conversation_id,))
        return row["sender_id"] if row else None

    async def raw_message(self, id: str) -> Message | None:
        row = await self._one("SELECT * FROM messages WHERE id = ?", (id,))
        return Message.from_row(row) if row else None

    # --- turns ------------------------------------------------------------------------
    async def add_turn(self, agent_id: str, conversation_id: str, trigger_msg_id: str, *, state: str = "queued",
                       hop: int = 0, rest: list[str] = (), group_id: str | None = None) -> Turn:
        id, at = new_id(), now()
        await self._run(
            "INSERT INTO turns(id, agent_id, conversation_id, trigger_msg_id, state, hop, rest_json, group_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, agent_id, conversation_id, trigger_msg_id, state, hop, json.dumps(list(rest)), group_id, at))
        return await self.turn(id)

    async def turn(self, id: str) -> Turn | None:
        row = await self._one("SELECT * FROM turns WHERE id = ?", (id,))
        return Turn.from_row(row) if row else None

    async def update_turn(self, id: str, **fields) -> Turn:
        sets = ", ".join(f"{name} = ?" for name in fields)
        await self._run(f"UPDATE turns SET {sets} WHERE id = ?", [*fields.values(), id])
        return await self.turn(id)

    async def turns(self, where: str, params=(), order: str = "id") -> list[Turn]:
        return [Turn.from_row(r) for r in await self._all(f"SELECT * FROM turns WHERE {where} ORDER BY {order}", params)]

    async def open_turn(self, agent_id: str, conversation_id: str) -> Turn | None:
        found = await self.turns("agent_id = ? AND conversation_id = ? AND state IN ('queued','running')",
                                 (agent_id, conversation_id))
        return found[0] if found else None

    async def next_queued(self, agent_id: str) -> Turn | None:
        found = await self.turns("agent_id = ? AND state = 'queued'", (agent_id,))
        return found[0] if found else None

    async def last_turn(self, agent_id: str) -> Turn | None:
        row = await self._one("SELECT * FROM turns WHERE agent_id = ? ORDER BY id DESC LIMIT 1", (agent_id,))
        return Turn.from_row(row) if row else None

    async def recent_turns(self, limit: int = 50) -> list[Turn]:
        rows = await self._all("SELECT * FROM turns ORDER BY id DESC LIMIT ?", (limit,))
        return [Turn.from_row(r) for r in rows][::-1]

    async def usage(self, agent_id: str, *, since: str | None = None, conversation_id: str | None = None
                    ) -> tuple[int, float]:
        """(turns, USD) the agent has used. Turns still waiting for approval do not count."""
        sql = "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS usd FROM turns WHERE agent_id = ? " \
              "AND state NOT IN ('awaiting_approval', 'queued', 'cancelled')"
        params = [agent_id]
        if since:
            sql += " AND COALESCE(started_at, created_at) >= ?"
            params.append(since)
        if conversation_id:
            sql += " AND conversation_id = ?"
            params.append(conversation_id)
        row = await self._one(sql, params)
        return row["n"], row["usd"]

    # --- trace events (owner-only; only owner endpoints call these) -------------------
    async def add_trace(self, turn_id: str, type: str, payload) -> dict:
        event = {"id": new_id(), "turn_id": turn_id, "type": type, "payload": payload, "at": now()}
        await self._run("INSERT INTO trace_events(id, turn_id, type, payload, at) VALUES (?, ?, ?, ?, ?)",
                        (event["id"], turn_id, type, json.dumps(payload), event["at"]))
        return event

    async def trace(self, turn_id: str) -> list[dict]:
        rows = await self._all("SELECT * FROM trace_events WHERE turn_id = ? ORDER BY id", (turn_id,))
        return [{"id": r["id"], "turn_id": r["turn_id"], "type": r["type"],
                 "payload": json.loads(r["payload"]), "at": r["at"]} for r in rows]
