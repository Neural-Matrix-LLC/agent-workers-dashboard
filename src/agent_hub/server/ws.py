"""Live feed for the owner's browser: messages, turns, agent status, traces, notices."""

from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .owner_api import COOKIE, valid_session


def build_ws(state) -> APIRouter:
    router = APIRouter()

    @router.websocket("/api/ws")
    async def feed(socket: WebSocket):
        # Browsers send cookies on cross-site WebSocket handshakes, so the Origin is checked too.
        origin = socket.headers.get("origin")
        same_site = origin is None or urlsplit(origin).netloc == socket.headers.get("host")
        if not same_site or not valid_session(state.secret, socket.cookies.get(COOKIE)):
            await socket.close(code=4401)
            return
        await socket.accept()
        queue = state.hub.subscribe()
        try:
            await socket.send_json({"type": "agents", "data": await state.hub.agents_view()})
            while True:
                await socket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            state.hub.unsubscribe(queue)

    return router
