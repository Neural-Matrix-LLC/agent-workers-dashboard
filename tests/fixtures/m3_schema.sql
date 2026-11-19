-- The schema of Agent Hub M3 (commit 526b1c6), used by tests/test_migration.py.
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
