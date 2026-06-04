#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    decimal_to_base_units,
    dump_json,
    get_v2_router02_address,
    load_local_env,
    normalize_chain,
    parse_amount,
    resolve_token,
    resolve_wallet_address,
    sort_token_addresses,
)
from uniswap_autopilot.execute._internal.rpc import build_calldata, encode_address, encode_uint
from uniswap_autopilot.lp.v2.pair import query_pair_full_info


def _apply_slippage(amount_base_units: str, slippage_pct: float) -> str:
    from decimal import Decimal
    amount = Decimal(amount_base_units)
    factor = Decimal("1") - Decimal(str(slippage_pct)) / Decimal("100")
    return str(int(amount * factor))


def build_add_liquidity_tx(
    chain_name: str,
    token_a: str,
    token_b: str,
    amount_a: str,
    amount_b: str,
    slippage_pct: float = 0.5,
    recipient: str | None = None,
    deadline_seconds: int = 600,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    router02 = get_v2_router02_address(chain_name)

    tok_a = resolve_token(chain, token_a, rpc_url)
    tok_b = resolve_token(chain, token_b, rpc_url)
    if tok_a["address"] == "NATIVE" or tok_b["address"] == "NATIVE":
        raise ValueError("V2 LP does not support NATIVE token; use wrapped token (e.g. WETH instead of ETH)")
    token0, token1 = sort_token_addresses(tok_a["address"], tok_b["address"])

    # Map amounts to sorted token order
    if tok_a["address"].lower() == token0.lower():
        amount0_human, amount1_human = amount_a, amount_b
        dec0, dec1 = tok_a["decimals"], tok_b["decimals"]
    else:
        amount0_human, amount1_human = amount_b, amount_a
        dec0, dec1 = tok_b["decimals"], tok_a["decimals"]

    amount0_desired = decimal_to_base_units(parse_amount(amount0_human), dec0)
    amount1_desired = decimal_to_base_units(parse_amount(amount1_human), dec1)
    amount0_min = _apply_slippage(amount0_desired, slippage_pct)
    amount1_min = _apply_slippage(amount1_desired, slippage_pct)

    to = recipient or resolve_wallet_address(None) or ""
    if not to:
        raise ValueError("recipient wallet address is required (--recipient or configure wallet env)")

    deadline = str(int(time.time()) + deadline_seconds)

    calldata = build_calldata(
        "addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)",
        encode_address(token0),
        encode_address(token1),
        encode_uint(int(amount0_desired)),
        encode_uint(int(amount1_desired)),
        encode_uint(int(amount0_min)),
        encode_uint(int(amount1_min)),
        encode_address(to),
        encode_uint(int(deadline)),
    )

    return {
        "action": "v2_add_liquidity",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "token0": {"address": token0, "decimals": dec0, "amountDesired": amount0_desired, "amountMin": amount0_min},
        "token1": {"address": token1, "decimals": dec1, "amountDesired": amount1_desired, "amountMin": amount1_min},
        "recipient": to,
        "deadline": deadline,
        "transaction": {
            "kind": "v2_add_liquidity",
            "to": router02,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": to,
        },
    }


def build_remove_liquidity_tx(
    chain_name: str,
    token_a: str,
    token_b: str,
    liquidity: str,
    slippage_pct: float = 0.5,
    recipient: str | None = None,
    deadline_seconds: int = 600,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    router02 = get_v2_router02_address(chain_name)

    tok_a = resolve_token(chain, token_a, rpc_url)
    tok_b = resolve_token(chain, token_b, rpc_url)
    token0, token1 = sort_token_addresses(tok_a["address"], tok_b["address"])
    if tok_a["address"].lower() == token0.lower():
        dec0, dec1 = tok_a["decimals"], tok_b["decimals"]
    else:
        dec0, dec1 = tok_b["decimals"], tok_a["decimals"]

    deadline = str(int(time.time()) + deadline_seconds)
    to = recipient or resolve_wallet_address(None) or ""

    # Query current reserves to estimate minimum amounts
    pair_info = query_pair_full_info(chain_name, tok_a["address"], tok_b["address"], rpc_url)
    if not pair_info.get("exists"):
        raise ValueError(f"V2 pair {tok_a['symbol']}/{tok_b['symbol']} does not exist on {chain_name}")

    reserves = pair_info.get("reserves", {})
    total_supply = int(pair_info.get("totalSupply", "1"))
    liquidity_int = int(liquidity)

    # Pro-rata: amount_min = (liquidity * reserve) / totalSupply * (1 - slippage)
    from decimal import Decimal
    reserve0 = reserves.get("reserve0", 0)
    reserve1 = reserves.get("reserve1", 0)

    share0 = Decimal(liquidity_int) * Decimal(reserve0) / Decimal(total_supply)
    share1 = Decimal(liquidity_int) * Decimal(reserve1) / Decimal(total_supply)
    factor = Decimal("1") - Decimal(str(slippage_pct)) / Decimal("100")
    amount0_min = str(int(share0 * factor))
    amount1_min = str(int(share1 * factor))

    calldata = build_calldata(
        "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)",
        encode_address(token0),
        encode_address(token1),
        encode_uint(int(liquidity_int)),
        encode_uint(int(amount0_min)),
        encode_uint(int(amount1_min)),
        encode_address(to),
        encode_uint(int(deadline)),
    )

    return {
        "action": "v2_remove_liquidity",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "token0": {"address": token0, "decimals": dec0, "amountMin": amount0_min},
        "token1": {"address": token1, "decimals": dec1, "amountMin": amount1_min},
        "liquidity": str(liquidity_int),
        "recipient": to,
        "deadline": deadline,
        "transaction": {
            "kind": "v2_remove_liquidity",
            "to": router02,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": to,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 V2 LP 交易")
    sub = parser.add_subparsers(dest="command")

    add = sub.add_parser("add", help="构建 addLiquidity 交易")
    add.add_argument("--chain", required=True, help="链名")
    add.add_argument("--token-a", required=True, help="代币 A")
    add.add_argument("--token-b", required=True, help="代币 B")
    add.add_argument("--amount-a", required=True, help="代币 A 数量")
    add.add_argument("--amount-b", required=True, help="代币 B 数量")
    add.add_argument("--slippage", type=float, default=0.5, help="滑点百分比，默认 0.5")
    add.add_argument("--recipient", help="接收地址")
    add.add_argument("--rpc-url", help="RPC URL")
    add.add_argument("--output", help="输出 JSON 文件路径")

    remove = sub.add_parser("remove", help="构建 removeLiquidity 交易")
    remove.add_argument("--chain", required=True, help="链名")
    remove.add_argument("--token-a", required=True, help="代币 A")
    remove.add_argument("--token-b", required=True, help="代币 B")
    remove.add_argument("--liquidity", required=True, help="要移除的 LP token 数量（base units）")
    remove.add_argument("--slippage", type=float, default=0.5, help="滑点百分比")
    remove.add_argument("--recipient", help="接收地址")
    remove.add_argument("--rpc-url", help="RPC URL")
    remove.add_argument("--output", help="输出 JSON 文件路径")

    args = parser.parse_args()
    load_local_env()

    if args.command == "add":
        result = build_add_liquidity_tx(
            args.chain, args.token_a, args.token_b,
            args.amount_a, args.amount_b,
            args.slippage, args.recipient, rpc_url=args.rpc_url,
        )
    elif args.command == "remove":
        result = build_remove_liquidity_tx(
            args.chain, args.token_a, args.token_b,
            args.liquidity, args.slippage, args.recipient, rpc_url=args.rpc_url,
        )
    else:
        parser.print_help()
        sys.exit(1)

    dump_json(result)
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
