"""
Standalone web dashboard — no trading, just monitoring.

Reads historical data from positions.log and serves the dashboard.
Useful for reviewing past performance without running the bot.

Usage: python web_only.py
"""
import asyncio
import logging
import time

from utils.logger import setup_logging, attach_web_logger, load_position_history
from web import server as web_server

log = logging.getLogger(__name__)


async def run() -> None:
    history = load_position_history()
    log.info(
        f"History loaded: {history['total_trades']} trades, "
        f"W={history['wins']} L={history['losses']} "
        f"PnL={history['total_pnl']:+.2f}"
    )

    await web_server.start_web_server()

    # Keep pushing state so the dashboard stays alive
    start = time.time()
    while True:
        web_server.update_state({
            "btc_price": None,
            "btc_connected": False,
            "markets_count": 0,
            "today": {
                "trades": history.get("today_trades", 0),
                "wins": history.get("today_wins", 0),
                "losses": history.get("today_losses", 0),
                "pnl": round(history.get("today_pnl", 0.0), 4),
            },
            "all_time": {
                "trades": history.get("total_trades", 0),
                "wins": history.get("wins", 0),
                "losses": history.get("losses", 0),
                "pnl": round(history.get("total_pnl", 0.0), 4),
            },
            "positions": [],
            "uptime": int(time.time() - start),
        })
        await web_server.broadcast()
        await asyncio.sleep(2.0)


def main() -> None:
    setup_logging()
    attach_web_logger()
    log.info("Web-only mode — no trading, dashboard only")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutdown")


if __name__ == "__main__":
    main()
