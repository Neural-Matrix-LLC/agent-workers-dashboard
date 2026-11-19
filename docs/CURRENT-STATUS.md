# Current status (2026-11-19)

Shared tasks (M4) and multiple rooms (M5d) are built and committed on branch `claude/m4-tasks` (commit
379f404, one commit on top of `main`). The branch is not pushed yet.

- 204 automated tests pass (88 before this work), `ruff check` is clean, and the UI builds.
- M5a, M5b, and M5c are not built; the reasons are [below](#not-built-m5a-m5b-m5c).
- What QA has to run before merging: [QA plan: tasks and rooms](QA-PLAN-M4.md).

## What is built

### Shared tasks (M4)

- Agents and the owner can create tasks, split them into subtasks, claim them, report results, and attach
  files.
- A task lives in one conversation. Only that conversation's members can see it.
- Every change shows in the conversation as a one-line entry naming who made it.
- The hub wakes the right agent:
  - the assignee, when a task is assigned to it;
  - the creator, when the task finishes, fails, or is blocked;
  - the coordinator, when all subtasks are done.
- Task wakes are normal turns, so the hop limit, approval mode, budgets, and rate limit all apply.

### Task files

- Uploads are checked: size limits apply, the file type is detected from the content, and anything that
  looks like a secret or is named like `.env` is refused.
- Downloads can never run as a web page.
- Claude Code cannot read the file store directly.

### Upgrade

The database is migrated automatically on first start, after a backup copy is made
(`agent-hub.db.bak-v1-<time>`).

### UI

- A task board with filters, kept in the page address.
- A task drawer with the owner's actions and file previews.
- Task entries in conversations.
- An open-task count on each agent card.

### Rooms (M5d)

- The owner can create rooms with their own members, change the members, and archive rooms.
- Removing an agent from a room cancels its turns there and unassigns its tasks there.

### Agent skills

- Updated for Grok, Claude, and Hermes.
- Hermes now reports QA results on the task and attaches a `qa-report.md` file.

## How it was checked

- The automated tests cover the design's whole test plan (sections 15 and 21.6). This includes the
  end-to-end case: the owner sends one message, the agents split the work, report back, and finish without
  the owner passing anything along.
- The real hub was run with an echo agent, and the UI was driven in headless Chrome: board, filters,
  drawer, file preview, notes, creating a room, and phone width.
- **Not tried yet:**
  - real Claude Code, Hermes, or Grok Bot using the task tools;
  - a real (non-headless) browser;
  - migrating the real database from ml3090.

## Bug found and fixed

A file named `.env` got past the forbidden-name check, because the leading dot was stripped from the name
before the check ran. The check now also runs on the raw name, and a test covers it.

## Behaviour to know about

Each task change an agent makes outside a turn counts as one more agent-to-agent step (hop + 1). An agent
that creates many tasks in a row outside a turn therefore stops waking others after 8, until the owner
clicks **Continue**. The design specifies this, and the QA plan lists it as known behaviour. Inside a turn,
all of an agent's task changes share one hop.

## Decisions to confirm

The design's recommended answer was used for every open decision. These three need the owner's
confirmation; each can be changed later without touching the database:

- **OD-3:** in DMs between two agents, the owner can only reassign, unassign, or cancel tasks.
- **OD-4:** when some subtasks are in a DM that others in the room cannot see, finishing them posts nothing
  in the room.
- **OD-13:** the Grok skill ([skills/agent-hub/SKILL.md](../skills/agent-hub/SKILL.md)) is rewritten to
  describe the task tools. It needs a review.

## Not built (M5a, M5b, M5c)

- **A2A gateway (M5a):**
  - large;
  - needs public HTTPS;
  - needs three owner decisions (OD-14 to OD-16);
  - useful only once an outside agent exists.
- **Channels connector (M5b):** depends on a Claude Code preview feature and an untested path, so it needs a
  trial on ml3090 first.
- **Redis (M5c):** the design itself recommends skipping it.

The details, and every place where the code differs from the design, are in section 24 of the
[M4 and M5 design](M4-M5-DESIGN.md).

## Next steps

1. Push `claude/m4-tasks` and open a pull request.
2. Run the checks in the [QA plan: tasks and rooms](QA-PLAN-M4.md). Start by backing up `agent-hub.db*`
   and `artifacts/` before the upgrade.
3. Confirm OD-3, OD-4, and OD-13.
