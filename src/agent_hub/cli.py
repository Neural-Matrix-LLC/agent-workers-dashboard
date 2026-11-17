"""agent-hub serve | connect <agent> | token <agent>"""

import argparse
import asyncio
import logging
import os
import secrets
import socket
import sys

from .config import find_config, load_config
from .ids import hash_token


def serve(args) -> None:
    import uvicorn

    from .server.app import create_app

    config = load_config(args.config)
    print(f"Config: {find_config(args.config).resolve()}", flush=True)
    if not os.environ.get("HUB_OWNER_PASSWORD"):
        sys.exit("Set HUB_OWNER_PASSWORD (in .env next to agents.yaml) before starting the hub.")
    sockets = []
    for host, port in config.hub.binds:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            sockets.append(socket.create_server((host, port), family=family))
        except OSError as exc:
            sys.exit(f"Cannot listen on {host}:{port}: {exc.strerror}. Check hub.bind in agents.yaml "
                     "(the address must belong to this machine, and the port must be free).")
        print(f"Agent Hub listening on http://{host}:{port}", flush=True)
    uvicorn.Server(uvicorn.Config(create_app(config), log_level="info")).run(sockets=sockets)


def connect(args) -> None:
    from .connectors import CONNECTORS

    config = load_config(args.config)
    agent = config.agent(args.agent)
    if agent.connector not in CONNECTORS:
        sys.exit(f"'{agent.id}' has connector '{agent.connector}', which runs no connector process. "
                 f"Known connectors: {', '.join(CONNECTORS)}.")
    token = os.environ.get(agent.token_env)
    if not token:
        sys.exit(f"Set {agent.token_env} to the agent's token. Create one with: agent-hub token {agent.id}")
    connector = CONNECTORS[agent.connector](agent, args.hub or config.hub.base_url, token)
    try:
        asyncio.run(connector.serve())
    except KeyboardInterrupt:
        pass


def token(args) -> None:
    from .core.repo import Repo

    config = load_config(args.config)
    agent = config.agent(args.agent)
    value = secrets.token_hex(32)

    async def store() -> None:
        repo = await Repo.open(config.hub.db)
        await repo.upsert_participant(agent.id, agent.display_name, "agent", agent.connector, agent.delivery,
                                      {"description": agent.description})
        await repo.set_token_hash(agent.id, hash_token(value))
        await repo.close()

    asyncio.run(store())
    print(f"New token for {agent.id} (shown once; the hub stores only its hash):\n\n  {value}\n")
    print(f"The old token for {agent.id} no longer works.")
    if agent.connector == "none":
        print("Put it in the agent's hub connector as the header:  Authorization: Bearer <token>")
    else:
        print(f"On the machine that runs the connector, set {agent.token_env}=<token> (for example in .env).")
    if os.environ.get(agent.token_env):
        print(f"\nNote: {agent.token_env} is set here. The hub reads that variable at start-up and it "
              f"takes precedence, so update or remove it.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-hub", description="Agent Hub")
    parser.add_argument("--config", help="path to agents.yaml (default: $AGENT_HUB_CONFIG, ./agents.yaml, "
                                         "or agents.yaml at the repo root)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the hub").set_defaults(run=serve)
    connect_parser = commands.add_parser("connect", help="run the connector for one agent")
    connect_parser.add_argument("agent")
    connect_parser.add_argument("--hub", help="hub address (default: hub.url or hub.bind from the config)")
    connect_parser.set_defaults(run=connect)
    token_parser = commands.add_parser("token", help="create or rotate an agent's token")
    token_parser.add_argument("agent")
    token_parser.set_defaults(run=token)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        args.run(args)
    except (FileNotFoundError, KeyError) as error:
        sys.exit(str(error.args[0]) if error.args else str(error))


if __name__ == "__main__":
    main()
