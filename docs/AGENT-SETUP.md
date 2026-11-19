# Agent setup

How to install and connect each agent to a running hub. Installing and starting the hub itself is in the
[README](../README.md); using the agents from the chat UI is in [Using the chat UI](USING-THE-UI.md); the
reasons behind each connector are in the [design](AGENT-HUB-DESIGN.md), section 9.

This setup has two machines on one Tailscale network:

| Machine | Tailscale address | Runs |
|---|---|---|
| **ml3090** | `100.69.47.119` | The hub, and **Claude Code** as the coding agent, with its connector. |
| **ml3060** | `100.88.174.58` | **Hermes Agent** as the QA agent, with its connector. |

## How agents connect

The **hub is the MCP server**. Each agent is an **MCP client** of it: it calls the six hub tools
(`whoami`, `list_participants`, `list_conversations`, `read_messages`, `post_message`, `send_dm`) at
`http://100.69.47.119:8700/mcp` with its own token. An MCP client cannot be woken from outside, so each
agent also has a **connector**: a small process that holds a connection to the hub, starts the agent's
harness when the agent has a turn, and posts the answer back.

```
ml3090 (100.69.47.119)                                 ml3060 (100.88.174.58)
┌─────────────────────────────────────────┐            ┌──────────────────────────────────────┐
│ hub  (agent-hub serve)  :8700           │◀──── MCP ──│ Hermes gateway  127.0.0.1:8642        │
│   /mcp  /agent/v1  /api  UI             │            │   ▲  /v1/chat/completions             │
│        ▲                ▲ MCP           │◀─ events ──│ connector (agent-hub connect hermes)  │
│        │ events         │               │            │   runs tests in ~/projects, ~/qa      │
│ connector ──runs──▶ claude -p ──────────┘            └──────────────────────────────────────┘
│ (agent-hub connect claude)  works in /home/thomas/projects, pushes branches to GitHub
└─────────────────────────────────────────┘
```

You do **not** need `claude mcp serve` or `hermes mcp-serve`. Those make an agent's own tools available
to some other MCP client; they do not let the agent take work from the hub.

Every agent needs an entry in `agents.yaml` and a token: `HUB_<AGENT ID>_TOKEN` in the hub's `.env` (make
one with `openssl rand -hex 32`), and the same value wherever that agent's connector runs. Restart the hub
after changing `agents.yaml` or `.env`. Keys every agent entry accepts:

| Key | Meaning |
|---|---|
| `id` | Lower-case id, used in mentions (`@claude`) and conversation ids. |
| `connector` | `claude-code`, `hermes`, `openai-compat`, `echo`, or `none` (self-driven). |
| `description` | One line shown to the other agents by `list_participants`. |
| `budget` | Any of `usd_per_day`, `turns_per_day`, `usd_per_conversation`, `turns_per_conversation`. The day starts at 00:00 UTC. |
| `token_env` | Name of the token variable. Default `HUB_<ID>_TOKEN`. |
| `display_name` | Name shown in the UI. Default is the id, capitalized. |

A connector keeps its state between turns (session id, history) in `~/.local/state/agent-hub/` on its
machine. Set `AGENT_HUB_STATE_DIR` to move it.

## Claude Code on ml3090: the coding agent

Claude takes coding tasks from the hub and works only in projects under `/home/thomas/projects`. The
owner sets its mode on its card in the UI, and every action that needs permission waits for the owner's
**Allow**. It commits and pushes to a `claude/<topic>` branch only when told to, and it never pushes to
`main` or `master`.

### 1. Install

On ml3090:

1. **Claude Code:** check `claude --version` (2.1.311 or newer was tested). If it is missing, install it
   from <https://code.claude.com/docs/en/setup>. Run `claude` once and log in; the connector uses that
   login.
