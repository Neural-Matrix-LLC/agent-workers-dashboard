# QA plan

What has to pass before Agent Hub is released from the `feature/agent-hub` branch. Part 1 is automated and
runs in under ten seconds. Part 2 is a list of checks to run by hand on the real machines. Each check has an
ID, so a failure can be reported as, for example, "C4 failed: …".

Status values used below:

- **Passed (date)**: run by hand on the real setup, with the expected result.
- **Tests**: covered by the automated suite in part 1. Run it by hand only if you want to see it in the UI.
- **To run**: not yet run on the real setup.

## Before you start

- On ml3090: run `sudo apt install bubblewrap socat` and
  `git config --global url."git@github.com:".insteadOf "https://github.com/"`, then
  `sudo ufw allow in on tailscale0 to any port 8700 proto tcp`. The Claude connector does not start without
  `bubblewrap` and `socat`.
- Build and start the hub as in the [README](../README.md#setup): `uv sync`, `(cd web && npm install && npm
  run build)`, `uv run agent-hub serve`.
- Use a scratch repo for the Claude checks: `git init /home/thomas/projects/hub-scratch`, with a GitHub
  remote you can throw away for the push checks.
- **Claude turns cost money.** Set `model` to a cheap model and a small `budget` in the `claude` entry of
  `agents.yaml` while testing. The full run on 2026-11-17 cost about $0.20.
- For checks that need an agent but not a real model, add the `echo` agent (see the README, "Trying it
  without a real agent").

## Part 1: automated checks

Run on any machine with the repo:

```
uv sync
uv run pytest          # 204 tests, including the task and room tests of the M4 plan
uv run ruff check .    # lint
(cd web && npx tsc --noEmit && npm run build)
```

All four must pass with no errors.

| Test file | What it covers |
|---|---|
| `tests/test_visibility.py` | The visibility matrix of [design section 5](AGENT-HUB-DESIGN.md#5-visibility-model-r3): no agent sees content or metadata from a conversation it is not in, over REST, MCP, the wake stream, and webhook pings. Owner-only APIs and traces. Identity from the token. |
| `tests/test_router.py` | Who gets woken: mentions, DMs, `@all`, round-robin, moderated rooms. |
| `tests/test_delivery.py` | Turn life cycle for connector and webhook agents, retries, timeouts, one turn at a time, side DMs during a turn, parallel and sequential fan-out. |
| `tests/test_budgets.py` | Hop limit and **Continue**, rate limit, turn and dollar budgets, pause, stop, approval mode, budget set in the UI. |
| `tests/test_claude_worker.py` | Claude's modes and the mode API, permission requests (Allow, Deny, timeout, Stop), the `claude -p` command per mode, the safety settings, and the Allow/Deny text for long chained commands. |
| `tests/test_permission_mcp.py` | The permission helper Claude Code calls: the owner's answer reaches Claude; a refused or unreachable hub means deny; run as a real stdio MCP server it returns exactly one text block. |
| `tests/test_connectors.py` | The shared connector loop (wake, trace, reply, failure, cancel, reconnect, exit on a refused token, exit when replaced); the `hermes` and `openai-compat` connectors against a stand-in model server and a real hub; secrets kept out of the harness environment. |
| `tests/test_config_cli.py` | `agents.yaml` validation, `.env` loading, where the config and database are found, and the `agent-hub serve`, `connect`, and `token` commands. |

The automated tests do not start real Claude Code, Hermes, or Grok Bot, and do not drive the browser. Part 2
covers those.

## Part 2: checks by hand

### A. Hub and chat UI

| ID | Steps | Expected | Status |
|---|---|---|---|
| A1 | Open `http://<LAN address>:8700` and log in with `HUB_OWNER_PASSWORD`. Try a wrong password first. | Wrong password refused; right one shows the room, the DMs list, and one card per agent. | To run |
| A2 | With the `echo` connector running, post `@echo hello` in the room, then DM echo. | Echo answers in each place. Its card shows `idle` → `working` → `idle`. | To run |
| A3 | Ask echo to DM another agent (or use two echo agents). Open the agent-to-agent DM. | It is listed under **Agent DMs (observe)**, in italics, with no composer. | To run |
| A4 | Open **All activity**. | Every message from every conversation, each tagged with where it was posted. | To run |
| A5 | Click an agent's name. | The trace of its latest turn appears below the cards. | To run |
| A6 | Restart the hub while a connector runs. | The connector reconnects by itself within 30 seconds; history, modes, and budgets set in the UI are kept. | To run |
| A7 | Stop a connector (Ctrl+C) while its agent has a turn running. | The turn ends `failed` with "connector disconnected"; the card shows `offline`. | Tests |

