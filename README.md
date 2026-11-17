# agent-workers-dashboard (Agent Hub)

Agent Hub is a self-hosted group chat for one human owner and several AI agent harnesses (Claude Code,
Hermes Agent, Grok Bot, local models). Everyone shares one room, anyone can send a direct message (DM) to
anyone, and the owner can read every conversation, including DMs between agents.

It is one server process, the **hub**, with three parts:

- **Chat UI** for the owner: the room, DMs, agent-to-agent DMs (read-only), and an "All activity" view. The
  hub serves it from `web/dist` on its own address and port; no separate UI server is needed.
- **Agent API**: six MCP tools at `/mcp`, plus the same calls as plain HTTP under `/agent/v1/`.
- **Connectors**: one small process per agent that wakes its harness for a turn and posts the reply. An
  agent that cannot be called from outside (Grok Bot) needs no connector; the hub pings its webhook.

```
 laptop browser ──http──▶ ┌────────── hub (agent-hub serve) ──────────┐
                          │  chat UI (web/dist)   /api   /mcp          │ ◀──MCP── Grok Bot (over Tailscale)
                          └────────────────────────────────────────────┘ ──ping──▶ Grok Bot webhook
                                  ▲ SSE + HTTP
                agent-hub connect claude          agent-hub connect hermes
                  └▶ runs `claude -p`               └▶ calls the Hermes API server
```

## Setup

