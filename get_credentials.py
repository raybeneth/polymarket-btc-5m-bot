"""
Polymarket CLOB Credentials Helper
====================================

运行一次，自动生成 .env 所需的全部凭证。
不会花钱，不会下单，只是签一条消息。

─────────────────────────────────────────
凭证来源说明
─────────────────────────────────────────
Polymarket CLOB 使用两层认证：

  L1 — 链上签名（你的钱包私钥）
       用于给每一笔订单签名，保证不可伪造。
       来源：MetaMask → Account Details → Export Private Key
             或 Coinbase Wallet / 任何 EVM 钱包

  L2 — REST API 密钥（API Key / Secret / Passphrase）
       用于给 HTTP 请求签名，Polymarket 服务器校验身份。
       来源：用你的私钥调用 CLOB 的 /auth/api-key 接口，
             服务器用你的签名验证身份后返回这三个值。
             本脚本自动完成这个过程。

─────────────────────────────────────────
使用方式
─────────────────────────────────────────
  pip install py-clob-client python-dotenv
  python get_credentials.py

  输入你的钱包私钥（不会联网发送，仅本地签名），
  脚本会打印并写入 .env 文件。

警告：私钥是你资产的唯一控制权。
      - 只在你信任的机器上运行
      - 不要把 .env 提交到 git（已在 .gitignore 中排除）
"""

import os
import sys


def main() -> None:
    # ── 检查依赖 ──────────────────────────────────────────────────────
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("错误：请先安装依赖")
        print("  pip install py-clob-client")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  Polymarket CLOB 凭证生成工具")
    print("=" * 60)
    print()
    print("需要你的钱包私钥（Polygon/EVM 地址）")
    print("私钥格式：0x 开头的 64 位十六进制字符串")
    print("例：0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef12345678")
    print()

    private_key = input("请输入私钥: ").strip()
    if not private_key.startswith("0x") or len(private_key) != 66:
        print("格式错误：私钥必须是 0x 开头的 66 字符字符串")
        sys.exit(1)

    print()
    print("正在连接 Polymarket CLOB 并生成 API 密钥…")

    # ── 初始化客户端（L1 only，无 creds）────────────────────────────
    POLYMARKET_HOST = "https://clob.polymarket.com"
    CHAIN_ID = 137  # Polygon mainnet

    try:
        client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=CHAIN_ID,
            key=private_key,
        )

        # derive_api_key：用私钥签名消息，服务器验证后返回 L2 凭证
        # 同一私钥每次调用返回相同的凭证（确定性派生，不会重复创建）
        creds = client.derive_api_key()

    except Exception as exc:
        print(f"\n错误：{exc}")
        print()
        print("常见原因：")
        print("  1. 私钥错误或格式不对")
        print("  2. 这个钱包地址从未在 Polymarket 上交易过")
        print("     → 先去 polymarket.com 用这个钱包登录一次（无需充值）")
        print("  3. 网络问题（需要能访问 clob.polymarket.com）")
        sys.exit(1)

    api_key        = creds.api_key
    api_secret     = creds.api_secret
    api_passphrase = creds.api_passphrase

    # ── 打印结果 ──────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  生成成功！以下是你的凭证：")
    print("=" * 60)
    print(f"  PRIVATE_KEY        = {private_key}")
    print(f"  CLOB_API_KEY       = {api_key}")
    print(f"  CLOB_API_SECRET    = {api_secret}")
    print(f"  CLOB_API_PASSPHRASE= {api_passphrase}")
    print("=" * 60)
    print()

    # ── 写入 .env ─────────────────────────────────────────────────────
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    write = "y"

    if os.path.exists(env_path):
        write = input(f".env 文件已存在，覆盖？(y/N): ").strip().lower()

    if write == "y":
        content = f"""# 自动生成 by get_credentials.py
# 警告：不要把此文件提交到 git！

PRIVATE_KEY={private_key}
CLOB_API_KEY={api_key}
CLOB_API_SECRET={api_secret}
CLOB_API_PASSPHRASE={api_passphrase}

# 交易模式（true=模拟，false=真实下单）
PAPER_TRADING=true

# 每笔交易金额（美元）
TRADE_SIZE_USD=1.0

# 日志级别
LOG_LEVEL=INFO
"""
        with open(env_path, "w") as f:
            f.write(content)
        print(f"已写入 {env_path}")
    else:
        print("未写入文件，请手动复制上方凭证到 .env")

    print()
    print("下一步：")
    print("  python main.py    （PAPER_TRADING=true 先跑模拟）")
    print()


if __name__ == "__main__":
    main()
