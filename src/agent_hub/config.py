"""Hub configuration: agents.yaml plus secrets from the environment (.env)."""

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s|m|h)?$")


def parse_duration(value) -> float:
    """'10m', '90s', 30 -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION.match(str(value).strip())
    if not match:
        raise ValueError(f"bad duration: {value!r}")
    return float(match.group(1)) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, None: 1}[match.group(2)]


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usd_per_day: float | None = None
    turns_per_day: int | None = None
    usd_per_conversation: float | None = None
    turns_per_conversation: int | None = None


# Claude Code permission modes the owner can pick for a claude-code agent. bypassPermissions is never allowed.
CLAUDE_MODES = ("plan", "manual", "acceptEdits", "auto")
DEFAULT_MODE = "plan"


class AgentConfig(BaseModel):
    # Connector-specific keys (workdir, api_base, model, ...) are kept as extras.
    model_config = ConfigDict(extra="allow")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    display_name: str | None = None
    connector: str = "echo"
    delivery: Literal["sse", "webhook", "poll"] | None = None
    description: str = ""
    token_env: str | None = None
    webhook_url_env: str | None = None
    webhook_key_env: str | None = None
    turn_timeout: float = 600
    budget: Budget = Budget()

    @field_validator("turn_timeout", mode="before")
    @classmethod
    def _timeout(cls, value):
        return parse_duration(value)

    @field_validator("id")
    @classmethod
    def _reserved(cls, value):
        if value in ("owner", "all"):
            raise ValueError(f"'{value}' is a reserved id")
        return value

    def model_post_init(self, _context) -> None:
        if self.connector == "claude-code":
            mode = (self.model_extra or {}).get("permission_mode", DEFAULT_MODE)
            if mode not in CLAUDE_MODES:
                raise ValueError(f"permission_mode of '{self.id}' must be one of {', '.join(CLAUDE_MODES)}")
        if self.delivery is None:
            self.delivery = "poll" if self.connector == "none" else "sse"
        if self.token_env is None:
            self.token_env = f"HUB_{self.id.upper().replace('-', '_')}_TOKEN"
        if self.display_name is None:
            self.display_name = self.id.capitalize()

    @property
    def self_driven(self) -> bool:
        return self.delivery != "sse"

    @property
    def permission_timeout(self) -> float:
        """How long a permission request waits for the owner before it counts as denied."""
        return parse_duration((self.model_extra or {}).get("permission_timeout", "10m"))

    @property
    def mode(self) -> str | None:
        """The permission mode from agents.yaml, for a claude-code agent."""
        if self.connector != "claude-code":
            return None
        return (self.model_extra or {}).get("permission_mode", DEFAULT_MODE)

    def extras(self) -> dict:
        return dict(self.model_extra or {})


class RoomConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = "room:main"
    title: str | None = None
    mode: Literal["mention", "round_robin", "moderated"] = "mention"
    fanout: Literal["sequential", "parallel"] = "sequential"
    moderator: str | None = None


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_hops: int = 8
    turns_per_minute: int = 6
    agent_all: bool = False          # may an agent's @all wake the others
    approval_mode: bool = False      # agent-to-agent wakes wait for the owner
    webhook_retries: int = 3
    webhook_backoff: float = 2.0     # seconds; doubles on each retry


class HubSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bind: str | list[str] = "127.0.0.1:8700"   # one "host:port", or a list to listen on several
    db: str = "./agent-hub.db"
    url: str | None = None           # address connectors dial; default http://<first bind>
    owner_name: str = "Owner"

    @field_validator("bind")
    @classmethod
    def _check_bind(cls, value: str | list[str]) -> str | list[str]:
        items = [value] if isinstance(value, str) else value
        if not items:
            raise ValueError("bind needs at least one host:port")
        for item in items:
            host, _, port = item.rpartition(":")
            if not host or not port.isdigit():
                raise ValueError(f"bind {item!r} is not host:port")
        return value

    @property
    def binds(self) -> list[tuple[str, int]]:
        items = [self.bind] if isinstance(self.bind, str) else self.bind
        return [(item.rpartition(":")[0].strip("[]"), int(item.rpartition(":")[2])) for item in items]

    @property
    def base_url(self) -> str:
        if self.url:
            return self.url.rstrip("/")
        host, port = self.binds[0]
        host = {"0.0.0.0": "127.0.0.1", "::": "[::1]"}.get(host, host)
        return f"http://{host}:{port}"


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hub: HubSection = HubSection()
    rooms: list[RoomConfig] = [RoomConfig()]
    limits: Limits = Limits()
    agents: list[AgentConfig] = []

    def agent(self, agent_id: str) -> AgentConfig:
        for agent in self.agents:
            if agent.id == agent_id:
                return agent
        raise KeyError(f"no agent '{agent_id}' in the config")

    @field_validator("agents")
    @classmethod
    def _unique(cls, agents):
        ids = [a.id for a in agents]
        if len(ids) != len(set(ids)):
            raise ValueError("agent ids must be unique")
        return agents


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines. Variables already in the environment win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


REPO_ROOT = Path(__file__).resolve().parents[2]


def find_config(path: str | Path | None = None) -> Path:
    """--config, then $AGENT_HUB_CONFIG, then ./agents.yaml, then agents.yaml at the repo root."""
    if path or os.environ.get("AGENT_HUB_CONFIG"):
        return Path(path or os.environ["AGENT_HUB_CONFIG"]).expanduser()
    for candidate in (Path("agents.yaml"), REPO_ROOT / "agents.yaml"):
        if candidate.is_file():
            return candidate
    return REPO_ROOT / "agents.yaml"


def load_config(path: str | Path | None = None) -> Config:
    path = find_config(path)
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found. Copy agents.example.yaml to agents.yaml and edit it.")
    load_dotenv(path.resolve().parent / ".env")
    config = Config.model_validate(yaml.safe_load(path.read_text()) or {})
    db = Path(config.hub.db).expanduser()
    if not db.is_absolute():
        config.hub.db = str(path.resolve().parent / db)
    return config
