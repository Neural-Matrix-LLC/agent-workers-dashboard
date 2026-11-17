"""Connector registry. To add a harness: implement run_turn, then add it here (design 9.6)."""

from .base import Connector
from .claude_code import ClaudeCodeConnector
from .echo import EchoConnector
from .hermes import HermesConnector
from .openai_compat import OpenAICompatConnector

CONNECTORS: dict[str, type[Connector]] = {
    c.name: c for c in (EchoConnector, ClaudeCodeConnector, HermesConnector, OpenAICompatConnector)
}
