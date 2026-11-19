"""SQLite storage. The only module that runs SQL.

Reads that return conversations or messages take a `viewer` and apply the filter from
`visibility`. Functions without a viewer are for the hub core's own bookkeeping (turns,
cursors, budgets) and never hand rows to an API caller directly.
"""

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from ..ids import new_id, now
from .models import TERMINAL, Artifact, Conversation, Message, Participant, Task, TaskEvent, Turn, not_found
from .visibility import artifact_filter, conversation_filter, message_filter, task_filter

log = logging.getLogger("agent_hub.repo")

SCHEMA_VERSION = 3

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


# Schema version 2 (M4, shared tasks). Applied by `_migrate` in one transaction.
PARTICIPANTS_V2 = """
CREATE TABLE participants_new (
  id            TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('owner','agent','system')),
  connector     TEXT,
  delivery      TEXT NOT NULL DEFAULT 'sse',
  token_hash    TEXT,
  status        TEXT NOT NULL DEFAULT 'offline',
  config_json   TEXT NOT NULL DEFAULT '{}',
  active        INTEGER NOT NULL DEFAULT 1
);
INSERT INTO participants_new SELECT id, display_name, role, connector, delivery, token_hash, status,
  config_json, active FROM participants;
DROP TABLE participants;
ALTER TABLE participants_new RENAME TO participants;
INSERT OR IGNORE INTO participants(id, display_name, role, connector, delivery, status)
  VALUES ('hub', 'Hub', 'system', 'none', 'poll', 'idle');
"""

TASKS_V2 = """
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id) WHERE task_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS tasks (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  number          INTEGER NOT NULL,
  parent_id       TEXT REFERENCES tasks(id),
  depth           INTEGER NOT NULL DEFAULT 0,
  title           TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  acceptance      TEXT,
  priority        TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high')),
  labels_json     TEXT NOT NULL DEFAULT '[]',
  creator_id      TEXT NOT NULL REFERENCES participants(id),
  assignee_id     TEXT REFERENCES participants(id),
  state           TEXT NOT NULL CHECK (state IN
                    ('open','assigned','in_progress','blocked','done','failed','cancelled')),
  result          TEXT,
  blocked_reason  TEXT,
  reassignments   INTEGER NOT NULL DEFAULT 0,
  version         INTEGER NOT NULL DEFAULT 1,
  hop             INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  closed_at       TEXT,
  UNIQUE (conversation_id, number)
);
CREATE INDEX IF NOT EXISTS idx_tasks_conv     ON tasks(conversation_id, state);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id, state);
CREATE INDEX IF NOT EXISTS idx_tasks_parent   ON tasks(parent_id);
CREATE TABLE IF NOT EXISTS task_events (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(id),
  actor_id    TEXT NOT NULL REFERENCES participants(id),
  type        TEXT NOT NULL,
  from_state  TEXT,
  to_state    TEXT,
  note        TEXT,
  data_json   TEXT NOT NULL DEFAULT '{}',
  message_id  TEXT REFERENCES messages(id),
  hop         INTEGER NOT NULL,
  at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);
CREATE TABLE IF NOT EXISTS artifacts (
  id              TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL REFERENCES tasks(id),
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  uploader_id     TEXT NOT NULL REFERENCES participants(id),
  name            TEXT NOT NULL,
  description     TEXT,
  media_type      TEXT NOT NULL,
  size            INTEGER NOT NULL,
  sha256          TEXT NOT NULL,
  supersedes      TEXT REFERENCES artifacts(id),
  message_id      TEXT REFERENCES messages(id),
  created_at      TEXT NOT NULL,
  deleted_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, id);
CREATE INDEX IF NOT EXISTS idx_artifacts_sha  ON artifacts(sha256);
"""

