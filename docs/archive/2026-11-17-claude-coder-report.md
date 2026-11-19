# Report: Claude coding agent and Hermes QA skill (2026-11-17)

Archived status report from the session that added the Claude Code modes, Allow/Deny, and the Hermes QA skill. It is kept for the record; the current state is in [design section 17](../AGENT-HUB-DESIGN.md#17-implementation-status-v03) and the open checks are in the [QA plan](../QA-PLAN.md).

Claude on ml3090 is now set up as the coding agent, with modes and Allow/Deny on its card, and Hermes has a QA skill and setup guide for ml3060. The new tests pass (50 in total) and a live run with real Claude Code worked. Before you use it you need to install socat, restart the hub, and do the ml3060 steps yourself.

Claude does not need to be installed "as an MCP server", and neither does Hermes. The hub is the MCP server, and both agents connect to it as clients. A small connector process starts the agent whenever it has a task. The new guide in docs/AGENT-SETUP.md explains this with a diagram of both machines.

Claude Code on ml3090:

Modes: a Mode selector on Claude's card offers Plan (no edits), Manual (asks for everything), Edit automatically, and Auto (a safety check decides). It applies from Claude's next turn. Agents can't change it, and it survives restarts.
Allow/Deny: when Claude needs permission, the card shows the command or file with Allow and Deny, and Claude waits. Deny, no answer within 10 minutes, or Stop refuses the action. Claude then doesn't retry and says in its reply what it needed.
Projects only: Claude works only under /home/thomas/projects.
Git:
Every git push asks first.
Pushes to main/master (including HEAD:main) and force pushes are refused.
Claude commits and pushes only to a claude/<topic> branch, and only when you tell it to.
Secrets: .env files and the hub's own config, database and tokens can't be read.
Sandbox: shell commands run in the Claude Code sandbox.
Skill: it covers the modes, the git rules, the bug-fix steps, handing work to Hermes, and the reply format. The connector loads it on every hub turn, so nothing goes into ~/.claude and your own Claude sessions are unaffected.
Hermes on ml3060:

QA skill: in skills/hermes-qa/SKILL.md. Hermes fetches the branch Claude pushed, runs the tests in a separate checkout, and reports PASS or FAIL. It never edits code or pushes.
Setup guide: install or reinstall Hermes (don't delete ~/.hermes), turn on its API server, connect it to the hub with hermes mcp test hub as the check, link the skill, give ml3060 GitHub read access, run the connector on ml3060, and keep it all running with systemd.
Live test with real Claude Code (cheap model, about $0.20 in total):

Plan mode gave a plan and changed nothing.
Manual mode with Deny: Claude stopped and changed nothing. With Allow: the branch and file were created.
Reading .env was refused, both by the skill and by the deny rule on its own.
In Auto mode the commit and push waited for Allow, and the branch reached the remote.
Pushing to main, to HEAD:main, and a force push were all refused.
The live test found two problems, both now fixed:

Allow/Deny answers were rejected: Claude Code couldn't read our answer format at first.
Hidden push: when Claude chained git add && git commit && git push with a long commit message, the push fell off the end of the request text. The request now starts with [includes: git push, git commit] and always shows the end of the command.
Not tested:

Sandbox: it doesn't run on ml3090 yet because socat isn't installed. Claude Code accepted the settings, but whether git push over SSH works inside the sandbox is still unchecked.
ml3060 steps: I have no access to ml3060, so none of the Hermes steps have been run.
What you need to do:

On ml3090:

sudo apt install socat
git config --global url."git@github.com:".insteadOf "https://github.com/"
sudo ufw allow in on tailscale0 to any port 8700 proto tcp
Then restart the hub and run uv run agent-hub connect claude. The Claude connector won't start until socat is installed.
On ml3060, follow "Hermes on ml3060: the QA agent" in docs/AGENT-SETUP.md.
I updated your agents.yaml: Claude now uses workdir: /home/thomas/projects and starts in permission_mode: plan. I removed Edit from its allowed tools, because anything listed there runs without asking even in Manual mode. The README, the UI guide (with a new plan → fix → push → test workflow) and the design doc are updated.

This work was committed afterwards as 526b1c6 on `feature/agent-hub`.