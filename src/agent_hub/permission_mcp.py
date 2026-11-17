"""Claude Code's permission prompt, answered by the owner in the hub UI.

The claude-code connector starts this as a stdio MCP server for each `claude -p` run and passes
`--permission-prompt-tool mcp__hub_permission__approve`. When Claude Code needs permission for a tool call,
it calls `approve`; this asks the hub, which shows Allow / Deny on the agent's card and answers when the
owner clicks, when the wait times out, or when the turn ends.

Run as `python -m agent_hub.permission_mcp` with AGENT_HUB_URL, AGENT_HUB_TOKEN, and AGENT_HUB_TURN set.
It runs only inside a hub turn and is not one of the six hub tools.
"""

import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hub_permission")


# Claude Code needs exactly one text block and no structuredContent.
@mcp.tool(structured_output=False)
async def approve(tool_name: str, input: dict[str, Any], tool_use_id: str | None = None) -> str:
    """Ask the hub owner whether Claude Code may run this tool call."""
    url, token, turn = (os.environ.get(k, "") for k in ("AGENT_HUB_URL", "AGENT_HUB_TOKEN", "AGENT_HUB_TURN"))
    wait = float(os.environ.get("AGENT_HUB_PERMISSION_WAIT", "900"))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=wait)) as client:
            response = await client.post(
                f"{url.rstrip('/')}/agent/v1/turns/{turn}/permission",
                headers={"Authorization": f"Bearer {token}"},
                json={"tool_name": tool_name, "input": input, "tool_use_id": tool_use_id})
        if response.status_code == 200:
            return json.dumps(response.json())
        reason = response.json().get("message", response.text) if response.content else response.status_code
    except httpx.HTTPError as error:
        reason = f"the hub could not be reached ({type(error).__name__})"
    return json.dumps({"behavior": "deny", "message": f"Denied: {reason}. Do not retry this action."})


if __name__ == "__main__":
    mcp.run()