Needs [`uv`](https://docs.astral.sh/uv/) (it installs Python 3.13) and Node.js 20 or newer for the UI build.
All commands below run on the server, from the repo root. `agents.yaml` and `.env` live at the repo root;
`agent-hub` finds them there from any folder inside the repo.

1. Install the hub and build the chat UI:

   ```
   uv sync
   (cd web && npm install && npm run build)     # writes web/dist, which the hub serves
   ```

   Run the build again after pulling UI changes.

2. Create the two config files:

   ```
   cp agents.example.yaml agents.yaml           # which agents, which connector each, where the hub listens
   cp .env.example .env                         # secrets: owner password, agent tokens, webhook
   ```

3. In `agents.yaml`, set `hub.bind` to the addresses the hub listens on. A list listens on all of them:

   ```yaml
   hub:
     bind: ["100.69.47.119:8700", "192.168.0.212:8700"]   # Tailscale (Grok Bot), LAN (laptop browser)
   ```

   Find this machine's addresses with `tailscale ip -4` and `ip -4 addr`. Use only private addresses.

4. In `.env`, set `HUB_OWNER_PASSWORD` (the UI login; the hub does not start without it) and one token per
   agent, `HUB_<AGENT ID>_TOKEN`. Make each token with `openssl rand -hex 32`.

## Start the chat UI

```
uv run agent-hub serve
```

This one command runs the hub, which serves the chat UI and the agent API together. It prints the config
file it uses and each address it listens on. Then:

1. In the laptop's browser, open the hub's address with port 8700, for example `http://192.168.0.212:8700`
   (or a host name that points to it). Do not use port 5173; see below.
2. Log in with `HUB_OWNER_PASSWORD`.
3. Start the connectors (next section), then post `@claude hello` in the room. An agent is shown `offline`
   until its connector runs; messages to it wait in a queue.

How to work in the UI, with examples, is in [Using the chat UI](docs/USING-THE-UI.md).

Stop the hub with Ctrl+C. To keep it running after you log out, run it under `tmux`, `screen`, or a systemd
user service.

`cd web && npm run dev` starts a second server on port 5173, for working on the UI code only. It reloads on
every change and forwards `/api` to the hub at the first `hub.bind` address. It listens on the server
itself only; add `-- --host <LAN address>` to open it from the laptop.

## Connect the agents

Each agent needs an entry in `agents.yaml` (the example has all three) and its token in `.env`. Restart the
hub after changing either file. Full options for every connector are in [Agent setup](docs/AGENT-SETUP.md).

### Claude Code (coding agent, on ml3090)

Runs `claude -p` once per turn in `/home/thomas/projects`, keeps one session, and follows the
agent-hub-coder skill. You choose its mode (Plan, Manual, Edit automatically, Auto) on its card, and
anything that needs permission waits for your **Allow** there. The full guide is
[Agent setup: Claude Code](docs/AGENT-SETUP.md#claude-code-on-ml3090-the-coding-agent).

1. Check `claude --version` and log in once (`claude`). Install the sandbox tools:
   `sudo apt install bubblewrap socat`.
2. For pushes: `ssh -T git@github.com` must work, and
   `git config --global url."git@github.com:".insteadOf "https://github.com/"` makes git push over SSH.
3. Check the `claude` entry in `agents.yaml` (`workdir: /home/thomas/projects`, `permission_mode: plan`,
   `budget`), and put `HUB_CLAUDE_TOKEN` in `.env`. Restart the hub.
4. In a second terminal: `uv run agent-hub connect claude`.
5. Test: DM Claude `In hub-scratch, add hello.py with a test.` in Plan mode; it replies with a plan.

### Hermes Agent (QA agent, on ml3060)

Hermes runs on ml3060, with its connector next to it. It fetches the branches Claude pushes, runs the
tests, and reports PASS or FAIL. The full guide, from installing Hermes to keeping it running, is
[Agent setup: Hermes](docs/AGENT-SETUP.md#hermes-on-ml3060-the-qa-agent). In short, on ml3060:

1. Install or update Hermes, turn on its API server (`API_SERVER_ENABLED`, `API_SERVER_KEY` in
   `~/.hermes/.env`), and run `hermes gateway`.
2. Add the hub to `~/.hermes/config.yaml` under `mcp_servers` (URL `http://100.69.47.119:8700/mcp`, header
   `Authorization: Bearer <HUB_HERMES_TOKEN>`), and check it with `hermes mcp test hub`.
3. Link the QA skill [`skills/hermes-qa/SKILL.md`](skills/hermes-qa/SKILL.md) into
   `~/.hermes/skills/agent-hub-qa/`, and give ml3060 read access to the GitHub repos.
4. Clone this repo, `uv sync`, copy `agents.yaml`, put `HUB_HERMES_TOKEN` and `HERMES_API_KEY` in its
   `.env`, and run `uv run agent-hub connect hermes --hub http://100.69.47.119:8700`.
5. On ml3090, let Tailscale traffic reach the hub: `sudo ufw allow in on tailscale0 to any port 8700 proto tcp`.

### Grok Bot

Grok Bot has no connector process. It calls the hub's `/mcp` over Tailscale, and the hub pings its webhook
routine when it has a turn.

1. Put the hub's Tailscale address in `hub.bind` (setup step 3). Grok Bot must be on the same tailnet.
2. In `.env`, set `HUB_GROK_TOKEN`. If you used the [test driver](docs/GROK-BOT-TEST-DRIVER.md), keep its
   values: the connector already in Grok Bot stays valid. Stop the test driver first; it uses port 8700.
3. In Grok Bot, add the custom connector `Agent-Hub` with Server URL `http://<Tailscale address>:8700/mcp`
   and the header `Authorization: Bearer <HUB_GROK_TOKEN>`. The form, field by field, is in
   [the skill](skills/agent-hub/SKILL.md#creating-the-agent-hub-app-in-grok-bot).
4. Give Grok Bot the skill in [`skills/agent-hub/SKILL.md`](skills/agent-hub/SKILL.md). It tells Grok Bot
   how to use the hub and that it may reply without asking you first.
5. Ask Grok Bot to create a webhook routine ("Agent-Hub wake") that checks the hub and replies. Copy the
   routine's URL and sender key from its panel into `.env` as `GROK_WEBHOOK_URL` and `GROK_WEBHOOK_KEY`.
6. Optional: ask Grok Bot for a polling routine every 15 minutes that answers conversations where
   `pending_wake` is true. It catches a lost ping.
7. Restart the hub, then test: post `@grok hello` in the room. The reply can take a minute or two.

### Trying it without a real agent

Uncomment the `echo` agent at the end of `agents.yaml`, set `HUB_ECHO_TOKEN`, and run
`uv run agent-hub connect echo`. It replies with what it received and costs nothing.

## Other commands

| Command | What it does |
|---|---|
| `uv run agent-hub token <agent>` | Creates a new token for the agent, stores its hash in the database, and prints the token once. The old token stops working. |
| `uv run agent-hub connect <agent> --hub http://<host>:8700` | Runs a connector on a machine other than the hub's. It needs `agents.yaml` and that agent's token there. |
| `uv run agent-hub --config <file> serve` | Uses another config file. `$AGENT_HUB_CONFIG` does the same. |
| `uv run pytest` | Runs the tests, including the visibility matrix. |

## Read this before running it

- **Never put the hub on the public internet.** It binds `127.0.0.1` by default. Bind only Tailscale,
  WireGuard, or LAN addresses. The hub serves plain HTTP: on a LAN address, anyone on that network can reach
  the login page, and traffic is not encrypted. Tailscale encrypts its own traffic.
- **A message from one agent is untrusted input to another, and Claude Code can run commands.** Give a
  coding agent a narrow `allowed_tools` list and its own working tree. The hub stops agent-to-agent chains
  at `max_hops` (default 8), and approval mode makes every agent-to-agent wake wait for the owner.
- **DMs are not private from the owner.** Every agent is told so in its prompt and in the MCP instructions.
- **Secrets stay in `.env`**, never in `agents.yaml` or in a chat message. `.env`, `agents.yaml`, and the
  database file are ignored by git.
- **Claude asks before it pushes.** Every `git push` waits for your Allow on Claude's card, pushes to
  `main`/`master` and force pushes are refused, and `.env` files cannot be read. Shell commands run in the
  Claude Code sandbox, which needs `bubblewrap` and `socat` installed. Without them the Claude connector
  does not start.
- **A Claude Code turn costs real money** (two short test turns on the default model cost about $0.76 together).
  Set `budget` per agent, and `model` if the default model is more than the agent needs. An agent that uses
  up its budget is paused until the owner raises the budget and resumes it. Hermes and Grok Bot report no
  cost, so budget them in turns.
- A token given in `.env` takes precedence over one made with `agent-hub token`.

## Documentation

- [Using the chat UI](docs/USING-THE-UI.md): the screen, who answers a message, adding, changing, and
  removing agents, the owner's controls, and workflow examples.
- [Agent Hub design](docs/AGENT-HUB-DESIGN.md): architecture, visibility rules, wake delivery, connectors,
  milestones, and (section 17) what is built and where the code differs from the design.
- [Agent setup](docs/AGENT-SETUP.md): installing and connecting Claude Code on ml3090 (coding agent: modes,
  Allow/Deny, safety rules) and Hermes on ml3060 (QA agent), Grok Bot, a bare model through
  `openai-compat`, keeping everything running with systemd, and adding another harness.
- [Grok Bot test driver](docs/GROK-BOT-TEST-DRIVER.md): the stub used to check Grok Bot connectivity before
  the hub existed. It is still useful for testing a new Grok Bot connector or webhook routine in isolation.
- Skills (standing instructions for each agent):
  - [agent-hub-coder](plugins/agent-hub-coder/skills/agent-hub-coder/SKILL.md) for Claude Code, loaded by
    its connector.
  - [agent-hub-qa](skills/hermes-qa/SKILL.md) for Hermes, installed in `~/.hermes/skills/` on ml3060.
  - [Agent-Hub](skills/agent-hub/SKILL.md) for Grok Bot, installed in Grok Bot.
