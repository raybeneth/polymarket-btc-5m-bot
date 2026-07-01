"""
Web dashboard server — serves a monitoring UI and pushes real-time data via WebSocket.

Uses aiohttp (already a project dependency) for both HTTP and WebSocket.
Runs alongside the bot's main loop in the same event loop.
"""
import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

from aiohttp import web

log = logging.getLogger(__name__)

_WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
_STATIC_DIR = Path(__file__).parent / "static"
_MAX_LOG_LINES = 200

# ─────────────────────────────────────────────
# Shared state (written by bot, read by web handlers)
# ─────────────────────────────────────────────

_state: dict = {}
_log_buffer: deque = deque(maxlen=_MAX_LOG_LINES)
_ws_clients: set[web.WebSocketResponse] = set()


def update_state(data: dict) -> None:
    """Called by main loop every cycle to update the shared state."""
    global _state
    _state = data


def add_log_line(line: str) -> None:
    """Called by WebLogHandler to buffer log lines."""
    _log_buffer.append(line)


def _get_snapshot() -> dict:
    """Build a full JSON-serialisable snapshot for clients."""
    snapshot = dict(_state)
    snapshot["logs"] = list(_log_buffer)
    return snapshot


# ─────────────────────────────────────────────
# HTTP / WebSocket handlers
# ─────────────────────────────────────────────

async def _handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(_STATIC_DIR / "index.html")


async def _handle_api_state(request: web.Request) -> web.Response:
    return web.json_response(_get_snapshot())


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    _ws_clients.add(ws)
    log.info(f"WebSocket 客户端已连接 (共{len(_ws_clients)}个)")

    # Send initial snapshot
    try:
        await ws.send_json(_get_snapshot())
    except Exception:
        pass

    # Keep connection alive; the bot loop pushes data via broadcast()
    try:
        async for msg in ws:
            pass  # we don't expect messages from the client
    finally:
        _ws_clients.discard(ws)
        log.info(f"WebSocket 客户端已断开 (共{len(_ws_clients)}个)")

    return ws


async def broadcast() -> None:
    """Push current state to all connected WebSocket clients."""
    if not _ws_clients:
        return
    data = _get_snapshot()
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ─────────────────────────────────────────────
# Log handler for web
# ─────────────────────────────────────────────

class WebLogHandler(logging.Handler):
    """Routes log records to the web log buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = time.strftime("%H:%M:%S", time.localtime(record.created))
            msg = record.getMessage().strip()
            if not msg:
                return
            level = record.levelname[0]  # I/W/E
            add_log_line(f"{ts}  [{level}] {msg}")
        except Exception:
            pass


# ─────────────────────────────────────────────
# Server lifecycle
# ─────────────────────────────────────────────

async def start_web_server() -> None:
    """Start aiohttp web server as a background task in the running event loop."""
    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/api/state", _handle_api_state)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_static("/static/", _STATIC_DIR)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", _WEB_PORT)
    await site.start()
    log.info(f"Web 控制台已运行于 http://localhost:{_WEB_PORT}")
