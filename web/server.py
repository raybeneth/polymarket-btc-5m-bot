"""
Web dashboard server — serves a monitoring UI and pushes real-time data via WebSocket.

Uses aiohttp (already a project dependency) for both HTTP and WebSocket.
Runs alongside the bot's main loop in the same event loop.
"""
import asyncio
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path

from aiohttp import web

log = logging.getLogger(__name__)

_WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
_STATIC_DIR = Path(__file__).parent / "static"
_OPT_DIR = Path(__file__).parent.parent / "docs" / "optimizations"
_MAX_LOG_LINES = 200

# ─────────────────────────────────────────────
# Shared state (written by bot, read by web handlers)
# ─────────────────────────────────────────────

_state: dict = {}
_log_buffer: deque = deque(maxlen=_MAX_LOG_LINES)
_ws_clients: set[web.WebSocketResponse] = set()

# Log lines are persisted to MySQL off the hot path: emit() enqueues here and a
# daemon thread drains it, so a slow/unavailable DB never blocks the event loop
# or the logging caller.
_log_db_queue: "queue.Queue[tuple[float, str]]" = queue.Queue()
_log_writer_started = False


def update_state(data: dict) -> None:
    """Called by main loop every cycle to update the shared state."""
    global _state
    _state = data


def add_log_line(line: str) -> None:
    """Called by WebLogHandler to buffer log lines."""
    _log_buffer.append(line)


def preload_logs(lines: list[str]) -> None:
    """
    Seed the in-memory buffer with historical log lines loaded from MySQL on
    startup. Prepended ahead of any live lines already emitted this run so the
    dashboard shows a continuous history across restarts.
    """
    global _log_buffer
    _log_buffer = deque(list(lines) + list(_log_buffer), maxlen=_MAX_LOG_LINES)


def _log_writer_loop() -> None:
    """Daemon thread: persist queued log lines to MySQL, pruning periodically."""
    from storage import db
    n = 0
    while True:
        ts, line = _log_db_queue.get()
        try:
            db.insert_log(ts, line)
            n += 1
            if n % 200 == 0:
                db.prune_logs(5000)
        except Exception:
            pass  # never let a DB hiccup kill the log writer
        finally:
            _log_db_queue.task_done()


def _start_log_writer() -> None:
    global _log_writer_started
    if _log_writer_started:
        return
    _log_writer_started = True
    threading.Thread(target=_log_writer_loop, name="log_db_writer", daemon=True).start()


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


async def _handle_api_klines(request: web.Request) -> web.Response:
    # Recent 5m klines for the dashboard's BTC candlestick chart. Served on its
    # own endpoint (fetched by the frontend on load + every 60s) rather than in
    # the 2s WS broadcast, since ~180 candles per push would bloat the payload.
    from feeds import btc_price
    return web.json_response(btc_price.get_klines())


async def _handle_api_price(request: web.Request) -> web.Response:
    """Lightweight endpoint returning only the latest BTC spot price.
    The frontend polls this at ~700ms to update the live candlestick between
    the slower 2s WebSocket broadcasts."""
    from feeds import btc_price
    return web.json_response({"p": btc_price.get_latest()})


async def _handle_api_optimizations(request: web.Request) -> web.Response:
    """
    List the optimization records under docs/optimizations for the dashboard's
    slide-out menu. Returns [{name, num, title, markdown}] sorted by number,
    where `num` is the NNN filename prefix and `title` is the first '# ' heading.
    README.md and non-numbered files are skipped.
    """
    items = []
    if _OPT_DIR.is_dir():
        for p in sorted(_OPT_DIR.glob("[0-9]*.md")):
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            num = p.name.split("-", 1)[0]
            title = p.stem
            for line in text.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            items.append({"name": p.name, "num": num, "title": title, "markdown": text})
    return web.json_response(items)


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    _ws_clients.add(ws)
    log.debug(f"WebSocket 客户端已连接 (共{len(_ws_clients)}个)")

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
        log.debug(f"WebSocket 客户端已断开 (共{len(_ws_clients)}个)")

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
            line = f"{ts}  [{level}] {msg}"
            add_log_line(line)
            # Persist off the hot path — the writer thread does the DB insert.
            try:
                _log_db_queue.put_nowait((record.created, line))
            except Exception:
                pass
        except Exception:
            pass


# ─────────────────────────────────────────────
# Server lifecycle
# ─────────────────────────────────────────────

async def start_web_server() -> None:
    """Start aiohttp web server as a background task in the running event loop."""
    _start_log_writer()  # begin persisting dashboard log lines to MySQL
    app = web.Application()
    app.router.add_get("/", _handle_index)
    app.router.add_get("/api/state", _handle_api_state)
    app.router.add_get("/api/klines", _handle_api_klines)
    app.router.add_get("/api/price", _handle_api_price)
    app.router.add_get("/api/optimizations", _handle_api_optimizations)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_static("/static/", _STATIC_DIR)

    # access_log=None disables aiohttp's per-request access log — otherwise it
    # prints every HTTP/WS request including the client's User-Agent (browser/
    # device info), which is noise we don't want in the console/bot.log.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", _WEB_PORT)
    await site.start()
    log.info(f"Web 控制台已运行于 http://localhost:{_WEB_PORT}")
