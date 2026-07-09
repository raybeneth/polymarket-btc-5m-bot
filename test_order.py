"""
CLOB 下单测试脚本（polymarket-client 版）
=========================================

用 execution/order_manager.py 里跟机器人完全一样的客户端构造方式
（AsyncSecureClient，读取 .env 里的 PRIVATE_KEY / FUNDER_ADDRESS）
下一笔「最小金额」的真实订单，验证新 SDK 的下单链路是否真的能被
交易所接受，而不用等真实信号触发。

策略（把风险降到最低）：
  - 挂一个「远低于市价」的 GTC 限价买单，正常情况下不会成交；
  - 只用来验证「下单是否被服务器接受」这条链路；
  - 一旦返回 AcceptedOrder（下单成功），立即撤单 → 零成交、零成本；
  - 若被拒绝/报错，打印完整错误信息，方便判断根因。

只在「容器 / 服务器」上运行（SDK + 网络都在那边）：
    python3 test_order.py
"""
import asyncio
import sys

from execution import order_manager
from feeds.polymarket_clob import (
    create_session, _fetch_active_markets, get_active_markets, fetch_order_book,
)

# ── 测试参数（尽量小）──────────────────────────────────────────────
TEST_USD = 1.0        # 下单金额（美元）。Polymarket 单笔最小约 $1，别再往下调。
TEST_PRICE = 0.10     # 目标限价：远低于 BTC 5m 约 0.5 的市价 → 挂着不会成交。


async def find_market_and_book():
    """复用 bot 的市场发现，挑一个「剩余时间最多」的活跃市场，返回 (MarketInfo, OrderBook)。"""
    async with create_session() as session:
        await _fetch_active_markets(session)
        markets = [m for m in get_active_markets().values() if m.remaining_seconds > 60]
        rem = ", ".join(f"{int(m.remaining_seconds)}s" for m in markets)
        print(f"  找到活跃市场: {len(markets)} 个" + (f"（剩余 {rem}）" if markets else ""))
        if not markets:
            return None, None
        markets.sort(key=lambda m: m.remaining_seconds, reverse=True)
        m = markets[0]
        book = await fetch_order_book(session, m.yes_token_id)
        return m, book


async def main() -> None:
    print("=" * 64)
    print("  CLOB 下单测试（polymarket-client / AsyncSecureClient）")
    print("=" * 64)
    print()

    print("正在查找当前 BTC 5 分钟市场…")
    market, book = await find_market_and_book()
    if market is None:
        print("❌ 没找到剩余时间 > 60s 的活跃 BTC 5 分钟市场，稍后再试。")
        sys.exit(1)

    token_id = market.yes_token_id
    tick = str(book.tick_size) if (book and book.tick_size) else "0.01"
    tick_f = float(tick)
    best_ask = book.best_ask if book else None
    best_bid = book.best_bid if book else None
    print(f"  市场: {market.question}")
    print(f"  token_id(YES/Up): {token_id}")
    print(f"  盘口: bid={best_bid} ask={best_ask} tick={tick} 剩余={int(market.remaining_seconds)}s")
    print()

    # 定价：确保远低于卖一价（不会成交），且落在 tick 网格上
    price = TEST_PRICE
    if best_ask is not None:
        max_safe = best_ask - 3 * tick_f      # 至少低卖一价 3 个 tick，绝不吃单
        if price > max_safe:
            price = max(tick_f, max_safe)
    price = round(round(price / tick_f) * tick_f, 4)

    # 数量：满足金额 + 市场最小下单量
    size = round(TEST_USD / price, 2)
    if book and book.min_order_size and size < book.min_order_size:
        size = book.min_order_size

    print(f"将提交订单: 限价={price} 数量={size} 金额≈${price * size:.2f} (tick={tick})")
    print("（远低于卖一价，正常不会成交；一旦被服务器接受会立刻撤单）")
    print()

    # 复用 bot 自己的客户端构造路径（execution/order_manager.py._get_clob_client）
    client = await order_manager._get_clob_client()
    print(f"客户端: wallet_type={client.wallet_type} signer={client.signer} wallet={client.wallet}")
    print()

    try:
        response = await client.place_limit_order(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )
    except Exception as exc:
        print("❌ 下单请求异常，完整错误：")
        print(f"   {type(exc).__name__}: {exc}")
        sys.exit(2)

    if not response.ok:
        print("❌ 订单被服务器拒绝：")
        print(f"   code={response.code}")
        print(f"   message={response.message}")
        sys.exit(2)

    print("✅ 订单被服务器接受！")
    print(f"   order_id={response.order_id}")
    print(f"   status={response.status}")
    print()

    print("正在撤单（清理测试订单）…")
    cancel_resp = await client.cancel_order(order_id=response.order_id)
    print(f"   canceled={cancel_resp.canceled}")
    print(f"   not_canceled={cancel_resp.not_canceled}")
    print()
    print("测试完成：下单链路正常工作。")


if __name__ == "__main__":
    asyncio.run(main())
