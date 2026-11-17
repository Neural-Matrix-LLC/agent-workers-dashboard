"""Replies with what it received. For hub and UI development and tests (design section 9.5)."""

import asyncio

from .base import Connector, TraceSink, TurnResult, Wake


class EchoConnector(Connector):
    name = "echo"

    async def run_turn(self, wake: Wake, trace: TraceSink) -> TurnResult:
        trigger = next((m for m in wake.messages if m["id"] == wake.trigger_message_id), wake.messages[-1])
        await trace.emit("log", {"echo": f"{len(wake.messages)} message(s) received"})
        await asyncio.sleep(float(self.options.get("delay", 0.3)))
        template = self.options.get("reply", "echo from {agent}: {body}")
        return TurnResult(template.format(agent=self.agent.id, body=trigger["body"], sender=trigger["from"]))
