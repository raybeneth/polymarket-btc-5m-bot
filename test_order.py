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

只在「容器内」运行（SDK 只装在容器里）：
    docker exec -it polymarket-bot python /app/test_order.py
"""
import json
import sys
import time
import urllib.parse
import urllib.request

from config import (
    PRIVATE_KEY, FUNDER_ADDRESS, SIGNATURE_TYPE,
    API_KEY, API_SECRET, API_PASSPHRASE,
    POLYMARKET_HOST, GAMMA_HOST, CHAIN_ID,
)

# ── 测试参数（尽量小）──────────────────────────────────────────────
TEST_USD = 1.0      # 下单金额（美元）。Polymarket 单笔最小约 $1，别再往下调。
TEST_PRICE = 0.10   # 限价：远低于 BTC 5m 市场约 0.5 的中间价 → 不会成交，只测下单。


def fetch_btc_5m_token():
    """从 Gamma API 找一个当前活跃的 BTC 5 分钟 Up/Down 市场。

    返回 (yes_token_id, question)；找不到返回 (None, None)。
    """
    now = int(time.time())
    base = (now // 300) * 300
    for off in (0, -1, 1, 2):
        window_ts = base + off * 300
        slug = f"btc-updown-5m-{window_ts}"
        url = f"{GAMMA_HOST}/markets?" + urllib.parse.urlencode({"slug": slug})
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read().decode())
        except Exception:
            continue
        markets = data if isinstance(data, list) else data.get("markets", [])
        if not markets:
            continue
        m = markets[0]
        if m.get("closed"):
            continue
        end_ts = window_ts + 300
        if end_ts - now <= 30:   # 剩余时间太短，跳过（避免刚下单就结算）
            continue
        try:
            token_ids = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            continue
        if len(token_ids) >= 2:
            return token_ids[0], m.get("question", slug)  # index 0 = Up ≡ YES
    return None, None


def main() -> None:
    try:
        from py_clob_client_v2 import (
            ClobClient, ApiCreds, SignatureTypeV2,
            OrderArgs, OrderType, Side, OrderPayload, PartialCreateOrderOptions,
        )
    except ImportError:
        print("错误：请在装有 py-clob-client-v2 的环境（容器内）运行。")
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

    # 1) 找一个活跃市场
    print("正在查找当前 BTC 5 分钟市场…")
    token_id, question = fetch_btc_5m_token()
    if not token_id:
        print("❌ 没找到活跃的 BTC 5 分钟市场（可能正好在窗口切换），稍后再试。")
        sys.exit(1)
    print(f"  市场: {question}")
    print(f"  token_id(YES/Up): {token_id}")
    print()

    # 2) 构建客户端（与 execution/order_manager.py 完全一致）
    creds = ApiCreds(
        api_key=API_KEY,
        api_secret=API_SECRET,
        api_passphrase=API_PASSPHRASE,
    )
    client = ClobClient(
        host=POLYMARKET_HOST,
        chain_id=CHAIN_ID,
        key=PRIVATE_KEY,
        creds=creds,
        signature_type=SignatureTypeV2(SIGNATURE_TYPE),
        funder=FUNDER_ADDRESS or None,
    )

    # 3) 计算下单参数
    try:
        tick = str(client.get_tick_size(token_id))
    except Exception:
        tick = "0.01"   # BTC updown 市场固定 0.01
    price = TEST_PRICE
    size = round(TEST_USD / price, 2)

    print(f"将挂 GTC 买单: 限价={price} 数量={size} 金额≈${price * size:.2f} (tick={tick})")
    print("（价格远低于市价 → 不会成交，只验证下单是否被接受；成功后立即撤单）")
    print()
    if input("确认真实下单？(yes/N): ").strip().lower() != "yes":
        print("已取消。")
        return

    # 4) 下单（这一步就是之前报 maker 错误的地方）
    print("\n提交订单中…")
    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=Side.BUY,
        )
        resp = client.create_and_post_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size=tick),
            order_type=OrderType.GTC,
        )
    except Exception as exc:
        print()
        print("❌ 下单失败，完整错误：")
        print(f"   {type(exc).__name__}: {exc}")
        print()
        print("→ 若仍是 'maker address not allowed'，说明 maker 问题未解决。")
        sys.exit(2)

    print()
    print("✅ 下单被服务器接受！返回：")
    print(json.dumps(resp, indent=2, ensure_ascii=False, default=str))
    order_id = resp.get("orderID") or resp.get("id")

    # 5) 立即撤单，确保零成交零成本
    if order_id:
        try:
            client.cancel_order(OrderPayload(orderID=order_id))
            print(f"\n已撤单 {order_id} —— 零成交、零成本。")
        except Exception as exc:
            print(f"\n⚠️ 自动撤单失败，请手动到网页端撤掉订单 {order_id}: {exc}")
    else:
        print("\n⚠️ 返回里没有 orderID，请到网页端确认是否有挂单需要手动撤销。")

    print()
    print("结论：maker address 问题已解决，下单链路可用 ✅")


if __name__ == "__main__":
    main()
