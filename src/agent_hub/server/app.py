"""One process: owner API, agent API, the MCP endpoint, and the built UI (design section 4)."""

import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Config
from ..core.hub import Hub
from ..core.models import HubError
from ..core.repo import Repo
from .agent_api import bearer_token, build_agent_api
from .mcp_server import build_mcp
from .owner_api import build_owner_api
from .ws import build_ws

NO_UI = ("<h1>Agent Hub</h1><p>The UI is not built. "
         "Run <code>npm install && npm run build</code> in <code>web/</code>.</p>")


def web_dist() -> Path:
    return Path(os.environ.get("AGENT_HUB_WEB_DIST") or Path(__file__).resolve().parents[3] / "web" / "dist")


class Gateway:
    """Sends /mcp to the MCP app after checking the agent token; everything else goes to FastAPI."""

    def __init__(self, api: FastAPI, mcp_app, state):
        self.api, self.mcp_app, self.state = api, mcp_app, state

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/mcp" or path.startswith("/mcp/")):
            return await self.api(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        agent = await self.state.hub.authenticate(bearer_token(headers))
        if agent is None:
            body = json.dumps({"error": "unauthorized", "message": "missing or wrong agent token"}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
            return await send({"type": "http.response.body", "body": body})
        scope["hub_agent"] = agent
        return await self.mcp_app(scope, receive, send)


def create_app(config: Config, *, http: httpx.AsyncClient | None = None) -> Gateway:
    state = SimpleNamespace(hub=None, secret=None, config=config)
    mcp = build_mcp(state)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repo = await Repo.open(config.hub.db)
        try:
            state.secret = await repo.get_meta("secret")
            if state.secret is None:
                state.secret = secrets.token_hex(32)
                await repo.set_meta("secret", state.secret)
            state.hub = Hub(config, repo, http=http)
            await state.hub.setup()
            async with mcp.session_manager.run():
                yield
        finally:
            if state.hub:
                await state.hub.close()
            await repo.close()

    api = FastAPI(title="Agent Hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @api.exception_handler(HubError)
    async def hub_error(request, error: HubError):
        return JSONResponse({"error": error.code, "message": error.message}, status_code=error.status)

    public, owner = build_owner_api(state)
    api.include_router(public)
    api.include_router(owner)
    api.include_router(build_agent_api(state))
    api.include_router(build_ws(state))

    dist = web_dist()
    if (dist / "index.html").is_file():
        api.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    else:
        api.add_api_route("/", lambda: HTMLResponse(NO_UI), include_in_schema=False)

    return Gateway(api, mcp_app, state)
