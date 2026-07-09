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
  pip install py-clob-client-v2 python-dotenv
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
        from py_clob_client_v2 import ClobClient, SignatureTypeV2
    except ImportError:
        print("错误：请先安装依赖")
        print("  pip install py-clob-client-v2")
        sys.exit(1)

    # 尽量从现有 .env 预读默认值，省得手动重敲（缺失就走交互输入）
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
    except ImportError:
        pass

    print()
    print("=" * 60)
    print("  Polymarket CLOB 凭证生成工具")
    print("=" * 60)
    print()
    print("需要你的钱包私钥（Polygon/EVM 地址）")
    print("私钥格式：0x 开头的 64 位十六进制字符串")
    print("例：0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef12345678")
    print()

    env_pk = os.getenv("PRIVATE_KEY", "").strip()
    if env_pk:
        prompt = f"请输入私钥（回车沿用 .env 中的 {env_pk[:6]}…{env_pk[-4:]}）: "
    else:
        prompt = "请输入私钥: "
    private_key = input(prompt).strip() or env_pk
    if not private_key.startswith("0x") or len(private_key) != 66:
        print("格式错误：私钥必须是 0x 开头的 66 字符字符串")
        sys.exit(1)

    # ── 代理钱包 / 签名类型 ─────────────────────────────────────────────
    # 关键：派生 API key 时必须与「下单时」用的 signature_type + funder 一致，
    # 否则服务器把 key 绑定到裸 EOA 地址，而订单的 maker 是代理地址，
    # 两者不匹配 → 400（见 order_manager.py 的 ClobClient 构造）。
    print()
    env_funder = os.getenv("FUNDER_ADDRESS", "").strip()
    funder_in = input(
        f"资金/代理地址 FUNDER_ADDRESS（回车沿用 .env: {env_funder or '<空>'}）: "
    ).strip()
    funder = funder_in or env_funder

    env_sig = os.getenv("SIGNATURE_TYPE", "").strip()
    default_sig = env_sig or ("2" if funder else "0")
    sig_in = input(
        f"SIGNATURE_TYPE [0=EOA 1=Magic代理 2=浏览器钱包代理 3=deposit wallet]"
        f"（回车沿用 {default_sig}）: "
    ).strip()
    try:
        signature_type = int(sig_in or default_sig)
    except ValueError:
        print("格式错误：SIGNATURE_TYPE 必须是 0/1/2/3")
        sys.exit(1)

    print()
    print(f"正在连接 Polymarket CLOB 并生成 API 密钥…"
          f"（signature_type={signature_type}, funder={funder or '<无>'}）")

    # ── 初始化客户端（L1 only，无 creds）────────────────────────────
    POLYMARKET_HOST = "https://clob.polymarket.com"
    CHAIN_ID = 137  # Polygon mainnet

    try:
        client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=CHAIN_ID,
            key=private_key,
            signature_type=SignatureTypeV2(signature_type),
            funder=funder or None,
        )

        # create_or_derive_api_key：用私钥签名消息，服务器验证后返回 L2 凭证
        # 同一私钥每次调用返回相同的凭证（确定性派生，不会重复创建）
        creds = client.create_or_derive_api_key()

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

    # ── 写入 .env（就地更新，保留其余所有配置）──────────────────────
    updates = {
        "PRIVATE_KEY": private_key,
        "CLOB_API_KEY": api_key,
        "CLOB_API_SECRET": api_secret,
        "CLOB_API_PASSPHRASE": api_passphrase,
    }

    if not os.path.exists(env_path):
        # 没有 .env 时才创建一个最小骨架
        write = input(f"未找到 {env_path}，新建？(y/N): ").strip().lower()
        if write != "y":
            print("未写入文件，请手动复制上方凭证到 .env")
            _print_next_steps()
            return
        lines = [
            "# 自动生成 by get_credentials.py",
            "# 警告：不要把此文件提交到 git！",
            "",
            f"PRIVATE_KEY={private_key}",
            f"CLOB_API_KEY={api_key}",
            f"CLOB_API_SECRET={api_secret}",
            f"CLOB_API_PASSPHRASE={api_passphrase}",
            "",
            "PAPER_TRADING=true",
            "TRADE_SIZE_USD=1.0",
            "LOG_LEVEL=INFO",
        ]
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"已写入 {env_path}")
        _print_next_steps()
        return

    write = input(f"更新 {env_path} 中的凭证行（其余配置保留）？(y/N): ").strip().lower()
    if write != "y":
        print("未写入文件，请手动复制上方凭证到 .env")
        _print_next_steps()
        return

    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    seen = set()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}\n"
            seen.add(key)

    # 原来缺失的键补到末尾
    missing = [k for k in updates if k not in seen]
    if missing:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        for k in missing:
            lines.append(f"{k}={updates[k]}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"已就地更新 {env_path}（更新 {len(seen)} 行，新增 {len(missing)} 行，其余保留）")
    _print_next_steps()


def _print_next_steps() -> None:
    print()
    print("下一步：")
    print("  python main.py    （PAPER_TRADING=true 先跑模拟）")
    print()


if __name__ == "__main__":
    main()
