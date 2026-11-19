"""Scripted agents for the end-to-end task tests. Each agent is a function called once per wake.

Connector agents get the wake payload and return their final answer (or "NO_REPLY"); the driver posts it as
the turn's finish, as a connector would. The webhook agent (grok) is called on each ping; it must close its
own turn with post_message, as Grok Bot does.
"""

from dataclasses import dataclass, field


@dataclass
class Driver:
    env: object
    connector_agents: dict            # agent id -> async fn(env, wake) -> reply text
    webhook_agents: dict = field(default_factory=dict)   # agent id -> async fn(env, ping body) -> None
    approve: bool = False             # approve every turn waiting for the owner (approval mode)
    wakes: list = field(default_factory=list)            # (agent, wake payload or ping body) in order
    queues: dict = field(default_factory=dict)
    pings_seen: int = 0
    approvals: int = 0

    async def connect(self) -> None:
        for agent in self.connector_agents:
            self.queues[agent] = await self.env.hub.connect(await self.env.agent(agent))

    async def run(self, rounds: int = 50) -> None:
        """Deliver wakes until nothing is left to do."""
        env = self.env
        for _ in range(rounds):
            progressed = False
            if self.approve:
                for turn in await env.hub.repo.turns("state = 'awaiting_approval'"):
                    response = await env.http.post(f"/api/turns/{turn.id}/approve")
                    assert response.status_code == 200, response.text
                    self.approvals += 1
                    progressed = True
            for agent, queue in self.queues.items():
                while not queue.empty():
                    event = queue.get_nowait()
                    if event["event"] != "wake":
                        continue
                    wake = event["data"]
                    self.wakes.append((agent, wake))
                    reply = await self.connector_agents[agent](env, wake)
                    response = await env.post(agent, f"/turns/{wake['turn_id']}/finish", text=reply or "NO_REPLY")
                    assert response.status_code == 200, response.text
                    progressed = True
            await env.settle()
            while self.pings_seen < len(env.pings):
                ping = env.pings[self.pings_seen]["body"]
                self.pings_seen += 1
                self.wakes.append((ping["agent"], ping))
                await self.webhook_agents[ping["agent"]](env, ping)
                progressed = True
            if not progressed:
                return
        raise AssertionError("the agents did not settle")
