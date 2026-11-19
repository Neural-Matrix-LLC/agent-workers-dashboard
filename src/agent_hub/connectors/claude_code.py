"""Claude Code, headless: one `claude -p` subprocess per turn, resuming one session per agent (9.1).

Each run gets:
- the permission mode the owner chose for this agent (plan, manual, acceptEdits, auto);
- `--permission-prompt-tool`, so anything that needs permission waits for the owner's Allow in the hub;
- a `--settings` JSON with deny and ask rules (secrets, force pushes, pushes) and the Bash sandbox;
- `--plugin-dir` with the agent-hub-coder skill, which loads only in hub turns.
"""

import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

from ..config import DEFAULT_MODE, REPO_ROOT
from .base import Connector, TraceSink, TurnResult, Wake, scrubbed_env, state_dir

log = logging.getLogger("agent_hub.connector.claude")

# Read-only tools and the hub tools run without asking. Edits and commands are left to the mode.
DEFAULT_TOOLS = ["mcp__hub__*", "Read", "Grep", "Glob"]
PERMISSION_TOOL = "mcp__hub_permission__approve"
PLUGIN_DIR = REPO_ROOT / "plugins" / "agent-hub-coder"
ALLOWED_DOMAINS = ["github.com", "*.github.com", "*.githubusercontent.com",
                   "pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]


def short(value, limit: int = 2000):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def safety_settings(options: dict, protected: list[str]) -> dict:
    """`protected` holds absolute path patterns (globs allowed) that Claude may neither read nor edit."""
    # Deny and ask rules plus the Bash sandbox, passed to every run with --settings.
    deny = [
        "Read(**/.env)", "Read(**/.env.*)", "Edit(**/.env)", "Edit(**/.env.*)",
        "Bash(sudo *)",
        "Bash(git push --force *)", "Bash(git push -f *)", "Bash(git push --force-with-lease *)",
        "Bash(git push * --force)", "Bash(git push * -f)",
        "Bash(git push * main)", "Bash(git push * master)",
        "Bash(git push *:main)", "Bash(git push *:master)",
        "Bash(git push *:refs/heads/main)", "Bash(git push *:refs/heads/master)",
    ]
    for path in protected:                       # the hub's own config, database, and connector state
        deny += [f"Read(/{path})", f"Edit(/{path})"]
    deny += list(options.get("deny", []))
    ask = ["Bash(git push *)", *options.get("ask", [])]
    settings: dict = {"permissions": {"deny": deny, "ask": ask}}
    if options.get("sandbox", True):
        settings["sandbox"] = {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            # git runs outside the sandbox so `git push` can use the SSH key; pushes still ask.
            "excludedCommands": list(options.get("unsandboxed_commands", ["git"])),
            "network": {"allowedDomains": ALLOWED_DOMAINS + list(options.get("allowed_domains", []))},
            "filesystem": {
                "denyRead": ["~/.ssh", "~/.aws", "~/.config/gh", "~/projects/**/.env", "~/projects/**/.env.*",
                             *protected],
                "allowWrite": list(options.get("allow_write", [])),
            },
        }
    return settings


class ClaudeCodeConnector(Connector):
    name = "claude-code"

    def __init__(self, *args):
        super().__init__(*args)
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.extra_protected: list[str] = []      # e.g. the hub's artifact store, set by `agent-hub connect`
        workdir = Path(self.options.get("workdir", ".")).expanduser()   # noqa: ASYNC240 (no disk access)
        if not workdir.is_dir():
            raise SystemExit(f"workdir {workdir} of agent '{self.agent.id}' does not exist. "
                             "Create it or change workdir in agents.yaml.")
        if not shutil.which(self.options.get("command", "claude")):
            raise SystemExit("The claude CLI is not on PATH. Install Claude Code and log in first.")
        if self.options.get("sandbox", True) and not (shutil.which("bwrap") and shutil.which("socat")):
            raise SystemExit("The Bash sandbox needs bubblewrap and socat: sudo apt install bubblewrap socat "
                             "(or set sandbox: false for this agent in agents.yaml).")
        broad = [t for t in self.options.get("allowed_tools", []) if t.split("(")[0] in ("Edit", "Write", "Bash")]
        if broad:
            log.warning("allowed_tools lists %s: those run without asking in every mode, MANUAL included",
                        ", ".join(broad))

    def _protected(self) -> list[str]:
        """The hub's config, secrets, database, and this connector's state (it holds the agent token)."""
        return [f"{REPO_ROOT}/agents.yaml", f"{REPO_ROOT}/.env", f"{REPO_ROOT}/agent-hub.db*",
                f"{REPO_ROOT}/artifacts/**", f"{state_dir()}/**", *self.extra_protected,
                *self.options.get("protect", [])]

    def _mcp_config(self, turn_id: str) -> Path:
        path = state_dir() / f"{self.agent.id}-hub-mcp.json"
        path.write_text(json.dumps({"mcpServers": {
            "hub": {"type": "http", "url": f"{self.hub_url}/mcp",
                    "headers": {"Authorization": f"Bearer {self.token}"}},
            "hub_permission": {
                "type": "stdio", "command": sys.executable, "args": ["-m", "agent_hub.permission_mcp"],
                "env": {"AGENT_HUB_URL": self.hub_url, "AGENT_HUB_TOKEN": self.token, "AGENT_HUB_TURN": turn_id,
                        "AGENT_HUB_PERMISSION_WAIT": str(int(self.agent.permission_timeout) + 60)}},
        }}))
        path.chmod(0o600)
        return path

    def _settings(self) -> Path:
        path = state_dir() / f"{self.agent.id}-settings.json"
        path.write_text(json.dumps(safety_settings(self.options, self._protected()), indent=1))
        path.chmod(0o600)
        return path

    def _command(self, session: str | None, wake: Wake) -> list[str]:
        options = self.options
        mode = wake.options.get("permission_mode") or options.get("permission_mode", DEFAULT_MODE)
        command = [
            options.get("command", "claude"), "-p",
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", str(self._mcp_config(wake.turn_id)), "--strict-mcp-config",
            "--permission-mode", mode,
            "--permission-prompt-tool", PERMISSION_TOOL,
            "--settings", str(self._settings()),
            "--allowedTools", ",".join(options.get("allowed_tools", DEFAULT_TOOLS)),
        ]
        plugin_dir = Path(options.get("plugin_dir", PLUGIN_DIR)).expanduser()
        if plugin_dir.is_dir():
            command += ["--plugin-dir", str(plugin_dir)]
        if options.get("model"):
            command += ["--model", options["model"]]
        if session:
            command += ["--resume", session]
        return command

    async def run_turn(self, wake: Wake, trace: TraceSink) -> TurnResult:
        state = self.load_state()
        result = await self._run(wake, trace, state.get("session_id"))
        if result is None and state.get("session_id"):
            # The stored session can be gone (cleared, other machine). Start a new one once.
            await trace.emit("log", {"note": "could not resume the session; starting a new one"})
            result = await self._run(wake, trace, None)
        if result is None:
            raise RuntimeError("claude exited without a result")
        # On a resumed session `total_cost_usd` is the session's running total, so the turn's own
        # cost is the difference from the total stored after the previous turn.
        total = result.get("total_cost_usd")
        cost = total
        resumed = result.get("session_id") == state.get("session_id")
        if total is not None and resumed and total >= state.get("cost_total", 0):
            cost = total - state.get("cost_total", 0)
        if result.get("session_id"):
            self.save_state({**state, "session_id": result["session_id"], "cost_total": total or 0})
        usage = result.get("usage") or {}
        await trace.emit("usage", {"cost_usd": cost, **{k: v for k, v in usage.items() if isinstance(v, int)}})
        if result.get("is_error"):
            raise RuntimeError(short(result.get("result") or result.get("subtype") or "claude reported an error"))
        tokens_in = sum(usage.get(k) or 0 for k in
                        ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return TurnResult(result.get("result") or "NO_REPLY", cost,
                          tokens_in or None, usage.get("output_tokens"))

    async def _run(self, wake: Wake, trace: TraceSink, session: str | None) -> dict | None:
        workdir = Path(self.options.get("workdir", ".")).expanduser()   # noqa: ASYNC240 (no disk access)
        proc = await asyncio.create_subprocess_exec(
            *self._command(session, wake), cwd=workdir,
            env={**scrubbed_env(), "MCP_TOOL_TIMEOUT": str(int(self.agent.permission_timeout + 120) * 1000)},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=32 * 1024 * 1024)
        self.procs[wake.turn_id] = proc
        result = None
        try:
            proc.stdin.write(wake.prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            stderr = asyncio.create_task(proc.stderr.read())
            async for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "result":
                    result = event
                else:
                    await self._trace(event, trace)
            code = await proc.wait()
            if code != 0 and result is None:
                await trace.emit("log", {"exit_code": code, "stderr": short((await stderr).decode(errors="replace"))})
            return result
        finally:
            self.procs.pop(wake.turn_id, None)
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()

    @staticmethod
    async def _trace(event: dict, trace: TraceSink) -> None:
        """Map stream-json events to hub trace events so the owner sees tool calls live."""
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for block in content:
            kind = block.get("type")
            if kind == "text" and block.get("text"):
                await trace.emit("text_delta", {"text": short(block["text"])})
            elif kind == "tool_use":
                await trace.emit("tool_call", {"tool": block.get("name"), "args": short(block.get("input"), 1000)})
            elif kind == "tool_result":
                await trace.emit("tool_result", {"result": short(block.get("content"), 1000),
                                                 "is_error": bool(block.get("is_error"))})

    async def cancel(self, turn_id: str) -> None:
        proc = self.procs.get(turn_id)
        if proc and proc.returncode is None:
            proc.terminate()