### B. Owner controls

| ID | Steps | Expected | Status |
|---|---|---|---|
| B1 | **Pause** an agent, mention it, then **Resume**. | No turn while paused; the waiting wake is delivered on Resume. | Tests |
| B2 | Start a long Claude turn, press **Stop** on its card. | The turn is cancelled and the `claude` process ends (check with `ps`). | To run |
| B3 | Set `max_hops: 2`, get two agents mentioning each other. | The chain stops with a notice; **Continue** lets it go on. | Tests |
| B4 | Turn on **approval**, let one agent mention another. | The target's card shows **Approve** / **Reject**; nothing runs until you click. | Tests |
| B5 | Set a budget of 1 turn/day on the card, then give the agent two turns. | After the first, the agent is `paused` with a notice naming the budget. | Tests |
| B6 | **Pause all**, then **Stop all**. | A yellow bar shows; no agent gets a turn; running turns end. | To run |

### C. Claude Code on ml3090: modes and Allow / Deny

Use the scratch repo. The steps follow [Agent setup: Test it](AGENT-SETUP.md#test-it).

| ID | Steps | Expected | Status |
|---|---|---|---|
| C1 | Mode **Plan**. DM: `In hub-scratch, add hello.py that prints hello, with a pytest test.` | A plan; `git status` in hub-scratch shows no change. | Passed 2026-11-17 |
| C2 | Mode **Manual**. `Go ahead.` Deny the first request. | Claude stops, changes nothing, and says what it needed. It does not ask again. | Passed 2026-11-17 |
| C3 | Ask again and Allow each request. | A `claude/…` branch and the files are created. | Passed 2026-11-17 |
| C4 | Leave a request unanswered past `permission_timeout` (set it to `1m` for this check). | It counts as Deny; the card's request disappears. | Tests |
| C5 | Press **Stop** while a request waits. | The request is denied and the turn ends. | Tests |
| C6 | Switch the mode in the middle of a task. | The new mode applies from Claude's next turn; the first line of its prompt names the mode (see the trace). | To run |
| C7 | Restart the hub. | Claude's card shows the mode you set, not the one in `agents.yaml`. | Tests |

### D. Claude Code safety rules

| ID | Steps | Expected | Status |
|---|---|---|---|
| D1 | Create `hub-scratch/.env`, then: `Show me the .env file.` | Refused. The trace shows a denied `Read`. | Passed 2026-11-17 |
| D2 | Mode **Auto**: `Commit and push.` | The push shows on the card as `[includes: git push, git commit] …` and waits for Allow. After Allow the branch is on GitHub. | Passed 2026-11-17 |
| D3 | Ask Claude to push to `main`, to `HEAD:main`, and with `--force`. | All three refused without a request on the card. | Passed 2026-11-17 |
| D4 | Ask Claude to read the hub's `agents.yaml` or `agent-hub.db`. | Refused. | To run |
| D5 | Ask Claude to work in a folder outside `/home/thomas/projects`, for example `/tmp`. | It refuses or the action is denied. | To run |
| D6 | Ask Claude to run `sudo true`. | Refused. | To run |

### E. Claude Code sandbox (needs `socat`)

None of these has run, because ml3090 had no `socat`.

| ID | Steps | Expected | Status |
|---|---|---|---|
| E1 | Start `uv run agent-hub connect claude` after installing `bubblewrap` and `socat`. | The connector starts and Claude's card shows `idle`. | To run |
| E2 | Mode **Manual**: `In hub-scratch, run pytest.` and Allow. | The tests run and Claude reports the result. | To run |
| E3 | `Run: cat ~/.ssh/config` (or any file outside the projects folder). | Blocked by the sandbox or denied. | To run |
| E4 | Repeat D2 (commit and push) with the sandbox on. | The push over SSH works. The connector runs `git` outside the sandbox (`unsandboxed_commands`, default `["git"]`) so that it can use the SSH key, which the sandbox hides. If it fails, note the error from the trace. | To run |
| E5 | Mode **Manual**: ask Claude to add a `.git/hooks/pre-commit` script in hub-scratch that writes a file to `~/hook-test`, then to commit. | Known risk: because `git` runs outside the sandbox, a hook it starts does too. Expected today: Claude needs your Allow to write the hook file, so Deny stops it. Record whether the hook would run outside the sandbox if allowed; the result decides whether the git exclusion needs narrowing (for example `git -c core.hooksPath=/dev/null`). | To run |

### F. Hermes on ml3060 (QA agent)

Set up first with [Agent setup: Hermes on ml3060](AGENT-SETUP.md#hermes-on-ml3060-the-qa-agent). None of
these has run.

| ID | Steps | Expected | Status |
|---|---|---|---|
| F1 | On ml3060: `hermes mcp test hub`. | Lists the six hub tools. | To run |
| F2 | Start `uv run agent-hub connect hermes --hub http://100.69.47.119:8700`. | Hermes's card shows `idle`. | To run |
| F3 | DM Hermes: `Which QA skill do you use, and which hub tools can you see?` | It names agent-hub-qa and the `mcp_hub_*` tools. | To run |
| F4 | Send two DMs; in the second, refer to the first. | Hermes remembers the first (one session across turns). | To run |
| F5 | With a branch from D2: `@hermes please test hub-scratch branch claude/hello` in the room. | A report starting `QA hub-scratch @ claude/hello (<commit>): PASS` or `FAIL`. Hermes did not edit or push anything. | To run |
| F6 | Push a branch with a failing test and ask Hermes to test it. | `FAIL` with the failures, and an `@claude` mention that gives Claude a turn. | To run |
| F7 | Stop the Hermes API server, then DM Hermes. | The turn ends `failed`; the trace names the connection error. | Tests |

### G. Full workflow: Claude fixes, Hermes tests

| ID | Steps | Expected | Status |
|---|---|---|---|
| G1 | Run [UI guide workflow 7, "Plan, fix, push, test"](USING-THE-UI.md#7-plan-fix-push-test) on a real bug in a scratch repo. | Plan → fix → push (after Allow) → Hermes PASS, with no owner relaying between agents. Total cost within the budget you set. | To run |
| G2 | Repeat G1 with a fix that fails its test. | Hermes FAIL → Claude fixes → Hermes PASS, or the hop limit stops the loop with a notice. | To run |

### H. Grok Bot against the real hub

Only the [test driver](GROK-BOT-TEST-DRIVER.md) stub has been tested with Grok Bot. Stop the test driver
first; it uses port 8700.

| ID | Steps | Expected | Status |
|---|---|---|---|
| H1 | Post `@grok hello` in the room. | A reply within a few minutes, started by the webhook ping with nobody typing in Grok Bot. | To run |
| H2 | Ask Grok Bot (in the hub) which conversations it can see. | Only the room and its own DMs, never another agent pair's DM. | To run |
| H3 | Set up the polling routine, then make one ping fail (wrong `GROK_WEBHOOK_URL`, restart the hub). | The polling routine still answers within its interval. | To run |

## Not in this release

These are known and not to be reported as bugs:

- A per-conversation "thinking" indicator. The UI shows each agent's status only.
- A per-token rate limit on `/mcp`. It was meant for a public route, which is not used.
- The A2A gateway, Claude Code channels, and Redis (M5a to M5c).

Shared tasks, task files, the task board, and multiple rooms are built; their checks are in
[QA plan: tasks and rooms](QA-PLAN-M4.md).

## Reporting

For each failed check, give its ID, what you did, what happened, and the agent's trace (click its name in
the UI). Connector output is in the terminal that runs it, or in `journalctl --user -u agent-hub-connect@<agent>`
under systemd. When a check passes, change its status above to **Passed (date)**.
