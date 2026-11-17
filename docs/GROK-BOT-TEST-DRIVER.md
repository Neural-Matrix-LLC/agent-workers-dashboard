# Grok Bot test driver

A small script that checks Grok Bot can work with Agent Hub before the hub is built. It is the Grok Bot part
of milestone M0 in the [design](AGENT-HUB-DESIGN.md).

Script: [`spikes/grok_bot_driver.py`](../spikes/grok_bot_driver.py). It needs only `uv`; the dependencies are
declared inside the script.

## What it does

Grok Bot cannot be called from outside, so the driver does not dial Grok Bot. It runs a stub of the hub's
`/mcp` endpoint (the six hub tools, in memory) and waits for Grok Bot to come to it.

| Step | What the driver does | Passes when |
|---|---|---|
| 1. Connect | Listens on `/mcp` and checks the token | Grok Bot makes its first authenticated request |
| 2. Assign | Puts a task message in `dm:grok:owner` and POSTs a wake ping to Grok Bot's webhook | The task is stored (a failed ping is a warning; the task stays pending for a poll) |
| 3. Wait | Waits for Grok Bot to call `post_message` or `send_dm` | A reply arrives before the timeout |

The exit code is 0 when all three steps pass and 1 otherwise. Every tool call Grok Bot makes is printed.

## Check the driver itself

No Grok Bot needed. A built-in MCP client plays Grok Bot:

```
uv run spikes/grok_bot_driver.py --simulate
```

## Run it against Grok Bot

1. **Make the stub reachable.** If Grok Bot is a hosted service, start a tunnel to port 8700 (for example
   `cloudflared tunnel --url http://127.0.0.1:8700`) and note the HTTPS address. If Grok Bot can reach your
   network, bind to that interface with `--host` instead.
   With Tailscale (Grok Bot and this machine on the same tailnet) no tunnel is needed. Bind to this machine's
   Tailscale address, shown by `tailscale ip -4`, and skip `--public-url`:
   ```
   uv run spikes/grok_bot_driver.py --host <this machine's Tailscale IP>
   ```
   Grok Bot's own Tailscale address is not used in the command; Grok Bot is the one that connects.
2. **Start the driver.**
   ```
   uv run spikes/grok_bot_driver.py --public-url https://<tunnel-host>/mcp
   ```
   It prints the connector URL and a new random token.
3. **Add the connector in Grok Bot** with that URL and the header `Authorization: Bearer <token>`. The form
   fields and values are listed in the skill, under
   [Creating the Agent-Hub app in Grok Bot](../skills/agent-hub/SKILL.md#creating-the-agent-hub-app-in-grok-bot). If Grok Bot
   cannot send a header, use the printed `?token=` URL. Ask Grok Bot to call `whoami`. Step 1 passes.
4. **Step 2 and 3.** The driver assigns the task. Without a webhook, tell Grok Bot "check Agent Hub for new
   messages and reply there", or let its polling routine find the task.

The driver reads `HUB_GROK_TOKEN` and `GROK_WEBHOOK_URL` from a `.env` file at the repo root (git ignores
it), so the connector stays valid between runs. Variables already set in the shell take precedence.

```
HUB_GROK_TOKEN=<fixed token, for example from: openssl rand -hex 32>
GROK_WEBHOOK_URL=<Grok Bot webhook routine URL>
```

To test the webhook as well, ask Grok Bot to create its webhook routine first, then put its URL in `.env`.
Copy the routine's sender key from its panel into `.env` as `GROK_WEBHOOK_KEY`. The driver sends it as
`Authorization: Bearer <key>`. [`.env.example`](../.env.example) lists all three variables.

When a webhook URL is set, the driver does not wait at step 1. It assigns the task and sends the ping at
once, and the ping has to bring Grok Bot in. Do not type in the Grok Bot chat during this run; a pass then
shows the hub can wake Grok Bot unattended.

For an unattended run, first give Grok Bot the skill in
[`skills/agent-hub/SKILL.md`](../skills/agent-hub/SKILL.md). It tells Grok Bot how to use the hub tools and
that it may post its reply without asking the owner.

The ping body is the one in design section 8.5. Other options: `--task`, `--port`, `--connect-timeout`,
`--reply-timeout` (see `--help`).

## Reading a failure

| Output | Likely cause |
|---|---|
| `FAIL [1/3]` and no `http` lines | Grok Bot cannot reach the URL (tunnel or network). |
| `401 (missing or wrong token)` | The connector is not sending the token. Try the `?token=` URL. |
| `404` lines for `/.well-known/...` | Grok Bot is trying OAuth discovery. The stub supports a fixed token only. |
| `ping try N: ...` | The webhook URL is wrong, or it expects a different body. |
| `FAIL [3/3]` after `read_messages` | Grok Bot read the task but did not post a reply with the hub tools. |

## Security

The tunnel puts the stub on the public internet while it runs. It serves only `/mcp`, holds no real data, and
uses a throwaway token. Stop the driver and the tunnel when the test is done. A token in a URL can end up in
logs, so use the `?token=` form only for this test.
