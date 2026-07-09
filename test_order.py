"""
CLOB 下单测试脚本
=================

用 .env 里的真实 CLOB 配置（PRIVATE_KEY / FUNDER_ADDRESS / SIGNATURE_TYPE /
API creds）下一笔「最小金额」的真实订单，用来验证
    'maker address not allowed, please use the deposit wallet flow'
这个报错是否已经解决。

策略（把风险降到最低）：
  - 挂一个「远低于市价」的 GTC 限价买单，正常情况下不会成交；
  - 只用来验证「下单是否被服务器接受」这条链路；
  - 一旦返回 orderID（下单成功），立即撤单 → 零成交、零成本；
  - 若报错，打印完整错误信息，方便判断根因。

市场发现 / 订单簿获取直接复用 bot 自己的 feeds/polymarket_clob.py（aiohttp 实现，
已验证可用），避免自己用 urllib 复刻时被 Cloudflare 拦。

只在「容器 / 服务器」上运行（SDK + 网络都在那边）：
    python3 test_order.py
"""
import asyncio
import sys

import aiohttp

from config import (
    PRIVATE_KEY, FUNDER_ADDRESS, SIGNATURE_TYPE,
    API_KEY, API_SECRET, API_PASSPHRASE,
    POLYMARKET_HOST, CHAIN_ID,
)
from feeds.polymarket_clob import (
    _fetch_active_markets, get_active_markets, fetch_order_book,
)

# ── 测试参数（尽量小）──────────────────────────────────────────────
TEST_USD = 1.0        # 下单金额（美元）。Polymarket 单笔最小约 $1，别再往下调。
TEST_PRICE = 0.10     # 目标限价：远低于 BTC 5m 约 0.5 的市价 → 挂着不会成交。


async def find_market_and_book():
    """复用 bot 的市场发现，挑一个「剩余时间最多」的活跃市场，返回 (MarketInfo, OrderBook)。"""
    async with aiohttp.ClientSession() as session:
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


def main() -> None:
    try:
        from py_clob_client_v2 import (
            ClobClient, ApiCreds, SignatureTypeV2,
            OrderArgs, Side, PartialCreateOrderOptions,
        )
    except ImportError:
        print("错误：请在装有 py-clob-client-v2 的环境（容器/服务器）运行。")
        sys.exit(1)

    print("=" * 64)
    print("  CLOB 下单测试")
    print("=" * 64)
    print(f"  signature_type = {SIGNATURE_TYPE}")
    print(f"  funder         = {FUNDER_ADDRESS or '<无>'}")
    print(f"  api_key        = {(API_KEY[:8] + '…') if API_KEY else '<空>'}")
    print(f"  host           = {POLYMARKET_HOST}")
    print()

    if not PRIVATE_KEY or not API_KEY:
        print("❌ .env 缺少 PRIVATE_KEY 或 CLOB_API_KEY，无法测试。")
        sys.exit(1)

    # 1) 找一个活跃市场 + 订单簿（复用 bot 的 aiohttp 路径）
    print("正在查找当前 BTC 5 分钟市场…")
    market, book = asyncio.run(find_market_and_book())
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

    # 2) 定价：确保远低于卖一价（不会成交），且落在 tick 网格上
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

    # 3) 构建客户端（与 execution/order_manager.py 完全一致）
    creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
    client = ClobClient(
        host=POLYMARKET_HOST,
        chain_id=CHAIN_ID,
        key=PRIVATE_KEY,
        creds=creds,
        signature_type=SignatureTypeV2(SIGNATURE_TYPE),
        funder=FUNDER_ADDRESS or None,
    )

    print(f"将构建订单: 限价={price} 数量={size} 金额≈${price * size:.2f} (tick={tick})")
    print("（仅本地构建并签名订单，不提交、不下单、不产生真实挂单）")
    print()

    # 4) 只构建 + 签名订单（本地操作，不 POST 到服务器）
    #    打印出来检查 maker / signer / signatureType / signature 是否符合预期。
    try:
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY)
        order = client.create_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size=tick),
        )
    except Exception as exc:
        print("❌ 构建订单失败，完整错误：")
        print(f"   {type(exc).__name__}: {exc}")
        sys.exit(2)

    print("构建的订单对象：")
    print(order)

    # 额外：尽量把字段结构化打印出来，方便核对 maker/signer/signatureType
    try:
        d = order.__dict__ if hasattr(order, "__dict__") else dict(order)
        print()
        print("字段明细：")
        for k, v in d.items():
            print(f"  {k} = {v}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