# Columns added by migrations: (table, column, definition, schema version).
ADDED_COLUMNS = [
    ("messages", "task_id", "TEXT REFERENCES tasks(id)", 2),
    ("messages", "actor_id", "TEXT REFERENCES participants(id)", 2),
    ("messages", "meta_json", "TEXT NOT NULL DEFAULT '{}'", 2),
    ("turns", "task_id", "TEXT REFERENCES tasks(id)", 2),
    ("conversations", "archived_at", "TEXT", 3),              # M5d: archived rooms
]


class SchemaTooNew(RuntimeError):
    pass


def backup_database(path: str, version: int) -> Path | None:
    """Copy the database (WAL contents included) before a migration. Returns the copy's path."""
    if path == ":memory:" or not Path(path).is_file():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = Path(f"{path}.bak-v{version}-{stamp}")
    source, copy = sqlite3.connect(path), sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    target.chmod(0o600)
    return target


def dm_id(a: str, b: str) -> str:
    return "dm:" + ":".join(sorted((a, b)))


class Repo:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    @classmethod
    async def open(cls, path: str) -> "Repo":
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='participants'") as c:
                existed = await c.fetchone() is not None
            await db.executescript(SCHEMA)
            await db.commit()
            repo = cls(db)
            await repo._migrate(path, existed)
            await db.execute("PRAGMA foreign_keys=ON")
            return repo
        except BaseException:
            await db.close()
            raise

    async def _migrate(self, path: str, existed: bool) -> None:
        """Bring the schema to SCHEMA_VERSION (design M4 section 4.4). A newer schema is refused."""
        version = int(await self.get_meta("schema_version", "1"))
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(f"the database {path} has schema version {version}, newer than this code "
                               f"({SCHEMA_VERSION}). Run the newer Agent Hub, or restore a backup.")
        if version == SCHEMA_VERSION:
            return
        if existed:
            copy = await asyncio.to_thread(backup_database, path, version)
            if copy:
                log.info("database backed up to %s before migrating from schema %d", copy, version)
        script = ["BEGIN;"]
        if version < 2:
            script.append(PARTICIPANTS_V2)
        for table, column, definition, since in ADDED_COLUMNS:
            if version < since and column not in await self._columns(table):
                script.append(f"ALTER TABLE {table} ADD COLUMN {column} {definition};")
        if version < 2:
            script.append(TASKS_V2)
        script.append(f"INSERT INTO meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}') "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value;")
        script.append("COMMIT;")
        await self.db.execute("PRAGMA foreign_keys=OFF")        # needed to rebuild participants
        try:
            await self.db.executescript("\n".join(script))
        except BaseException:
            await self.db.rollback()
            raise
        problems = await self._all("PRAGMA foreign_key_check")
        if problems:
            raise RuntimeError(f"foreign key check failed after the migration: {[tuple(p) for p in problems]}")

    async def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in await self._all(f"PRAGMA table_info({table})")}

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
        keep = [*keep, "hub"]
        marks = ",".join("?" * len(keep))
        await self._run(f"UPDATE participants SET active = 0, token_hash = NULL WHERE id NOT IN ({marks})", keep)

    async def participants(self) -> list[Participant]:
        """The owner and the agents. The system participant 'hub' is never listed."""
        rows = await self._all("SELECT * FROM participants WHERE active = 1 AND role != 'system' "
                               "ORDER BY role DESC, rowid")
        return [self._participant(r) for r in rows]

    async def participant(self, id: str) -> Participant | None:
        row = await self._one("SELECT * FROM participants WHERE id = ? AND active = 1 AND role != 'system'", (id,))
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
                            row["created_at"], [m["participant_id"] for m in members], row["archived_at"])

    async def ensure_room(self, id: str, title: str | None, mode: str, config: dict, members: list[str]) -> None:
        """Create or update a room and set its exact member list. Returns nothing; see set_members for diffs."""
        row = await self._one("SELECT config_json FROM conversations WHERE id = ?", (id,))
        if row is None:
            await self._run("INSERT INTO conversations(id, kind, title, mode, config_json, created_at) "
                            "VALUES (?, 'room', ?, ?, ?, ?)", (id, title, mode, json.dumps(config), now()))
        else:
            # The owner can change the mode at run time, so the stored mode is kept.
            await self._run("UPDATE conversations SET title = ?, config_json = ? WHERE id = ?",
                            (title, json.dumps(config), id))
        await self.set_members(id, members)

    async def set_members(self, id: str, members: list[str]) -> tuple[list[str], list[str]]:
        """Make `members` the room's member list. Returns (added, removed). A new member's read cursor starts
        at the room's latest message, so joining never produces a large first wake."""
        current = [r["participant_id"] for r in await self._all(
            "SELECT participant_id FROM members WHERE conversation_id = ?", (id,))]
        added = [m for m in members if m not in current]
        removed = [m for m in current if m not in members]
        latest = await self._one("SELECT MAX(id) AS id FROM messages WHERE conversation_id = ?", (id,))
        for member in added:
            await self.db.execute("INSERT OR IGNORE INTO members(conversation_id, participant_id, read_cursor) "
                                  "VALUES (?, ?, ?)", (id, member, latest["id"] if latest else None))
        for member in removed:
            await self.db.execute("DELETE FROM members WHERE conversation_id = ? AND participant_id = ?", (id, member))
        await self.db.commit()
        return added, removed

    async def room_exists(self, id: str) -> bool:
        return await self._one("SELECT 1 FROM conversations WHERE id = ?", (id,)) is not None

    async def set_room_fields(self, id: str, **fields) -> None:
        sets = ", ".join(f"{name} = ?" for name in fields)
        await self._run(f"UPDATE conversations SET {sets} WHERE id = ?", [*fields.values(), id])

    async def rooms(self) -> list[Conversation]:
        rows = await self._all("SELECT * FROM conversations WHERE kind = 'room' ORDER BY id")
        return [await self._conversation(r) for r in rows]

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
                          turn_id: str | None = None, task_id: str | None = None, actor_id: str | None = None,
                          meta: dict | None = None) -> Message:
        message = Message(new_id(), conversation_id, sender_id, kind, body, list(mentions), reply_to, hop,
                          turn_id, now(), task_id, actor_id, meta or {})
        await self._run(
            "INSERT INTO messages(id, conversation_id, sender_id, kind, body, mentions_json, reply_to, hop, "
            "turn_id, created_at, task_id, actor_id, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.id, conversation_id, sender_id, kind, body, json.dumps(message.mentions), reply_to, hop,
             turn_id, message.created_at, task_id, actor_id, json.dumps(message.meta)))
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
            f"WHERE {clause} AND m.sender_id != ? AND (m.actor_id IS NULL OR m.actor_id != ?) "
            f"AND (mb.read_cursor IS NULL OR m.id > mb.read_cursor) "
            f"GROUP BY m.conversation_id", [viewer.id, *params, viewer.id, viewer.id])
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
        row = await self._one("SELECT hop FROM messages WHERE conversation_id = ? AND kind IN ('chat', 'task') "
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
                       hop: int = 0, rest: list[str] = (), group_id: str | None = None,
                       task_id: str | None = None) -> Turn:
        id, at = new_id(), now()
        await self._run(
            "INSERT INTO turns(id, agent_id, conversation_id, trigger_msg_id, state, hop, rest_json, group_id, "
            "created_at, task_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, agent_id, conversation_id, trigger_msg_id, state, hop, json.dumps(list(rest)), group_id, at,
             task_id))
        return await self.turn(id)

    async def turn(self, id: str) -> Turn | None:
        row = await self._one("SELECT * FROM turns WHERE id = ?", (id,))
        return Turn.from_row(row) if row else None

    async def update_turn(self, id: str, **fields) -> Turn:
        sets = ", ".join(f"{name} = ?" for name in fields)
        await self._run(f"UPDATE turns SET {sets} WHERE id = ?", [*fields.values(), id])
        return await self.turn(id)

    async def turns(self, where: str, params=(), order: str = "id") -> list[Turn]:
        rows = await self._all(f"SELECT * FROM turns WHERE {where} ORDER BY {order}", params)
        return [Turn.from_row(r) for r in rows]

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

    # --- tasks (design M4 sections 4 to 6) ---------------------------------------------
    # Reads that take a `viewer` apply task_filter / artifact_filter. Methods named raw_* are for the
    # task service's own checks and never hand rows to a caller who has not passed a visibility check.
    async def add_task(self, *, conversation_id: str, parent_id: str | None, depth: int, title: str,
                       description: str, acceptance: str | None, priority: str, labels: list[str],
                       creator_id: str, assignee_id: str | None, state: str, hop: int) -> Task:
        id, at = new_id(), now()
        await self.db.execute(
            "INSERT INTO tasks(id, conversation_id, number, parent_id, depth, title, description, acceptance, "
            "priority, labels_json, creator_id, assignee_id, state, hop, created_at, updated_at) "
            "VALUES (?, ?, (SELECT COALESCE(MAX(number), 0) + 1 FROM tasks WHERE conversation_id = ?), "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, conversation_id, conversation_id, parent_id, depth, title, description, acceptance, priority,
             json.dumps(labels), creator_id, assignee_id, state, hop, at, at))
        await self.db.commit()
        return await self.raw_task(id)

    async def raw_task(self, id: str) -> Task | None:
        row = await self._one("SELECT * FROM tasks WHERE id = ?", (id,))
        return Task.from_row(row) if row else None

    async def task(self, viewer: Participant, id: str) -> Task | None:
        clause, params = task_filter(viewer)
        row = await self._one(f"SELECT t.* FROM tasks t WHERE t.id = ? AND {clause}", [id, *params])
        return Task.from_row(row) if row else None

    async def tasks(self, viewer: Participant, *, conversation_id: str | None = None, assignee: str | None = None,
                    unassigned: bool = False, states: list[str] | None = None, parent_id: str | None = None,
                    top_level: bool = False, q: str | None = None, before: Task | None = None,
                    limit: int = 50) -> list[Task]:
        """Newest change first (updated_at, then id). `before` pages after the given task."""
        clause, params = task_filter(viewer)
        sql = f"SELECT t.* FROM tasks t WHERE {clause}"
        if conversation_id:
            sql += " AND t.conversation_id = ?"
            params.append(conversation_id)
        if assignee:
            sql += " AND t.assignee_id = ?"
            params.append(assignee)
        if unassigned:
            sql += " AND t.assignee_id IS NULL"
        if states:
            sql += f" AND t.state IN ({','.join('?' * len(states))})"
            params.extend(states)
        if parent_id:
            sql += " AND t.parent_id = ?"
            params.append(parent_id)
        if top_level:
            sql += " AND t.parent_id IS NULL"
        if q:
            sql += " AND t.title LIKE ? ESCAPE '\\'"
            params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if before:
            sql += " AND (t.updated_at < ? OR (t.updated_at = ? AND t.id < ?))"
            params.extend([before.updated_at, before.updated_at, before.id])
        sql += " ORDER BY t.updated_at DESC, t.id DESC LIMIT ?"
        rows = await self._all(sql, [*params, max(1, min(int(limit), 500))])
        return [Task.from_row(r) for r in rows]

    async def task_counts(self, viewer: Participant) -> dict[str, int]:
        clause, params = task_filter(viewer)
        rows = await self._all(f"SELECT t.state AS s, COUNT(*) AS n FROM tasks t WHERE {clause} GROUP BY t.state",
                               params)
        found = {r["s"]: r["n"] for r in rows}
        return {state: found.get(state, 0) for state in
                ("open", "assigned", "in_progress", "blocked", "done", "failed", "cancelled")}

    async def children(self, viewer: Participant, parent_id: str) -> list[Task]:
        clause, params = task_filter(viewer)
        rows = await self._all(f"SELECT t.* FROM tasks t WHERE t.parent_id = ? AND {clause} ORDER BY t.id",
                               [parent_id, *params])
        return [Task.from_row(r) for r in rows]

    async def raw_children(self, parent_id: str) -> list[Task]:
        rows = await self._all("SELECT * FROM tasks WHERE parent_id = ? ORDER BY id", (parent_id,))
        return [Task.from_row(r) for r in rows]

    async def open_tasks_of(self, viewer: Participant, assignee_id: str, limit: int = 20) -> list[Task]:
        return await self.tasks(viewer, assignee=assignee_id,
                                states=["open", "assigned", "in_progress", "blocked"], limit=limit)

    async def count_open_assigned(self, assignee_id: str) -> int:
        marks = ",".join("?" * len(TERMINAL))
        row = await self._one(f"SELECT COUNT(*) AS n FROM tasks WHERE assignee_id = ? AND state NOT IN ({marks})",
                              (assignee_id, *TERMINAL))
        return row["n"]

    async def count_open_in(self, conversation_id: str) -> int:
        marks = ",".join("?" * len(TERMINAL))
        row = await self._one(
            f"SELECT COUNT(*) AS n FROM tasks WHERE conversation_id = ? AND state NOT IN ({marks})",
            (conversation_id, *TERMINAL))
        return row["n"]

    async def open_task_counts(self, viewer: Participant) -> dict[str, int]:
        """conversation id -> non-terminal tasks assigned to the viewer, in readable conversations."""
        clause, params = task_filter(viewer)
        marks = ",".join("?" * len(TERMINAL))
        rows = await self._all(
            f"SELECT t.conversation_id AS c, COUNT(*) AS n FROM tasks t WHERE {clause} AND t.assignee_id = ? "
            f"AND t.state NOT IN ({marks}) GROUP BY t.conversation_id", [*params, viewer.id, *TERMINAL])
        return {r["c"]: r["n"] for r in rows}

    async def count_task_creations(self, turn_id: str) -> int:
        row = await self._one("SELECT COUNT(*) AS n FROM messages WHERE turn_id = ? AND kind = 'task' "
                              "AND json_extract(meta_json, '$.event') = 'created'", (turn_id,))
        return row["n"]

    async def update_task_row(self, task: Task, **fields) -> Task:
        """Write fields and bump the version, if the row still has `task.version`. Returns the new row."""
        fields = {**fields, "version": task.version + 1, "updated_at": now()}
        if "labels" in fields:
            fields["labels_json"] = json.dumps(fields.pop("labels"))
        sets = ", ".join(f"{name} = ?" for name in fields)
        cursor = await self.db.execute(f"UPDATE tasks SET {sets} WHERE id = ? AND version = ?",
                                       [*fields.values(), task.id, task.version])
        await self.db.commit()
        if cursor.rowcount != 1:
            raise RuntimeError(f"task {task.id} changed under the hub lock")
        return await self.raw_task(task.id)

    async def claim_task_row(self, task_id: str, assignee_id: str, hop: int) -> bool:
        cursor = await self.db.execute(
            "UPDATE tasks SET state = 'assigned', assignee_id = ?, version = version + 1, hop = ?, updated_at = ? "
            "WHERE id = ? AND state = 'open' AND assignee_id IS NULL", (assignee_id, hop, now(), task_id))
        await self.db.commit()
        return cursor.rowcount == 1

    async def add_task_event(self, task_id: str, actor_id: str, type: str, *, hop: int, from_state: str | None = None,
                             to_state: str | None = None, note: str | None = None, data: dict | None = None,
                             message_id: str | None = None) -> TaskEvent:
        event = TaskEvent(new_id(), task_id, actor_id, type, from_state, to_state, note, data or {}, message_id,
                          hop, now())
        await self._run(
            "INSERT INTO task_events(id, task_id, actor_id, type, from_state, to_state, note, data_json, message_id, "
            "hop, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.id, task_id, actor_id, type, from_state, to_state, note, json.dumps(event.data), message_id,
             hop, event.at))
        return event

    async def task_events(self, viewer: Participant, task_id: str, limit: int = 50) -> list[TaskEvent]:
        """Events of a task the viewer can read, newest last. Private events are for the owner only."""
        clause, params = task_filter(viewer)
        sql = (f"SELECT e.* FROM task_events e JOIN tasks t ON t.id = e.task_id WHERE e.task_id = ? AND {clause}")
        if not viewer.is_owner:
            sql += " AND COALESCE(json_extract(e.data_json, '$.private'), 0) = 0"
        rows = await self._all(sql + " ORDER BY e.id DESC LIMIT ?", [task_id, *params, max(1, min(int(limit), 500))])
        return [TaskEvent.from_row(r) for r in rows][::-1]

    async def task_event_for_message(self, message_id: str) -> TaskEvent | None:
        row = await self._one("SELECT * FROM task_events WHERE message_id = ?", (message_id,))
        return TaskEvent.from_row(row) if row else None

    # --- artifacts --------------------------------------------------------------------
    async def add_artifact(self, *, task: Task, uploader_id: str, name: str, description: str | None,
                           media_type: str, size: int, sha256: str, supersedes: str | None) -> Artifact:
        id = new_id()
        await self._run(
            "INSERT INTO artifacts(id, task_id, conversation_id, uploader_id, name, description, media_type, size, "
            "sha256, supersedes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (id, task.id, task.conversation_id, uploader_id, name, description, media_type, size, sha256,
             supersedes, now()))
        return await self.raw_artifact(id)

    async def set_artifact_message(self, artifact_id: str, message_id: str) -> None:
        await self._run("UPDATE artifacts SET message_id = ? WHERE id = ?", (message_id, artifact_id))

    async def raw_artifact(self, id: str) -> Artifact | None:
        row = await self._one("SELECT * FROM artifacts WHERE id = ?", (id,))
        return Artifact.from_row(row) if row else None

    async def artifact(self, viewer: Participant, id: str) -> Artifact | None:
        clause, params = artifact_filter(viewer)
        row = await self._one(f"SELECT a.* FROM artifacts a WHERE a.id = ? AND {clause}", [id, *params])
        return Artifact.from_row(row) if row else None

    async def artifacts(self, viewer: Participant, task_id: str) -> list[Artifact]:
        clause, params = artifact_filter(viewer)
        rows = await self._all(f"SELECT a.* FROM artifacts a WHERE a.task_id = ? AND {clause} ORDER BY a.id",
                               [task_id, *params])
        return [Artifact.from_row(r) for r in rows]

    async def artifact_count(self, viewer: Participant, task_id: str) -> int:
        clause, params = artifact_filter(viewer)
        row = await self._one(f"SELECT COUNT(*) AS n FROM artifacts a WHERE a.task_id = ? AND {clause}",
                              [task_id, *params])
        return row["n"]

    async def task_artifact_usage(self, task_id: str) -> tuple[int, int]:
        """(live artifacts, their bytes) on a task."""
        row = await self._one("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM artifacts "
                              "WHERE task_id = ? AND deleted_at IS NULL", (task_id,))
        return row["n"], row["b"]

    async def store_bytes(self) -> int:
        """Bytes on disk: each live content hash counted once."""
        row = await self._one("SELECT COALESCE(SUM(size), 0) AS b FROM (SELECT sha256, MAX(size) AS size "
                              "FROM artifacts WHERE deleted_at IS NULL GROUP BY sha256)")
        return row["b"]

    async def live_hashes(self) -> set[str]:
        return {r["sha256"] for r in await self._all("SELECT DISTINCT sha256 FROM artifacts WHERE deleted_at IS NULL")}

    async def delete_artifact(self, id: str) -> None:
        await self._run("UPDATE artifacts SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (now(), id))

    async def token_hashes(self) -> set[str]:
        return {r["token_hash"] for r in await self._all(
            "SELECT token_hash FROM participants WHERE token_hash IS NOT NULL")}
