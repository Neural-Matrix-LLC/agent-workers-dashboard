"""A bare model behind an OpenAI-compatible endpoint (design section 9.3).

There is no harness, so this connector is one: it keeps the history, and runs a small tool
loop in which the six hub tools are function calls executed against the hub's HTTP API.
"""

import json
import os

import httpx

from .base import Connector, TraceSink, TurnResult, Wake

SYSTEM = (
    'You are "{agent}", a participant in Agent Hub, a group chat of one human owner and several AI '
    "agents. {description} Your final answer to each prompt is posted for you; use the tools only to "
    "read history or to message someone else. Be concise."
)


def tool(name: str, description: str, properties: dict | None = None, required: list[str] = ()) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": list(required)}}}


TEXT = {"type": "string"}
TOOLS = [
    tool("whoami", "Your id and the conversations you belong to."),
    tool("list_participants", "Roster with status and a one-line description per participant."),
    tool("list_conversations", "Conversations you can read, with unread counts."),
    tool("read_messages", "Messages in a conversation. Without `after`, the unread ones.",
         {"conversation_id": TEXT, "after": TEXT, "limit": {"type": "integer"}}, ["conversation_id"]),
    tool("post_message", "Post to a conversation you belong to.",
         {"conversation_id": TEXT, "body": TEXT, "reply_to": TEXT}, ["conversation_id", "body"]),
    tool("send_dm", "Post to your direct-message conversation with a participant.",
         {"to": TEXT, "body": TEXT}, ["to", "body"]),
]


class OpenAICompatConnector(Connector):
    name = "openai-compat"

    async def _hub_tool(self, name: str, args: dict):
        api = "/agent/v1"
        if name == "whoami":
            response = await self.client.get(f"{api}/whoami")
        elif name == "list_participants":
            response = await self.client.get(f"{api}/participants")
        elif name == "list_conversations":
            response = await self.client.get(f"{api}/conversations")
        elif name == "read_messages":
            params = {k: args[k] for k in ("after", "limit") if args.get(k) is not None}
            response = await self.client.get(f"{api}/conversations/{args['conversation_id']}/messages", params=params)
        elif name == "post_message":
            response = await self.client.post(f"{api}/conversations/{args['conversation_id']}/messages",
                                              json={"body": args["body"], "reply_to": args.get("reply_to")})
        elif name == "send_dm":
            response = await self.client.post(f"{api}/dm/{args['to']}", json={"body": args["body"]})
        else:
            return {"error": f"unknown tool: {name}"}
        return response.json()

    async def run_turn(self, wake: Wake, trace: TraceSink) -> TurnResult:
        options = self.options
        state = self.load_state()
        history = state.get("history", [])
        history.append({"role": "user", "content": wake.prompt})
        system = options.get("system_prompt") or SYSTEM.format(
            agent=self.agent.id, description=self.agent.description)
        key = os.environ.get(options.get("api_key_env", "OPENAI_API_KEY"), "")
        tokens_in = tokens_out = 0
        text = ""
        timeout = httpx.Timeout(30, read=float(options.get("request_timeout", 600)))
        async with httpx.AsyncClient(timeout=timeout, headers={"Authorization": f"Bearer {key}"} if key else {}) as llm:
            for round in range(int(options.get("max_tool_rounds", 8)) + 1):
                request = {"model": options["model"], "messages": [{"role": "system", "content": system}, *history]}
                if round < int(options.get("max_tool_rounds", 8)):
                    request["tools"] = TOOLS              # the last round must answer in text
                if options.get("temperature") is not None:
                    request["temperature"] = options["temperature"]
                response = await llm.post(options["api_base"].rstrip("/") + "/chat/completions", json=request)
                if response.status_code >= 400:
                    raise RuntimeError(f"model API returned {response.status_code}: {response.text[:500]}")
                data = response.json()
                usage = data.get("usage") or {}
                tokens_in += usage.get("prompt_tokens") or 0
                tokens_out += usage.get("completion_tokens") or 0
                message = data["choices"][0]["message"]
                calls = message.get("tool_calls") or []
                history.append({"role": "assistant", "content": message.get("content") or "",
                                **({"tool_calls": calls} if calls else {})})
                if not calls:
                    text = (message.get("content") or "").strip()
                    break
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                        result = await self._hub_tool(name, args)
                    except (ValueError, KeyError, httpx.HTTPError) as error:
                        args, result = {}, {"error": f"{type(error).__name__}: {error}"}
                    await trace.emit("tool_call", {"tool": name, "args": args})
                    history.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
        await trace.emit("usage", {"tokens_in": tokens_in, "tokens_out": tokens_out})
        self.save_state({**state, "history": self._trim(history, int(options.get("history_messages", 60)))})
        return TurnResult(text or "NO_REPLY", None, tokens_in or None, tokens_out or None)

    @staticmethod
    def _trim(history: list[dict], keep: int) -> list[dict]:
        """Keep the newest messages, starting at a user message so no tool result is orphaned."""
        history = history[-keep:]
        while history and history[0]["role"] != "user":
            history.pop(0)
        return history
