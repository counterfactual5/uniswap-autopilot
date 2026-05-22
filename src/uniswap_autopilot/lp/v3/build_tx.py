#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import (
    decimal_to_base_units,
    dump_json,
    get_position_manager_address,
    load_local_env,
    normalize_chain,
    parse_amount,
    resolve_token,
    resolve_wallet_address,
    sort_token_addresses,
    validate_fee_tier,
)
from uniswap_autopilot.execute._internal.rpc import build_calldata, encode_address, encode_int, encode_uint
from uniswap_autopilot.lp.v3.pool import query_pool_full_info
from uniswap_autopilot.lp.v3.position import query_position
from uniswap_autopilot.lp.v3.tick import fee_tier_to_tick_spacing


def _encode_mint_calldata(
    token0: str, token1: str, fee: int,
    tick_lower: int, tick_upper: int,
    amount0_desired: str, amount1_desired: str,
    amount0_min: str, amount1_min: str,
    recipient: str, deadline: str,
) -> str:
    return build_calldata(
        "mint((address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256))",
        encode_address(token0),
        encode_address(token1),
        encode_uint(fee),
        encode_int(tick_lower),
        encode_int(tick_upper),
        encode_uint(int(amount0_desired)),
        encode_uint(int(amount1_desired)),
        encode_uint(int(amount0_min)),
        encode_uint(int(amount1_min)),
        encode_address(recipient),
        encode_uint(int(deadline)),
    )


def _encode_increase_liquidity_calldata(
    token_id: str,
    amount0_desired: str, amount1_desired: str,
    amount0_min: str, amount1_min: str,
    deadline: str,
) -> str:
    return build_calldata(
        "increaseLiquidity((uint256,uint256,uint256,uint256,uint256))",
        encode_uint(int(token_id)),
        encode_uint(int(amount0_desired)),
        encode_uint(int(amount1_desired)),
        encode_uint(int(amount0_min)),
        encode_uint(int(amount1_min)),
        encode_uint(int(deadline)),
    )


def _encode_decrease_liquidity_calldata(
    token_id: str, liquidity: str,
    amount0_min: str, amount1_min: str,
    deadline: str,
) -> str:
    return build_calldata(
        "decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))",
        encode_uint(int(token_id)),
        encode_uint(int(liquidity)),
        encode_uint(int(amount0_min)),
        encode_uint(int(amount1_min)),
        encode_uint(int(deadline)),
    )


def _encode_collect_calldata(
    token_id: str, recipient: str,
    amount0_max: str, amount1_max: str,
) -> str:
    return build_calldata(
        "collect((uint256,address,uint128,uint128))",
        encode_uint(int(token_id)),
        encode_address(recipient),
        encode_uint(int(amount0_max)),
        encode_uint(int(amount1_max)),
    )


def _apply_slippage(amount_str: str, slippage_pct: float) -> str:
    from decimal import Decimal, ROUND_DOWN
    amount = Decimal(amount_str)
    factor = Decimal("1") - Decimal(str(slippage_pct)) / Decimal("100")
    result = (amount * factor).to_integral_value(rounding=ROUND_DOWN)
    return str(result)


