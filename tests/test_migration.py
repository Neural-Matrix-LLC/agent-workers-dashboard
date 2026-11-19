"""Schema migration (design M4 section 15.6: M-01 to M-04)."""

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_hub.config import AgentConfig, Config
from agent_hub.core.repo import SCHEMA_VERSION, Repo, SchemaTooNew

M3_SCHEMA = (Path(__file__).parent / "fixtures" / "m3_schema.sql").read_text()


def make_m3_database(path: Path) -> None:
    """A database as Agent Hub M3 left it, with a conversation, messages, a turn, and a trace."""
    db = sqlite3.connect(path)
    db.executescript(M3_SCHEMA)
    db.executescript("""
        INSERT INTO participants(id, display_name, role, connector, delivery, token_hash, status, config_json)
          VALUES ('owner', 'Owner', 'owner', NULL, 'sse', NULL, 'idle', '{}'),
                 ('claude', 'Claude', 'agent', 'claude-code', 'sse', 'abc', 'idle', '{"mode": "plan"}');
        INSERT INTO conversations(id, kind, title, mode, created_at) VALUES ('room:main', 'room', NULL, 'mention',
          '2026-11-17T00:00:00.000Z');
        INSERT INTO members(conversation_id, participant_id, read_cursor) VALUES ('room:main', 'owner', NULL),
          ('room:main', 'claude', 'M2');
        INSERT INTO messages(id, conversation_id, sender_id, body, mentions_json, hop, created_at)
          VALUES ('M1', 'room:main', 'owner', '@claude hi', '["claude"]', 0, '2026-11-17T00:00:01.000Z'),
                 ('M2', 'room:main', 'claude', 'hello', '[]', 1, '2026-11-17T00:00:02.000Z');
        INSERT INTO turns(id, agent_id, conversation_id, trigger_msg_id, state, hop, created_at)
          VALUES ('T1', 'claude', 'room:main', 'M1', 'done', 0, '2026-11-17T00:00:01.500Z');
        INSERT INTO trace_events(id, turn_id, type, payload, at) VALUES ('E1', 'T1', 'usage', '{}', 'now');
        INSERT INTO meta(key, value) VALUES ('secret', 's3cret');
    """)
    db.commit()
    db.close()


async def test_m01_m3_database_migrates_with_data_intact_and_a_backup(tmp_path):
    path = tmp_path / "hub.db"
    make_m3_database(path)
    repo = await Repo.open(str(path))
    try:
        assert await repo.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert await repo.get_meta("secret") == "s3cret"
        messages = await repo.messages(await repo.participant("owner"), "room:main")
        assert [(m.id, m.body, m.hop, m.task_id, m.actor_id, m.meta) for m in messages] == [
            ("M1", "@claude hi", 0, None, None, {}), ("M2", "hello", 1, None, None, {})]
        assert (await repo.turn("T1")).task_id is None
        claude = await repo.participant("claude")
        assert (claude.connector, claude.config) == ("claude-code", {"mode": "plan"})
        assert await repo.has_token("claude")
        assert await repo.cursor("claude", "room:main") == "M2"
        assert [p.id for p in await repo.participants()] == ["owner", "claude"]    # hub is not listed
        hub = await repo._one("SELECT role, active FROM participants WHERE id = 'hub'")
        assert (hub["role"], hub["active"]) == ("system", 1)
        assert await repo._all("PRAGMA foreign_key_check") == []
        assert {"tasks", "task_events", "artifacts"} <= {r["name"] for r in await repo._all(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "archived_at" in await repo._columns("conversations")
    finally:
        await repo.close()
    [backup] = list(tmp_path.glob("hub.db.bak-v1-*"))
    copy = sqlite3.connect(backup)
    assert copy.execute("SELECT COUNT(*) FROM messages").fetchone() == (2,)
    assert copy.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() is None
    copy.close()


async def test_m02_running_the_migration_again_is_a_no_op(tmp_path):
    path = tmp_path / "hub.db"
    make_m3_database(path)
    await (await Repo.open(str(path))).close()
    await (await Repo.open(str(path))).close()
    assert len(list(tmp_path.glob("hub.db.bak-*"))) == 1                 # only the first open backed up


async def test_new_install_needs_no_backup(tmp_path):
    repo = await Repo.open(str(tmp_path / "new.db"))
    await repo.close()
    assert list(tmp_path.glob("*.bak-*")) == []


async def test_m03_a_newer_schema_is_refused(tmp_path):
    path = tmp_path / "hub.db"
    repo = await Repo.open(str(path))
    await repo.set_meta("schema_version", "99")
    await repo.close()
    with pytest.raises(SchemaTooNew, match="schema version 99"):
        await Repo.open(str(path))


def test_m04_hub_is_a_reserved_agent_id():
    with pytest.raises(ValidationError, match="reserved"):
        AgentConfig(id="hub")
    with pytest.raises(ValidationError):
        Config.model_validate({"agents": [{"id": "hub", "connector": "echo"}]})
