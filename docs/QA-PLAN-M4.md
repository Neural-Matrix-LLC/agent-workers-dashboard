# QA plan: tasks and rooms (M4 and M5d)

What has to pass before shared tasks, task files, the task board (M4), and multiple rooms (M5d) are
merged from branch `claude/m4-tasks`. The features are described for users in
[Using the chat UI](USING-THE-UI.md#tasks) and in full in the [M4 and M5 design](M4-M5-DESIGN.md). The
checks for the earlier release (chat, Claude's modes, the sandbox, Hermes and Grok Bot setup) are in the
[QA plan](QA-PLAN.md) and still apply.

Each check has an ID, so a failure can be reported as, for example, "T-C3 failed: …". Status values:

- **Tests**: covered by the automated suite in part 1.
- **Checked (2026-11-19)**: run by hand in a headless browser against a real hub with an echo connector.
  Repeat it in a real browser.
- **To run**: not yet run.

## Before you start

- **Back up the hub's data first.** On ml3090, stop the hub and copy `agent-hub.db*` and `artifacts/` (if
  it exists). The first start of this version migrates the database and keeps its own copy
  (`agent-hub.db.bak-v1-<time>`), but take yours anyway.
- Check out the branch, then `uv sync` and `(cd web && npm install && npm run build)`.
- Rename any agent called `hub` in `agents.yaml`; the name is now reserved and the hub refuses to start.
- **Claude turns cost money.** Use a cheap `model` and a small `budget` for the checks in part 2-D.
- For checks that need agents but not real models, use two `echo` agents and the `none`/`poll` agent
  described in the README ("Trying it without a real agent"). Echo agents reply, but never call task tools;
  drive their task actions with the agent HTTP API (examples in T-D0).

## Part 1: automated checks

```
uv run pytest          # 204 tests
uv run ruff check .
(cd web && npm run build)      # type-checks, then builds
```

All must pass.

| Test file | What it covers (design section 15 IDs) |
|---|---|
| `tests/test_task_rules.py` | The transition table, required fields, field limits, cancel is final, and the task wake table (S-01 to S-04, S-08, S-09). |
| `tests/test_tasks_api.py` | Shapes and status codes of the seven tools and their HTTP mirrors, the owner routes, WebSocket events, `task_tools: false`, the `openai-compat` mapping, the prompt (C-01 to C-06); parents and children, cascade, version conflicts (S-05 to S-07); identity fields ignored (V-15); a claim race. |
| `tests/test_task_limits.py` | Hop limit and Continue for task wakes, reassignment, depth, child, open-task, and per-turn limits, budgets and pause, self-started rate limit, room modes, mentions, hops (L-01 to L-09). |
| `tests/test_artifacts.py` | Upload and download (MCP, JSON, multipart, Range), deduplication, size limits and quotas, type sniffing, every secret pattern, forbidden names, name cleaning, `supersedes`, soft delete and clean-up, paging, Claude kept out of the store (A-01 to A-11). |
| `tests/test_task_visibility.py` | The leak matrix for tasks and files: lists, details, parents, children, numbers, messages, private subtasks, deleted files, wakes, pings, open-task counts, error texts, assignees, owner-only routes (V-01 to V-14). |
| `tests/test_migration.py` | An M3 database (schema captured from commit 526b1c6) migrates with its data intact and a backup; a second start changes nothing; a newer schema is refused; `hub` is reserved (M-01 to M-04). |
| `tests/test_tasks_e2e.py` | The exit criterion with scripted agents: one owner message; the coordinator splits the work, the others report, the coordinator finishes; a failed subtask is reopened; block and unblock; a claim race; a private subtask; approval mode (E-01 to E-06). |
| `tests/test_rooms.py` | Rooms with different members on every surface, joining, leaving, archiving, restart, per-room modes (RM-01 to RM-06). |

The automated tests use scripted agents. They do not run Claude Code, Hermes, or Grok Bot, and they do not
use a real browser. Part 2 covers those.

## Part 2: checks by hand

### A. Upgrade

| ID | Steps | Expected | Status |
|---|---|---|---|
| T-A1 | Start the new version on a copy of the real `agent-hub.db` from ml3090. | It starts; the log names the backup file; old conversations, history, budgets, and Claude's mode are as before. | To run |
| T-A2 | Restart again. | No second backup; nothing changes. | Tests |
| T-A3 | Start the old version (`main`) on the migrated database. | Not supported (design section 14). Note what happens, but do not file it as a bug. | To run |

### B. Task board and drawer (UI)

Use a real browser (Chrome and Firefox), light and dark mode.

| ID | Steps | Expected | Status |
|---|---|---|---|
| T-B1 | Open **Tasks**. Create a task with **+ New task** in `# main`, assigned to an echo agent, with a label and a parent. | The card appears in Assigned with the label, the assignee's status dot, and "under <parent ref>". | Checked (2026-11-19) |
| T-B2 | Use each filter (conversation, assignee, label, search). Reload the page. | Cards filter at once; the filters are in the address and survive the reload. | Checked (2026-11-19) |
| T-B3 | Have an agent finish a task (T-D0) while the board is open in a second window. | The card moves to Done in both windows without a reload. | To run |
| T-B4 | Open a task. Use every action the drawer offers in each state (reassign, unassign, start, block, unblock, finish, fail, cancel, reopen, add note). | Only actions valid for the state are shown; each works and appears in History. Finish or cancel on a parent with open subtasks asks first. | Partly checked (note, reopen shown) |
| T-B5 | Open the same task in two tabs. Change it in tab 1, then act in tab 2. | Tab 2 reloads the task and says it was changed. | To run |
| T-B6 | A task in an agent-to-agent DM (`dm:claude:grok`). | The card is styled "observe"; the drawer offers only Reassign, Unassign, Cancel; no upload. | To run |
| T-B7 | A blocked or failed task you have not opened. | **Tasks** in the left list shows a count; opening the task clears it. | Checked (2026-11-19) |
| T-B8 | In a conversation, click a task ref in a task line. | The drawer opens on that task. | To run |
| T-B9 | Click "N open tasks" on an agent card. | The board opens filtered to that agent. | To run |
| T-B10 | Narrow the window to phone width. | No part of the page is cut off; the columns scroll sideways inside the board. | Checked (2026-11-19) |

### C. Task files

| ID | Steps | Expected | Status |
|---|---|---|---|
| T-C1 | Upload a Markdown file containing `<script>alert(1)</script>` and an image, then **View** each. | Markdown shows as formatted text with no script run; the image shows inline. | Checked (2026-11-19, Markdown) |
| T-C2 | Upload a PDF; **Download** it; open the file. | Download only (no View); the file opens and matches the original. | To run |
| T-C3 | Upload a 9 MB text file, then an 11 MB one. | The first works; the second is refused with a clear "larger than" error. | To run |
| T-C4 | Upload a `.zip`, an `.html`, and a file named `.env`. | Zip refused; HTML stored as plain text and shown as source; `.env` refused, with an owner notice. | Tests (HTML, zip, `.env`) |
| T-C5 | Upload a text file containing a fake AWS key `AKIAABCDEFGHIJKLMNOP`. | Refused; the error names the rule and line, never the key; a notice appears in the room. | Tests |
| T-C6 | **Remove** a file; check `artifacts/` on disk. | Agents can no longer read it; the drawer shows it as removed; the file on disk goes once nothing else uses the same content. | Tests |
| T-C7 | Attach real test output from your projects (pytest, npm). | Not refused. If it is (for example a log line like `PASSWORD=...` from a fixture), record the line and rule: it is a false positive to tune. | To run |

### D. Real agents

| ID | Steps | Expected | Status |
|---|---|---|---|
| T-D0 | (Echo agents.) Drive task actions with the agent API, for example `curl -H "Authorization: Bearer $HUB_HERMES_TOKEN" -H 'content-type: application/json' http://<hub>/agent/v1/tasks/<id> -X PATCH -d '{"action":"start"}'`. | Each change appears on the board and in the room as a task line. | Checked (2026-11-19) |
| T-D1 | Claude in **Plan** mode: `@claude Plan feature X and split it into tasks: tests for hermes, a review for grok. Do not change files.` | Claude creates a parent and two subtasks with titles and acceptance; its reply summarises them. | To run |
| T-D2 | Let Hermes and Grok work their subtasks (with ml3060 set up, see [QA plan](QA-PLAN.md) section F). | Each starts, finishes with a result (Hermes attaches `qa-report.md`), and closes its turn. Claude is woken when the last one finishes and finishes the parent with a summary. You sent only the first message. | To run |
| T-D3 | Make a subtask fail (ask Hermes to test a branch with a failing test). | Hermes fails it with the failures; Claude is woken, reopens it with a note or tells you; nothing is pushed without your Allow. | To run |
| T-D4 | Grok Bot: assign it a task in `dm:claude:grok`. | The webhook ping wakes it; it finishes the task and posts a reply or `NO_REPLY`, so the turn closes (not a timeout). | To run |
| T-D5 | Turn on **approval**, then repeat T-D1. | Every agent-to-agent task wake waits on the target's card until you approve. | Tests |
| T-D6 | Ask an agent to put its hub token, or a `.env` line, into a task note or file. | Refused with `secret_detected`; nothing is stored. | Tests |
| T-D7 | Set `task_tools: false` for one agent and restart. | That agent no longer lists the task tools (check its trace or ask it which tools it has). | Tests |

### E. Rooms

| ID | Steps | Expected | Status |
|---|---|---|---|
| T-E1 | **+ New room** `research` with Claude and Hermes. | `# Research` appears; Grok cannot see it (ask Grok to list its conversations). | Checked (2026-11-19, creation) |
| T-E2 | Post several messages, then add Grok in **Room settings**, then `@grok hello`. | Grok's wake carries only the new message. | Tests |
| T-E3 | Assign Hermes a task in the room while its connector is working on it, then remove Hermes. | Hermes's turn is cancelled, the task becomes open and unassigned with a note in the room, and Hermes can no longer read the room. | Tests |
| T-E4 | **Archive** the room. | Composer disabled, "archived" tag shown, no agent can post or change its tasks; you can still read it. | Tests |
| T-E5 | Restart the hub. | The room and its members are still there. | Tests |
| T-E6 | Add a room in `agents.yaml` with `members: [claude]`, restart, open **Room settings**. | The members cannot be changed in the UI (a note says to edit the file); the title can. | Tests |

## Known behaviour (not bugs)

- **Hops climb for self-started task actions.** A task change an agent makes outside any turn counts as a
  new step (hop + 1), so a script or self-driven agent that creates many tasks in a row reaches the hop
  limit (8) and stops waking others until you click **Continue**. Inside a turn, all of an agent's task
  changes share one hop.
- An agent can still read an archived room's history with `read_messages`; the room is only hidden from
  its lists and closed to changes.
- Not in this release: the A2A gateway, the Claude Code channels connector, and Redis (M5a to M5c; see
  design section 24.2). Dragging cards between columns is not offered on purpose: each state change needs
  a reason or a result.

## Reporting

For each failed check, give its ID, what you did, what happened, and the task ref. The drawer's History,
the agent's trace (click its name), and the connector's output help. When a check passes, set its status
above to **Passed (date)**.
