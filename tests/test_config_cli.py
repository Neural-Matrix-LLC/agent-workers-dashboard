"""agents.yaml and .env loading, and the agent-hub command line."""

import asyncio
import os
import sys

import pytest
from pydantic import ValidationError

from agent_hub import cli
from agent_hub.config import AgentConfig, Config, HubSection, find_config, load_config, load_dotenv, parse_duration
from agent_hub.core.repo import Repo
from agent_hub.ids import hash_token


# ------------------------------------------------------------------ config
def test_parse_duration():
    assert [parse_duration(v) for v in (30, 2.5, "90s", "10m", "1.5h", "250ms", "45")] == [
        30, 2.5, 90, 600, 5400, 0.25, 45]
    with pytest.raises(ValueError):
        parse_duration("ten minutes")


def test_hub_bind_and_base_url():
    hub = HubSection(bind=["100.69.47.119:8700", "[::1]:8701"])
    assert hub.binds == [("100.69.47.119", 8700), ("::1", 8701)]
    assert hub.base_url == "http://100.69.47.119:8700"
    assert HubSection(bind="0.0.0.0:8700").base_url == "http://127.0.0.1:8700"
    assert HubSection(bind="127.0.0.1:8700", url="https://hub.example/").base_url == "https://hub.example"
    for bad in ("8700", "host:", "host:port", []):
        with pytest.raises(ValidationError):
            HubSection(bind=bad)


def test_agent_defaults():
    agent = AgentConfig(id="grok-bot", connector="none")
    assert (agent.delivery, agent.self_driven, agent.token_env, agent.display_name) == (
        "poll", True, "HUB_GROK_BOT_TOKEN", "Grok-bot")
    agent = AgentConfig(id="hermes", connector="hermes", turn_timeout="20m", api_base="http://h/v1")
    assert (agent.delivery, agent.turn_timeout, agent.extras(), agent.mode) == (
        "sse", 1200, {"api_base": "http://h/v1"}, None)
    claude = AgentConfig(id="claude", connector="claude-code")
    assert (claude.mode, claude.permission_timeout) == ("plan", 600)


@pytest.mark.parametrize("agent", [
    {"id": "owner"}, {"id": "all"}, {"id": "Claude"}, {"id": "1bot"},
    {"id": "claude", "connector": "claude-code", "permission_mode": "yolo"},
    {"id": "x", "delivery": "carrier-pigeon"}, {"id": "x", "budget": {"usd_per_week": 1}},
])
def test_bad_agents_are_refused(agent):
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(agent)


def test_config_refuses_duplicates_and_unknown_keys():
    with pytest.raises(ValidationError, match="unique"):
        Config.model_validate({"agents": [{"id": "a"}, {"id": "a"}]})
    with pytest.raises(ValidationError):
        Config.model_validate({"limits": {"max_hop": 3}})              # typo of max_hops
    with pytest.raises(KeyError, match="no agent 'nope'"):
        Config().agent("nope")


def test_dotenv_reads_values_and_never_overrides(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("# comment\nA_ONE=1\nA_TWO = 'two words'\nA_THREE=\"x=y\"\nA_SET=from-file\nnot a line\n")
    monkeypatch.setenv("A_SET", "from-env")
    for key in ("A_ONE", "A_TWO", "A_THREE"):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(path)
    assert [os.environ[k] for k in ("A_ONE", "A_TWO", "A_THREE", "A_SET")] == ["1", "two words", "x=y", "from-env"]
    load_dotenv(tmp_path / "missing.env")                              # no file is fine


def test_load_config_finds_the_file_and_places_the_db_next_to_it(tmp_path, monkeypatch):
    (tmp_path / "agents.yaml").write_text("hub:\n  db: data/hub.db\nagents:\n  - id: echo1\n")
    (tmp_path / ".env").write_text("CFG_TEST_SECRET=s3cret\n")
    monkeypatch.delenv("CFG_TEST_SECRET", raising=False)
    monkeypatch.setenv("AGENT_HUB_CONFIG", str(tmp_path / "agents.yaml"))
    assert find_config() == tmp_path / "agents.yaml"
    config = load_config()
    assert config.hub.db == str(tmp_path / "data" / "hub.db")
    assert os.environ["CFG_TEST_SECRET"] == "s3cret"
    assert [a.id for a in config.agents] == ["echo1"]
    with pytest.raises(FileNotFoundError, match="agents.example.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_empty_config_file_loads_defaults(tmp_path):
    (tmp_path / "agents.yaml").write_text("")
    assert load_config(tmp_path / "agents.yaml").hub.bind == "127.0.0.1:8700"


# ------------------------------------------------------------------ command line
CONFIG = """
hub:
  db: hub.db
agents:
  - id: coder
    connector: echo
  - id: grok
    connector: none
    delivery: poll
"""


@pytest.fixture
def run_cli(tmp_path, monkeypatch):
    (tmp_path / "agents.yaml").write_text(CONFIG)
    for key in ("HUB_OWNER_PASSWORD", "HUB_CODER_TOKEN", "HUB_GROK_TOKEN", "AGENT_HUB_CONFIG"):
        monkeypatch.delenv(key, raising=False)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["agent-hub", "--config", str(tmp_path / "agents.yaml"), *args])
        cli.main()

    return run


def test_serve_needs_the_owner_password(run_cli):
    with pytest.raises(SystemExit, match="HUB_OWNER_PASSWORD"):
        run_cli("serve")


def test_connect_explains_what_is_missing(run_cli):
    with pytest.raises(SystemExit, match="runs no connector process"):
        run_cli("connect", "grok")
    with pytest.raises(SystemExit, match="Set HUB_CODER_TOKEN"):
        run_cli("connect", "coder")
    with pytest.raises(SystemExit, match="no agent 'ghost'"):
        run_cli("connect", "ghost")


def test_token_stores_only_the_hash(run_cli, tmp_path, capsys):
    run_cli("token", "grok")
    out = capsys.readouterr().out
    token = next(line.strip() for line in out.splitlines() if len(line.strip()) == 64)
    assert "Authorization: Bearer <token>" in out

    async def stored():
        repo = await Repo.open(str(tmp_path / "hub.db"))
        try:
            return await repo.participant_by_token_hash(hash_token(token))
        finally:
            await repo.close()

    assert asyncio.run(stored()).id == "grok"
    assert token.encode() not in (tmp_path / "hub.db").read_bytes()


def test_missing_config_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["agent-hub", "--config", str(tmp_path / "none.yaml"), "serve"])
    with pytest.raises(SystemExit, match="Copy agents.example.yaml"):
        cli.main()
