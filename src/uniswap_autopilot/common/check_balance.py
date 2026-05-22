#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    check_balance,
    decimal_to_base_units,
    dump_json,
    is_native,
    load_local_env,
    normalize_chain,
    parse_amount,
    resolve_token,
    resolve_wallet_address,
)
from uniswap_autopilot.execute._internal.rpc import resolve_rpc_url


def main() -> None:
    parser = argparse.ArgumentParser(description="检查钱包代币余额")
    parser.add_argument("--chain", required=True, help="链名")
    parser.add_argument("--token", required=True, help="代币 symbol 或地址")
    parser.add_argument("--amount", help="要检查的数量（人类可读），如 1.5")
    parser.add_argument("--wallet", help="钱包地址（默认从 env 读取）")
    parser.add_argument("--rpc-url", help="RPC URL")
    args = parser.parse_args()

    load_local_env()
    chain = normalize_chain(args.chain)
    rpc = args.rpc_url or resolve_rpc_url(None, chain.chain_id)[0]

    wallet = resolve_wallet_address(args.wallet)
    if not wallet:
        print(json.dumps({"error": "wallet address required"}), file=sys.stderr)
        sys.exit(1)

    token = resolve_token(chain, args.token, rpc)

    if args.amount:
        amount_base = decimal_to_base_units(parse_amount(args.amount), token["decimals"])
        result = check_balance(chain, token, amount_base, wallet, rpc)
        result["humanAmount"] = args.amount
        result["token"] = token["symbol"]
        result["wallet"] = wallet
        status = "OK" if result.get("ok") else "INSUFFICIENT"
        print(f"{status}: {token['symbol']} balance={result.get('balance', '?')} required={amount_base} ({args.amount})")
    else:
        from uniswap_autopilot.execute._internal.rpc import query_erc20_balance, query_native_balance
        if is_native(chain, args.token) or token.get("address") == "NATIVE":
            raw = query_native_balance(wallet, rpc)
        else:
            raw = query_erc20_balance(wallet, token["address"], rpc)
        result = {"token": token["symbol"], "wallet": wallet, "balance": str(raw), "decimals": token["decimals"]}
        print(f"{token['symbol']}: balance={raw} ({token['decimals']} decimals)")

    dump_json(result)


if __name__ == "__main__":
    main()
