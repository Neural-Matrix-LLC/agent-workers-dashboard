"""The agent API as six MCP tools, served over streamable HTTP at /mcp (design section 8.1)."""

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from ..core.delivery import NOTICES
from ..core.models import HubError

INSTRUCTIONS = (
    "Agent Hub is a group chat of the owner and several agents. You are the participant your token "
    "belongs to; call whoami to see your id. When you are woken for a conversation, call read_messages "
    "for it, then answer with post_message in the same conversation. Post exactly NO_REPLY to stay "
    "silent. " + NOTICES
)


def build_mcp(state) -> FastMCP:
    """`state.hub` is set when the server starts. The caller is `scope['hub_agent']`, set by the gateway."""
    mcp = FastMCP(
        "agent-hub",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        # Identity is the bearer token. Agents reach the hub by overlay or LAN address, so the
        # Host header is not a fixed name.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def call(ctx: Context, tool: str, args: dict, run):
        agent = ctx.request_context.request.scope["hub_agent"]
        await state.hub.record_tool_call(agent, tool, args)
        try:
            return await run(agent)
        except HubError as error:
            raise ValueError(f"{error.code}: {error.message}") from None

    @mcp.tool()
    async def whoami(ctx: Context) -> dict[str, Any]:
        """Your id, display name, and the conversations you belong to."""
        return await call(ctx, "whoami", {}, state.hub.whoami)

    @mcp.tool()
    async def list_participants(ctx: Context) -> list[dict[str, Any]]:
        """Roster with status and a one-line description per participant."""
        return await call(ctx, "list_participants", {}, lambda agent: state.hub.roster())

    @mcp.tool()
    async def list_conversations(ctx: Context) -> list[dict[str, Any]]:
        """Conversations you can read. `unread` is a count; `pending_wake` is true where you have a turn."""
        return await call(ctx, "list_conversations", {}, state.hub.list_conversations)

    @mcp.tool()
    async def read_messages(ctx: Context, conversation_id: str, after: str | None = None,
                            limit: int = 50) -> list[dict[str, Any]]:
        """Messages in a conversation, oldest first. Without `after`, returns your unread messages.
        Pass a message id as `after` to read what follows it, or "0" to read from the beginning."""
        args = {"conversation_id": conversation_id, "after": after, "limit": limit}
        return await call(ctx, "read_messages", args,
                          lambda agent: state.hub.read_messages(agent, conversation_id, after, limit))

    @mcp.tool()
    async def post_message(ctx: Context, conversation_id: str, body: str,
                           reply_to: str | None = None) -> dict[str, Any]:
        """Post to a conversation you belong to. Mention a participant as @id to give them a turn.
        The exact body NO_REPLY closes your turn and stores nothing."""
        args = {"conversation_id": conversation_id, "body": body, "reply_to": reply_to}
        return await call(ctx, "post_message", args,
                          lambda agent: state.hub.post(agent, conversation_id, body, reply_to))

    @mcp.tool()
    async def send_dm(ctx: Context, to: str, body: str) -> dict[str, Any]:
        """Post to your direct-message conversation with participant `to`, creating it if needed."""
        return await call(ctx, "send_dm", {"to": to, "body": body},
                          lambda agent: state.hub.send_dm(agent, to, body))

    return mcp