def build_mint_transaction(
    chain_name: str,
    token_a: str,
    token_b: str,
    fee_tier: int,
    tick_lower: int,
    tick_upper: int,
    amount_a: str,
    amount_b: str,
    slippage_pct: float = 0.5,
    recipient: str | None = None,
    deadline_seconds: int = 600,
    rpc_url: str | None = None,
    request_only: bool = False,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    fee = validate_fee_tier(fee_tier)
    pm = get_position_manager_address(chain_name)

    if tick_lower >= tick_upper:
        raise ValueError(f"tick_lower ({tick_lower}) must be less than tick_upper ({tick_upper})")

    token_a_info = resolve_token(chain, token_a, rpc_url)
    token_b_info = resolve_token(chain, token_b, rpc_url)
    addr_a = token_a_info["address"]
    addr_b = token_b_info["address"]
    if addr_a == "NATIVE" or addr_b == "NATIVE":
        raise ValueError("LP does not support NATIVE; use wrapped token (e.g. WETH)")

    token0_addr, token1_addr = sort_token_addresses(addr_a, addr_b)
    decimals0 = token_a_info["decimals"] if token0_addr == addr_a else token_b_info["decimals"]
    decimals1 = token_b_info["decimals"] if token1_addr == addr_b else token_a_info["decimals"]

    amount0_human = amount_a if token0_addr == addr_a else amount_b
    amount1_human = amount_b if token1_addr == addr_b else amount_a

    base0 = decimal_to_base_units(parse_amount(amount0_human), decimals0)
    base1 = decimal_to_base_units(parse_amount(amount1_human), decimals1)
    min0 = _apply_slippage(base0, slippage_pct)
    min1 = _apply_slippage(base1, slippage_pct)

    wallet = recipient or os.environ.get("SECURE_WALLET_ADDRESS") or os.environ.get("HOT_WALLET_ADDRESS")
    if not wallet:
        raise ValueError("recipient is required; pass --recipient or set wallet env")

    deadline = str(int(time.time()) + deadline_seconds)
    calldata = _encode_mint_calldata(
        token0_addr, token1_addr, fee,
        tick_lower, tick_upper,
        base0, base1, min0, min1,
        wallet, deadline,
    )

    result: dict[str, Any] = {
        "action": "lp_mint",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "feeTier": fee,
        "tickLower": tick_lower,
        "tickUpper": tick_upper,
        "token0": {"address": token0_addr, "decimals": decimals0, "amountDesired": base0, "amountMin": min0},
        "token1": {"address": token1_addr, "decimals": decimals1, "amountDesired": base1, "amountMin": min1},
        "recipient": wallet,
        "deadline": deadline,
        "transaction": {
            "kind": "lp_mint",
            "to": pm,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": wallet,
        },
    }

    if not request_only and rpc_url:
        try:
            pool_info = query_pool_full_info(chain_name, token_a, token_b, fee_tier, rpc_url)
            result["pool"] = pool_info
        except Exception as exc:
            result["poolError"] = str(exc)

    return result


def build_increase_liquidity_transaction(
    chain_name: str,
    token_id: int,
    amount0: str,
    amount1: str,
    slippage_pct: float = 0.5,
    deadline_seconds: int = 600,
    rpc_url: str | None = None,
    wallet: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pm = get_position_manager_address(chain_name)
    rpc_url_resolved = rpc_url
    if not rpc_url_resolved:
        from uniswap_autopilot.execute._internal.rpc import resolve_rpc_url
        r, _ = resolve_rpc_url(None, chain.chain_id)
        rpc_url_resolved = r
    if not rpc_url_resolved:
        raise RuntimeError(f"RPC URL not configured for {chain_name}; set an RPC env var or pass --rpc-url")

    pos = query_position(token_id, chain_name, rpc_url_resolved)
    if pos["liquidity"] == "0":
        raise ValueError(f"position {token_id} has no liquidity; use mint instead")

    owner = resolve_wallet_address(wallet) or os.environ.get("SECURE_WALLET_ADDRESS") or os.environ.get("HOT_WALLET_ADDRESS")
    if not owner:
        raise ValueError("wallet is required; pass --wallet or set wallet env")

    token0_info = resolve_token(chain, pos["token0"])
    token1_info = resolve_token(chain, pos["token1"])
    base0 = decimal_to_base_units(parse_amount(amount0), token0_info["decimals"])
    base1 = decimal_to_base_units(parse_amount(amount1), token1_info["decimals"])
    min0 = _apply_slippage(base0, slippage_pct)
    min1 = _apply_slippage(base1, slippage_pct)

    deadline = str(int(time.time()) + deadline_seconds)
    calldata = _encode_increase_liquidity_calldata(
        str(token_id), base0, base1, min0, min1, deadline,
    )

    return {
        "action": "lp_increase",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "position": pos,
        "owner": owner,
        "token0": {"address": pos["token0"], "decimals": token0_info["decimals"], "amountDesired": base0, "amountMin": min0},
        "token1": {"address": pos["token1"], "decimals": token1_info["decimals"], "amountDesired": base1, "amountMin": min1},
        "deadline": deadline,
        "transaction": {
            "kind": "lp_increase",
            "to": pm,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": owner,
        },
    }


def build_decrease_liquidity_transaction(
    chain_name: str,
    token_id: int,
    liquidity_pct: float,
    slippage_pct: float = 0.5,
    deadline_seconds: int = 600,
    rpc_url: str | None = None,
    wallet: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pm = get_position_manager_address(chain_name)
    rpc_url_resolved = rpc_url
    if not rpc_url_resolved:
        from uniswap_autopilot.execute._internal.rpc import resolve_rpc_url
        r, _ = resolve_rpc_url(None, chain.chain_id)
        rpc_url_resolved = r
    if not rpc_url_resolved:
        raise RuntimeError(f"RPC URL not configured for {chain_name}; set an RPC env var or pass --rpc-url")

    pos = query_position(token_id, chain_name, rpc_url_resolved)
    current_liq = int(pos["liquidity"])
    if current_liq == 0:
        raise ValueError(f"position {token_id} has no liquidity to remove")

    owner = resolve_wallet_address(wallet) or os.environ.get("SECURE_WALLET_ADDRESS") or os.environ.get("HOT_WALLET_ADDRESS")
    if not owner:
        raise ValueError("wallet is required; pass --wallet or set wallet env")

    if liquidity_pct <= 0 or liquidity_pct > 100:
        raise ValueError("liquidity_pct must be between 0 and 100")
    remove_liq = int(current_liq * liquidity_pct / 100)
    if remove_liq == 0:
        raise ValueError(
            f"liquidity_pct {liquidity_pct}% of {current_liq} rounds to 0; increase percentage or remove all"
        )

    token0_info = resolve_token(chain, pos["token0"])
    token1_info = resolve_token(chain, pos["token1"])
    min0 = "0"
    min1 = "0"
    if slippage_pct > 0:
        from decimal import Decimal, ROUND_DOWN
        frac = Decimal(remove_liq) / Decimal(current_liq)
        amt0_est = (Decimal(pos.get("amount0", "0")) * frac).to_integral_value(rounding=ROUND_DOWN)
        amt1_est = (Decimal(pos.get("amount1", "0")) * frac).to_integral_value(rounding=ROUND_DOWN)
        min0 = _apply_slippage(str(amt0_est), slippage_pct)
        min1 = _apply_slippage(str(amt1_est), slippage_pct)

    deadline = str(int(time.time()) + deadline_seconds)
    calldata = _encode_decrease_liquidity_calldata(
        str(token_id), str(remove_liq), min0, min1, deadline,
    )

    return {
        "action": "lp_decrease",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "position": pos,
        "owner": owner,
        "currentLiquidity": str(current_liq),
        "removeLiquidity": str(remove_liq),
        "removePct": liquidity_pct,
        "deadline": deadline,
        "transaction": {
            "kind": "lp_decrease",
            "to": pm,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": owner,
        },
    }


def build_collect_transaction(
    chain_name: str,
    token_id: int,
    recipient: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pm = get_position_manager_address(chain_name)

    wallet = recipient or os.environ.get("SECURE_WALLET_ADDRESS") or os.environ.get("HOT_WALLET_ADDRESS")
    if not wallet:
        raise ValueError("recipient is required; pass --recipient or set wallet env")

    max_uint128 = str(2**128 - 1)
    calldata = _encode_collect_calldata(str(token_id), wallet, max_uint128, max_uint128)

    return {
        "action": "lp_collect",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "recipient": wallet,
        "transaction": {
            "kind": "lp_collect",
            "to": pm,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": wallet,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="构造 Uniswap v3 LP 交易")
    sub = parser.add_subparsers(dest="command", required=True)

    # mint
    mint_p = sub.add_parser("mint", help="创建新 LP 仓位")
    mint_p.add_argument("--chain", required=True)
    mint_p.add_argument("--token-a", required=True)
    mint_p.add_argument("--token-b", required=True)
    mint_p.add_argument("--fee-tier", type=int, required=True)
    mint_p.add_argument("--tick-lower", type=int, required=True)
    mint_p.add_argument("--tick-upper", type=int, required=True)
    mint_p.add_argument("--amount-a", required=True, help="tokenA 数量（人类可读）")
    mint_p.add_argument("--amount-b", required=True, help="tokenB 数量（人类可读）")
    mint_p.add_argument("--slippage", type=float, default=0.5)
    mint_p.add_argument("--recipient", help="接收 LP NFT 的地址")
    mint_p.add_argument("--deadline", type=int, default=600)
    mint_p.add_argument("--rpc-url")
    mint_p.add_argument("--request-only", action="store_true")
    mint_p.add_argument("--output")

    # increase
    inc_p = sub.add_parser("increase", help="增加流动性")
    inc_p.add_argument("--chain", required=True)
    inc_p.add_argument("--token-id", type=int, required=True)
    inc_p.add_argument("--amount0", required=True)
    inc_p.add_argument("--amount1", required=True)
    inc_p.add_argument("--slippage", type=float, default=0.5)
    inc_p.add_argument("--deadline", type=int, default=600)
    inc_p.add_argument("--rpc-url")
    inc_p.add_argument("--output")

    # decrease
    dec_p = sub.add_parser("decrease", help="减少流动性")
    dec_p.add_argument("--chain", required=True)
    dec_p.add_argument("--token-id", type=int, required=True)
    dec_p.add_argument("--liquidity-pct", type=float, required=True, help="移除百分比 (1-100)")
    dec_p.add_argument("--slippage", type=float, default=0.5)
    dec_p.add_argument("--deadline", type=int, default=600)
    dec_p.add_argument("--rpc-url")
    dec_p.add_argument("--output")

    # collect
    col_p = sub.add_parser("collect", help="收取手续费")
    col_p.add_argument("--chain", required=True)
    col_p.add_argument("--token-id", type=int, required=True)
    col_p.add_argument("--recipient")
    col_p.add_argument("--output")

    args = parser.parse_args()

    try:
        load_local_env()
        if args.command == "mint":
            result = build_mint_transaction(
                chain_name=args.chain, token_a=args.token_a, token_b=args.token_b,
                fee_tier=args.fee_tier, tick_lower=args.tick_lower, tick_upper=args.tick_upper,
                amount_a=args.amount_a, amount_b=args.amount_b,
                slippage_pct=args.slippage, recipient=args.recipient,
                deadline_seconds=args.deadline, rpc_url=args.rpc_url,
                request_only=args.request_only,
            )
        elif args.command == "increase":
            result = build_increase_liquidity_transaction(
                chain_name=args.chain, token_id=args.token_id,
                amount0=args.amount0, amount1=args.amount1,
                slippage_pct=args.slippage, deadline_seconds=args.deadline,
                rpc_url=args.rpc_url,
            )
        elif args.command == "decrease":
            result = build_decrease_liquidity_transaction(
                chain_name=args.chain, token_id=args.token_id,
                liquidity_pct=args.liquidity_pct, slippage_pct=args.slippage,
                deadline_seconds=args.deadline, rpc_url=args.rpc_url,
            )
        elif args.command == "collect":
            result = build_collect_transaction(
                chain_name=args.chain, token_id=args.token_id,
                recipient=args.recipient,
            )
        else:
            parser.print_help()
            sys.exit(1)

        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