2. **Sandbox tools:** `sudo apt install bubblewrap socat`. The sandbox limits what shell commands can
   write and read (see [What protects the machine](#what-protects-the-machine)). Without these two the
   connector refuses to start, unless you set `sandbox: false`.
3. **GitHub push access** (only if Claude should push): the repos use HTTPS remotes, and pushes go over
   SSH with the key `~/.ssh/id_github` (already set for `github.com` in `~/.ssh/config`). Check it with
   `ssh -T git@github.com`, then make git use SSH for GitHub:

   ```
   git config --global url."git@github.com:".insteadOf "https://github.com/"
   ```

   The key must have write access to the repos Claude may push to. Pushes to `main` and `master` are
   refused by the connector's rules whatever the key allows.

### 2. Configure

In `agents.yaml` on ml3090:

```yaml
  - id: claude
    connector: claude-code
    description: Coding agent. Plans, edits, tests, and (when told) commits and pushes branches in /home/thomas/projects.
    workdir: /home/thomas/projects
    permission_mode: plan       # starting mode; change it on Claude's card
    sandbox: true
    permission_timeout: 10m
    budget: {usd_per_day: 10}
```

All options of the `claude-code` connector:

| Option | Default | Meaning |
|---|---|---|
| `workdir` | `.` | Where `claude` runs. File tools work inside it without extra permission. |
| `permission_mode` | `plan` | Starting mode: `plan`, `manual`, `acceptEdits`, or `auto`. The owner's choice on the card overrides it until this value is changed. `bypassPermissions` is refused. |
| `permission_timeout` | `10m` | How long an Allow/Deny request waits. No answer counts as Deny. |
| `sandbox` | `true` | Run shell commands in the Claude Code sandbox. |
| `model` | your default | Passed to `claude --model`, for example `sonnet` or `opus`. |
| `allowed_tools` | hub tools, `Read`, `Grep`, `Glob` | Tools that run without asking in **every** mode. Do not add `Edit`, `Write`, or `Bash` here: that would skip MANUAL's questions. The connector warns if you do. |
| `deny`, `ask` | — | Extra [permission rules](https://code.claude.com/docs/en/permissions), for example `deny: ["Bash(docker *)"]`. |
| `protect` | — | Extra absolute paths (globs allowed) Claude may neither read nor edit. The hub's `agents.yaml`, `.env`, database, and the connector's state are always protected. |
| `allow_write` | — | Extra folders sandboxed commands may write to. |
| `allowed_domains` | — | Extra domains sandboxed commands may reach (GitHub, PyPI, and npm are allowed). |
| `unsandboxed_commands` | `["git"]` | Commands that run outside the sandbox. `git` is there so `git push` can use the SSH key. |
| `plugin_dir` | `plugins/agent-hub-coder` | The plugin with the agent-hub-coder skill. |
| `command` | `claude` | The Claude Code executable. |

### 3. Token and start

1. Put `HUB_CLAUDE_TOKEN=<openssl rand -hex 32>` in `.env` and restart the hub.
2. Start the connector in a second terminal: `uv run agent-hub connect claude`. Claude's card turns from
   `offline` to `idle`.

To keep the hub and the connector running after you log out, use systemd user services. Create
`~/.config/systemd/user/agent-hub.service`:

```ini
[Unit]
Description=Agent Hub
After=network-online.target tailscaled.service

[Service]
WorkingDirectory=/home/thomas/projects/agent-workers-dashboard
ExecStart=/snap/bin/uv run agent-hub serve
Restart=on-failure

[Install]
WantedBy=default.target
```

and `~/.config/systemd/user/agent-hub-connect@.service` (one instance per agent):

```ini
[Unit]
Description=Agent Hub connector for %i
After=agent-hub.service

[Service]
WorkingDirectory=/home/thomas/projects/agent-workers-dashboard
ExecStart=/snap/bin/uv run agent-hub connect %i
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Then:

```
systemctl --user daemon-reload
systemctl --user enable --now agent-hub agent-hub-connect@claude
sudo loginctl enable-linger thomas          # keep user services running without a login session
journalctl --user -u agent-hub-connect@claude -f
```

Stop the hub with `systemctl --user stop agent-hub` before running it by hand.

### 4. Modes and Allow / Deny

Pick the mode in the **Mode** selector on Claude's card. It applies from Claude's next turn, and only the
owner can change it. Messages from other agents cannot.

| Mode on the card | Claude Code mode | What happens |
|---|---|---|
| **Plan (no edits)** | `plan` | Claude reads and researches, then replies with a plan and its open questions. No file changes, commits, or pushes. |
| **Manual (ask for everything)** | `manual` | Every edit and every command except read-only ones waits for your Allow. |
| **Edit automatically** | `acceptEdits` | Edits inside `workdir` run without asking. Other commands (tests, builds, git) wait for your Allow. |
| **Auto (safety check decides)** | `auto` | A classifier approves routine actions. Whatever it blocks waits for your Allow. |

In every mode, `git push` waits for your Allow, and the rules in the next section apply.

When Claude needs permission, its card shows a yellow box with the tool and the command or file, for
example `[includes: git push, git commit] git add … && git push -u origin claude/fix-login`, with
**Allow** and **Deny**. Claude waits for your click. Deny, a timeout (`permission_timeout`), or **Stop**
tells Claude the action was refused; it does not retry and says in its reply what it needed.

A usual sequence: **Plan** → read the plan, answer its questions → switch to **Edit automatically** →
"go ahead" → allow the test runs → "commit and push" → allow the push.

### What protects the machine

| Protection | How |
|---|---|
| Only `/home/thomas/projects` | `workdir` is the working directory. File tools outside it need permission, which reaches your card. With the sandbox, shell commands can write only inside `workdir` and temp folders. |
| No secrets | `.env` and `.env.*` anywhere cannot be read or edited, and `~/.ssh`, `~/.aws`, and `~/.config/gh` cannot be read by sandboxed commands. The hub's `agents.yaml`, `.env`, database, and task file store (`artifacts/`), and the connector's state (it holds Claude's hub token), are blocked for Claude's tools, so Claude reads task files only through the hub, under the hub's visibility rules. The connector also removes hub tokens and webhook keys from Claude's environment. |
| Safe git | `git push --force`, `-f`, `--force-with-lease`, and any push to `main` or `master` (including `HEAD:main`) are denied. Every other push asks. |
| No root | `sudo` is denied. |
| Hub tasks only | The skill loads only in hub turns (`--plugin-dir`), and only owner messages count as instructions. |
| Your own sessions untouched | Nothing is installed in `~/.claude`; the rules and the skill are passed per run. |

Deny rules match command patterns. A determined agent could get round a pattern (a script that pushes,
for example), which is what the sandbox and your Allow clicks are for. Keep the sandbox on.

### The skill

[`plugins/agent-hub-coder/skills/agent-hub-coder/SKILL.md`](../plugins/agent-hub-coder/skills/agent-hub-coder/SKILL.md)
holds Claude's standing instructions: who gives instructions, where it may work, what each mode means, the
branch, commit, and push rules, the bug-fix steps, handing work to QA, and the reply format. The
connector loads it with `--plugin-dir` on every turn; there is nothing to install. Edit the file to change
how Claude works; the next turn uses the new text.

### Test it

With a scratch repo, for example `git init /home/thomas/projects/hub-scratch`:

1. Mode **Plan**. DM Claude: `In hub-scratch, add hello.py that prints hello, with a pytest test.` →
   a plan, and no files change.
2. Mode **Manual**. `Go ahead.` → requests for `git switch -c claude/…` and the file write appear on the
   card. Deny one: Claude stops and says what it needed. Ask again and allow: the files appear.
3. `Show me the .env file.` (after creating one) → refused.
4. Mode **Edit automatically**. `Commit and push.` (with a GitHub remote) → the push waits for your Allow;
   after Allow the branch is on GitHub.

Each Claude turn costs money; set `model` and `budget` to match.

## Hermes on ml3060: the QA agent

Hermes fetches the branches Claude pushed, runs the tests and checks, and reports PASS or FAIL. It does
not change code or push. Everything in this section runs **on ml3060**, apart from the firewall rule.

### 1. Install or reinstall Hermes

```
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source ~/.bashrc
hermes setup                   # model provider, keys
```

To repair or upgrade an existing install, run `hermes update` first, and the install script again if
that is not enough. **Do not delete `~/.hermes`** to "start clean": it holds Hermes's configuration,
memory, and skills, and the Hermes documentation warns against removing it.

### 2. Turn on the API server

The connector talks to Hermes through its OpenAI-compatible API server. In `~/.hermes/.env`:

```
API_SERVER_ENABLED=true
API_SERVER_KEY=<openssl rand -hex 32>
```

Start it with `hermes gateway`. The log shows `API server listening on http://127.0.0.1:8642`. It stays
on `127.0.0.1`, because the connector runs on the same machine.

### 3. Give Hermes the hub tools (MCP)

In `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  hub:
    url: http://100.69.47.119:8700/mcp
    headers:
      Authorization: Bearer <value of HUB_HERMES_TOKEN>
```

Restart `hermes gateway`, then check the connection with `hermes mcp test hub`. Inside Hermes the tools
appear as `mcp_hub_whoami`, `mcp_hub_post_message`, and so on.

If the test cannot connect, the hub machine's firewall is the usual cause. On **ml3090**:

```
sudo ufw allow in on tailscale0 to any port 8700 proto tcp
```

### 4. Install the QA skill

The skill is [`skills/hermes-qa/SKILL.md`](../skills/hermes-qa/SKILL.md) in this repo. With the repo
cloned on ml3060 (step 6), link it so updates arrive with `git pull`:

```
mkdir -p ~/.hermes/skills/agent-hub-qa
ln -sf ~/projects/agent-workers-dashboard/skills/hermes-qa/SKILL.md ~/.hermes/skills/agent-hub-qa/SKILL.md
```

Check it is listed: ask Hermes `What skills do you have for Agent Hub QA?`, or use its `/skills` command.

### 5. GitHub access for the repos to test

Hermes needs read access to the repos Claude pushes:

1. Make an SSH key on ml3060 (`ssh-keygen -t ed25519 -f ~/.ssh/id_github`) and add the public key to
   GitHub: a read-only deploy key per repo, or your account's keys.
2. Point git at it in `~/.ssh/config` (`Host github.com`, `IdentityFile ~/.ssh/id_github`) and check
   with `ssh -T git@github.com`.
3. Clone the repos to test into `~/projects/`, or let Hermes clone them on the first request. It uses
   `git@github.com:Neural-Matrix-LLC/<repo>.git`, and tests each branch in a worktree under `~/qa/`.
4. Install what the projects need to run their tests (`uv`, Node.js, and so on).

### 6. Run the connector on ml3060

The connector for Hermes runs next to Hermes, so the API server never leaves `127.0.0.1`.

```
git clone git@github.com:Neural-Matrix-LLC/agent-workers-dashboard.git ~/projects/agent-workers-dashboard
cd ~/projects/agent-workers-dashboard
uv sync
scp ml3090:/home/thomas/projects/agent-workers-dashboard/agents.yaml .     # or copy it by hand
```

Create `.env` in that folder with only what this machine needs:

```
HUB_HERMES_TOKEN=<the same value as in the hub's .env on ml3090>
HERMES_API_KEY=<the API_SERVER_KEY from ~/.hermes/.env>
```

Then start it:

```
uv run agent-hub connect hermes --hub http://100.69.47.119:8700
```

Hermes's card in the UI turns from `offline` to `idle`. To keep it running, use the two systemd user
units from the Claude section with these changes: `ExecStart=<path of uv> run agent-hub connect hermes
--hub http://100.69.47.119:8700` in the connector unit, and a second unit with
`ExecStart=<path of hermes> gateway` for the Hermes API server (`which uv`, `which hermes` give the
paths). Run `sudo loginctl enable-linger <user>` on ml3060 too.

### 7. Test it

1. In the UI, DM Hermes: `Which QA skill do you use, and which hub tools can you see?` → it names
   agent-hub-qa and the `mcp_hub_*` tools.
2. With a branch Claude pushed: `@hermes please test hub-scratch branch claude/hello` in the room → a
   report starting with `QA hub-scratch @ claude/hello (<commit>): PASS` or `FAIL`.

Hermes reports no cost, so budget it in turns. If a turn fails, the trace on Hermes's card shows the API
answer, and the connector's terminal (or `journalctl --user -u agent-hub-connect@hermes`) shows the rest.

## Workflow: Claude fixes, Hermes tests

1. In the room, mode **Plan** on Claude's card: `@claude In <repo>, <bug description>. Find the cause and
   plan a fix.`
2. Read the plan. Switch Claude to **Edit automatically**: `@claude Go ahead. Fix it, commit, push the
   branch, and ask Hermes to test it.`
3. Allow the test runs and the push on Claude's card. Claude posts `@hermes please test <repo> branch
   claude/<topic>`, which gives Hermes a turn.
4. Hermes reports PASS or FAIL in the room. On FAIL it mentions `@claude` with the failures, and Claude
   gets a turn to fix them. The hop limit (`max_hops`, default 8) stops an endless loop; **Continue** on
   the notice lets it go on.
5. On PASS, review the branch on GitHub and merge it yourself. Neither agent merges.

## Grok Bot (`connector: none`)

Grok Bot cannot be called from outside, so it has no connector process. It calls `/mcp` itself and the hub
pings its webhook routine when it has a turn.

```yaml
- id: grok
  connector: none
  delivery: webhook                # or poll: no ping, Grok Bot checks on a schedule
  webhook_url_env: GROK_WEBHOOK_URL
  webhook_key_env: GROK_WEBHOOK_KEY
  turn_timeout: 10m
  description: Second opinions and critique.
  budget: {turns_per_day: 50}
```

1. Bind the hub to an address Grok Bot can reach (the hub machine's Tailscale address) in `agents.yaml`.
2. Put `HUB_GROK_TOKEN`, `GROK_WEBHOOK_URL`, and `GROK_WEBHOOK_KEY` in `.env`. The values used with the
   [test driver](GROK-BOT-TEST-DRIVER.md) work unchanged, so the connector already added in Grok Bot stays
   valid. Stop the test driver first: it uses the same port.
3. In Grok Bot, the connector, the webhook routine, and the skill are set up as described in
   [the skill](../skills/agent-hub/SKILL.md).
4. Ask Grok Bot to create a slow polling routine (every 15 minutes) that calls `list_conversations` and
   answers where `pending_wake` is true. It catches any ping that was lost.

What the owner's controls mean for Grok Bot: Pause stops the pings and makes its posts fail with `paused`.
Stop closes its open turn and refuses a late reply to it. The hub cannot cancel work inside Grok Bot and
does not see its cost.

## A bare model (`connector: openai-compat`)

For a model with no harness: Ollama, llama.cpp, or the xAI API. The connector keeps the history and runs
the tool loop itself.

```yaml
- id: local
  connector: openai-compat
  description: Local model for quick drafts.
  api_base: http://127.0.0.1:11434/v1
  model: qwen3:14b
  api_key_env: LOCAL_API_KEY       # optional
  temperature: 0.4                 # optional
  max_tool_rounds: 8               # default
  history_messages: 60             # default; older messages are dropped
  system_prompt: "..."             # optional; replaces the built-in one
```

The model must support OpenAI-style tool calls. Start it with `uv run agent-hub connect local`.

## Echo (`connector: echo`)

Replies with what it received. For trying the hub and the UI without spending anything.

```yaml
- id: echo
  connector: echo
  reply: "echo from {agent}: {body}"   # optional template
```

## Shared tasks for every agent

Every agent gets seven task tools next to the six chat tools: `create_task`, `claim_task`, `update_task`,
`list_tasks`, `get_task`, `attach_artifact`, and `read_artifact` (Claude Code sees them as
`mcp__hub__*`, Hermes as `mcp_hub_*`; the `openai-compat` connector offers them as function calls). The
skills tell each agent how to use them. How tasks look to you is in
[Using the chat UI: tasks](USING-THE-UI.md#tasks), and the full design is in
[M4 and M5 design](M4-M5-DESIGN.md).

- **Turn them off for an agent** that does not need them (each tool costs prompt space in every turn):

  ```yaml
  agents:
    - id: local
      connector: openai-compat
      task_tools: false
  ```

  The tools then disappear from its tool list and calls are refused with `not_enabled`.
- **Files** are stored in `artifacts/` next to the database (set `hub.artifacts_dir` to move it). Back it
  up with the database. Agents on other machines reach files only through the hub API.
- **Limits** (`limits.tasks` and `limits.artifacts` in `agents.yaml`) are shown, with their defaults, in
  `agents.example.yaml`.
- **Upgrading from an earlier version:** the hub migrates the database on its first start and keeps a
  copy first, named `agent-hub.db.bak-v1-<time>`. An agent named `hub` must be renamed: `hub` is now
  reserved for the hub's own task messages.

## Adding another harness

Subclass `Connector` in `src/agent_hub/connectors/`, implement `run_turn` (and `cancel` if the harness can
be interrupted), give the class a `name`, and add it to `CONNECTORS` in `connectors/__init__.py`. The base
class handles the wake stream, the trace, and posting the reply.
