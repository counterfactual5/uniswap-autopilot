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
    get_v4_pool_manager_address,
    get_v4_position_manager_address,
    get_v4_state_view_address,
    load_local_env,
    normalize_chain,
    parse_amount,
    resolve_token,
    resolve_wallet_address,
    sort_token_addresses,
)
from uniswap_autopilot.execute._internal.rpc import build_calldata, encode_uint
from uniswap_autopilot.lp.v3.tick import tick_to_sqrt_price_x96
from uniswap_autopilot.lp.v4.pool import compute_pool_id, query_v4_slot0

# V4 Action constants
INCREASE_LIQUIDITY = 0x00
DECREASE_LIQUIDITY = 0x01
MINT_POSITION = 0x02
BURN_POSITION = 0x03
COLLECT = 0x06
TAKE = 0x09
SETTLE = 0x0A
SETTLE_PAIR = 0x0B
TAKE_PAIR = 0x0D
CLOSE_CURRENCY = 0x12
SWEEP = 0x14

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
_UINT256_MOD = 2**256


def _encode_parameters(tick_spacing: int, hooks_registration: int = 0) -> int:
    return (tick_spacing << 24) | hooks_registration


def _uint256(val: int) -> bytes:
    return val.to_bytes(32, "big")


def _uint128(val: int) -> bytes:
    return val.to_bytes(16, "big")


def _address(addr: str) -> bytes:
    return int(addr, 16).to_bytes(32, "big")


def _encode_bytes(data: bytes) -> bytes:
    length = len(data)
    padded = data + b"\x00" * ((32 - length % 32) % 32)
    return _uint256(length) + padded


def _encode_unlock_data(actions: list[int], params: list[bytes]) -> str:
    """Build unlockData = abi.encode(bytes actions, bytes[] params)."""
    if len(actions) != len(params):
        raise ValueError("actions and params must have same length")

    actions_bytes = bytes(actions)

    # Encode bytes[] params
    params_encoded = b""
    offsets = []
    # 1 word for array length, then N words for offsets, then data
    data_start = 32 + 32 * len(params)
    current_offset = data_start
    for p in params:
        offsets.append(current_offset)
        padded_len = len(p) + ((32 - len(p) % 32) % 32)
        current_offset += 32 + padded_len

    params_encoded += _uint256(len(params))
    for off in offsets:
        params_encoded += _uint256(off)
    for p in params:
        params_encoded += _encode_bytes(p)

    # Encode (bytes, bytes[])
    # offset_actions = 64 (after two uint256 offset words)
    # offset_params = 64 + size_of_actions_encoding
    actions_padded = actions_bytes + b"\x00" * ((32 - len(actions_bytes) % 32) % 32)
    actions_encoded = _uint256(len(actions_bytes)) + actions_padded
    offset_actions = 64
    offset_params = offset_actions + len(actions_encoded)

    result = _uint256(offset_actions) + _uint256(offset_params) + actions_encoded + params_encoded
    return "0x" + result.hex()


def _encode_pool_key(
    currency0: str,
    currency1: str,
    hooks: str,
    pool_manager: str,
    fee: int,
    tick_spacing: int,
) -> bytes:
    params = _encode_parameters(tick_spacing)
    return (
        _address(currency0)
        + _address(currency1)
        + _address(hooks)
        + _address(pool_manager)
        + _uint256(fee)
        + _uint256(params)
    )


def _encode_mint_params(
    pool_key_bytes: bytes,
    tick_lower: int,
    tick_upper: int,
    liquidity: int,
    amount0_max: int,
    amount1_max: int,
    owner: str,
    hook_data: bytes = b"",
) -> bytes:
    return (
        pool_key_bytes
        + _uint256(tick_lower % _UINT256_MOD)
        + _uint256(tick_upper % _UINT256_MOD)
        + _uint256(liquidity)
        + _uint128(amount0_max).rjust(32, b"\x00")
        + _uint128(amount1_max).rjust(32, b"\x00")
        + _address(owner)
        + _encode_bytes(hook_data)
    )


def _encode_decrease_params(
    token_id: int,
    liquidity: int,
    amount0_min: int,
    amount1_min: int,
    hook_data: bytes = b"",
) -> bytes:
    return (
        _uint256(token_id)
        + _uint256(liquidity)
        + _uint128(amount0_min).rjust(32, b"\x00")
        + _uint128(amount1_min).rjust(32, b"\x00")
        + _encode_bytes(hook_data)
    )


def _encode_settle_pair_params(currency0: str, currency1: str) -> bytes:
    return _address(currency0) + _address(currency1)


def _encode_take_pair_params(currency0: str, currency1: str, recipient: str) -> bytes:
    return _address(currency0) + _address(currency1) + _address(recipient)


def _encode_close_currency_params(currency: str, recipient: str) -> bytes:
    return _address(currency) + _address(recipient)


def _apply_slippage(amount_str: str, slippage_pct: float) -> str:
    from decimal import Decimal, ROUND_DOWN
    amount = Decimal(amount_str)
    factor = Decimal("1") - Decimal(str(slippage_pct)) / Decimal("100")
    result = (amount * factor).to_integral_value(rounding=ROUND_DOWN)
    return str(result)


def _get_liquidity_for_amount0(sqrt_a: int, sqrt_b: int, amount0: int) -> int:
    if amount0 == 0:
        return 0
    return amount0 * sqrt_a * sqrt_b // ((sqrt_b - sqrt_a) * (2 ** 96))


def _get_liquidity_for_amount1(sqrt_a: int, sqrt_b: int, amount1: int) -> int:
    if amount1 == 0:
        return 0
    return amount1 * (2 ** 96) // (sqrt_b - sqrt_a)


def _get_liquidity_for_amounts(
    sqrt_price_x96: int,
    sqrt_ratio_a_x96: int,
    sqrt_ratio_b_x96: int,
    amount0: int,
    amount1: int,
) -> int:
    if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
        sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96
    if sqrt_price_x96 <= sqrt_ratio_a_x96:
        return _get_liquidity_for_amount0(sqrt_ratio_a_x96, sqrt_ratio_b_x96, amount0)
    elif sqrt_price_x96 < sqrt_ratio_b_x96:
        liq0 = _get_liquidity_for_amount0(sqrt_price_x96, sqrt_ratio_b_x96, amount0)
        liq1 = _get_liquidity_for_amount1(sqrt_ratio_a_x96, sqrt_price_x96, amount1)
        return min(liq0, liq1)
    else:
        return _get_liquidity_for_amount1(sqrt_ratio_a_x96, sqrt_ratio_b_x96, amount1)


def _get_amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if liquidity == 0 or sqrt_b <= sqrt_a:
        return 0
    return liquidity * (2 ** 96) * (sqrt_b - sqrt_a) // (sqrt_a * sqrt_b)


def _get_amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if liquidity == 0 or sqrt_b <= sqrt_a:
        return 0
    return liquidity * (sqrt_b - sqrt_a) // (2 ** 96)


def _compute_expected_amounts(
    current_tick: int, tick_lower: int, tick_upper: int, liquidity: int,
) -> tuple[int, int]:
    """Estimate token amounts for a given liquidity in a tick range."""
    if liquidity == 0:
        return 0, 0
    sqrt_price = tick_to_sqrt_price_x96(current_tick)
    sqrt_a = tick_to_sqrt_price_x96(tick_lower)
    sqrt_b = tick_to_sqrt_price_x96(tick_upper)
    if current_tick <= tick_lower:
        return _get_amount0_delta(sqrt_a, sqrt_b, liquidity), 0
    elif current_tick < tick_upper:
        return (
            _get_amount0_delta(sqrt_price, sqrt_b, liquidity),
            _get_amount1_delta(sqrt_a, sqrt_price, liquidity),
        )
    else:
        return 0, _get_amount1_delta(sqrt_a, sqrt_b, liquidity)


def _build_modify_liquidities_calldata(unlock_data_hex: str, deadline: int) -> str:
    # modifyLiquidities(bytes,uint256) — manual ABI encoding for dynamic bytes
    sel = "0x0b22dd98"  # modifyLiquidities(bytes,uint256)
    data_bytes = bytes.fromhex(unlock_data_hex.replace("0x", ""))
    data_len = len(data_bytes)
    padded_len = data_len + ((32 - data_len % 32) % 32)
    # slot0: offset to bytes = 64 (two 32-byte words: offset + uint256)
    # slot1: uint256 deadline
    # slot2: bytes length
    # slot3+: bytes data padded to 32-byte boundary
    encoded = (
        sel.replace("0x", "")
        + hex(64)[2:].rjust(64, "0")  # offset to bytes
        + encode_uint(deadline)  # uint256 deadline
        + hex(data_len)[2:].rjust(64, "0")  # bytes length
        + data_bytes.hex().ljust(padded_len * 2, "0")  # padded data
    )
    return "0x" + encoded


def build_v4_mint_transaction(
    chain_name: str,
    token_a: str,
    token_b: str,
    fee: int,
    tick_spacing: int,
    tick_lower: int,
    tick_upper: int,
    amount_a: str,
    amount_b: str,
    slippage_pct: float = 0.5,
    hooks: str = ZERO_ADDRESS,
    recipient: str | None = None,
    deadline_seconds: int = 600,
    rpc_url: str | None = None,
    request_only: bool = False,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pm = get_v4_position_manager_address(chain_name)
    pool_manager = get_v4_pool_manager_address(chain_name)

    tok_a = resolve_token(chain, token_a, rpc_url)
    tok_b = resolve_token(chain, token_b, rpc_url)
    c0, c1 = sort_token_addresses(tok_a["address"], tok_b["address"])

    # Remap amounts: after sort, c0<=c1 but amounts must follow
    if c0.lower() == tok_a["address"].lower():
        decimals0, decimals1 = tok_a["decimals"], tok_b["decimals"]
        amount0_human, amount1_human = amount_a, amount_b
    else:
        decimals0, decimals1 = tok_b["decimals"], tok_a["decimals"]
        amount0_human, amount1_human = amount_b, amount_a

    base0 = decimal_to_base_units(parse_amount(amount0_human), decimals0)
    base1 = decimal_to_base_units(parse_amount(amount1_human), decimals1)

    if tick_lower >= tick_upper:
        raise ValueError(f"tick_lower ({tick_lower}) must be less than tick_upper ({tick_upper})")

    wallet = recipient or os.environ.get("SECURE_WALLET_ADDRESS") or os.environ.get("HOT_WALLET_ADDRESS")
    if not wallet:
        raise ValueError("recipient is required")

    # V4 requires client-side liquidity calculation
    state_view = get_v4_state_view_address(chain_name)
    pool_id = compute_pool_id(c0, c1, hooks, pool_manager, fee, tick_spacing)
    slot0 = query_v4_slot0(state_view, pool_id, rpc_url) if rpc_url else None
    sqrt_price_x96 = int(slot0["sqrtPriceX96"]) if slot0 else 0

    sqrt_a = tick_to_sqrt_price_x96(tick_lower)
    sqrt_b = tick_to_sqrt_price_x96(tick_upper)

    if sqrt_price_x96 > 0:
        liquidity = _get_liquidity_for_amounts(sqrt_price_x96, sqrt_a, sqrt_b, int(base0), int(base1))
    else:
        # Cannot compute without pool state; caller must ensure rpc_url is provided
        raise ValueError("RPC URL is required for V4 mint (pool state needed for liquidity calculation)")

    deadline = str(int(time.time()) + deadline_seconds)

    pool_key_bytes = _encode_pool_key(c0, c1, hooks, pool_manager, fee, tick_spacing)
    mint_params = _encode_mint_params(
        pool_key_bytes, tick_lower, tick_upper, liquidity,
        int(base0), int(base1), wallet,
    )
    settle_pair_params = _encode_settle_pair_params(c0, c1)

    unlock_data = _encode_unlock_data(
        [MINT_POSITION, SETTLE_PAIR],
        [mint_params, settle_pair_params],
    )

    calldata = _build_modify_liquidities_calldata(unlock_data, int(deadline))

    result: dict[str, Any] = {
        "action": "v4_mint",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "fee": fee,
        "tickSpacing": tick_spacing,
        "hooks": hooks,
        "tickLower": tick_lower,
        "tickUpper": tick_upper,
        "currency0": c0,
        "currency1": c1,
        "liquidity": str(liquidity),
        "amount0Max": base0,
        "amount1Max": base1,
        "recipient": wallet,
        "deadline": deadline,
        "transaction": {
            "kind": "v4_mint",
            "to": pm,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": wallet,
        },
    }
    return result


def build_v4_decrease_liquidity_transaction(
    chain_name: str,
    token_id: int,
    liquidity_pct: float,
    slippage_pct: float = 0.5,
    deadline_seconds: int = 600,
    rpc_url: str | None = None,
    wallet: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pm = get_v4_position_manager_address(chain_name)

    owner = resolve_wallet_address(wallet) or os.environ.get("SECURE_WALLET_ADDRESS") or os.environ.get("HOT_WALLET_ADDRESS")
    if not owner:
        raise ValueError("wallet is required")

    from uniswap_autopilot.lp.v4.position import query_v4_position
    pos = query_v4_position(token_id, chain_name, rpc_url)
    current_liq = int(pos["liquidity"])
    if current_liq == 0:
        raise ValueError(f"position {token_id} has no liquidity")

    if liquidity_pct <= 0 or liquidity_pct > 100:
        raise ValueError("liquidity_pct must be between 0 and 100")
    remove_liq = int(current_liq * liquidity_pct / 100)
    if remove_liq == 0:
        raise ValueError("liquidity_pct results in 0 liquidity")

    # Compute expected amounts from liquidity for proper slippage protection
    current_tick = pos.get("currentTick")
    tick_lower = pos["tickLower"]
    tick_upper = pos["tickUpper"]
    amount0_min = "0"
    amount1_min = "0"
    if current_tick is not None and slippage_pct > 0:
        est0, est1 = _compute_expected_amounts(current_tick, tick_lower, tick_upper, remove_liq)
        if est0 > 0:
            amount0_min = _apply_slippage(str(est0), slippage_pct)
        if est1 > 0:
            amount1_min = _apply_slippage(str(est1), slippage_pct)

    deadline = str(int(time.time()) + deadline_seconds)
    c0 = pos["currency0"]["address"]
    c1 = pos["currency1"]["address"]

    decrease_params = _encode_decrease_params(token_id, remove_liq, int(amount0_min), int(amount1_min))
    take_pair_params = _encode_take_pair_params(c0, c1, owner)

    unlock_data = _encode_unlock_data(
        [DECREASE_LIQUIDITY, TAKE_PAIR],
        [decrease_params, take_pair_params],
    )
    calldata = _build_modify_liquidities_calldata(unlock_data, int(deadline))

    return {
        "action": "v4_decrease",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "owner": owner,
        "currentLiquidity": str(current_liq),
        "removeLiquidity": str(remove_liq),
        "removePct": liquidity_pct,
        "deadline": deadline,
        "transaction": {
            "kind": "v4_decrease",
            "to": pm,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": owner,
        },
    }


def build_v4_collect_transaction(
    chain_name: str,
    token_id: int,
    recipient: str | None = None,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    chain = normalize_chain(chain_name)
    pm = get_v4_position_manager_address(chain_name)

    wallet = recipient or os.environ.get("SECURE_WALLET_ADDRESS") or os.environ.get("HOT_WALLET_ADDRESS")
    if not wallet:
        raise ValueError("recipient is required")

    from uniswap_autopilot.lp.v4.position import query_v4_position
    pos = query_v4_position(token_id, chain_name, rpc_url)
    c0 = pos["currency0"]["address"]
    c1 = pos["currency1"]["address"]

    deadline = str(int(time.time()) + 600)
    decrease_params = _encode_decrease_params(token_id, 0, 0, 0)
    close0_params = _encode_close_currency_params(c0, wallet)
    close1_params = _encode_close_currency_params(c1, wallet)
    sweep0_params = _address(c0) + _address(wallet)
    sweep1_params = _address(c1) + _address(wallet)

    unlock_data = _encode_unlock_data(
        [DECREASE_LIQUIDITY, CLOSE_CURRENCY, CLOSE_CURRENCY, SWEEP, SWEEP],
        [decrease_params, close0_params, close1_params, sweep0_params, sweep1_params],
    )
    calldata = _build_modify_liquidities_calldata(unlock_data, int(deadline))

    return {
        "action": "v4_collect",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "recipient": wallet,
        "deadline": deadline,
        "transaction": {
            "kind": "v4_collect",
            "to": pm,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": wallet,
        },
    }


def build_v4_increase_liquidity_transaction(
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
    pm = get_v4_position_manager_address(chain_name)

    owner = resolve_wallet_address(wallet) or os.environ.get("SECURE_WALLET_ADDRESS") or os.environ.get("HOT_WALLET_ADDRESS")
    if not owner:
        raise ValueError("wallet is required")

    from uniswap_autopilot.lp.v4.position import query_v4_position
    pos = query_v4_position(token_id, chain_name, rpc_url)
    if pos["liquidity"] == "0":
        raise ValueError(f"position {token_id} has no liquidity; use mint instead")

    c0 = pos["currency0"]["address"]
    c1 = pos["currency1"]["address"]
    decimals0 = pos["currency0"]["decimals"]
    decimals1 = pos["currency1"]["decimals"]

    base0 = decimal_to_base_units(parse_amount(amount0), decimals0)
    base1 = decimal_to_base_units(parse_amount(amount1), decimals1)

    # V4 requires client-side liquidity calculation (same as mint)
    current_tick = pos.get("currentTick")
    if current_tick is None:
        raise ValueError("RPC URL is required for V4 increase (pool state needed for liquidity calculation)")

    tick_lower = pos["tickLower"]
    tick_upper = pos["tickUpper"]
    sqrt_price_x96 = tick_to_sqrt_price_x96(current_tick)
    sqrt_a = tick_to_sqrt_price_x96(tick_lower)
    sqrt_b = tick_to_sqrt_price_x96(tick_upper)
    liquidity = _get_liquidity_for_amounts(sqrt_price_x96, sqrt_a, sqrt_b, int(base0), int(base1))

    deadline = str(int(time.time()) + deadline_seconds)

    increase_params = (
        _uint256(token_id)
        + _uint256(liquidity)
        + _uint128(int(base0)).rjust(32, b"\x00")
        + _uint128(int(base1)).rjust(32, b"\x00")
        + _encode_bytes(b"")
    )
    settle_pair_params = _encode_settle_pair_params(c0, c1)

    unlock_data = _encode_unlock_data(
        [INCREASE_LIQUIDITY, SETTLE_PAIR],
        [increase_params, settle_pair_params],
    )
    calldata = _build_modify_liquidities_calldata(unlock_data, int(deadline))

    return {
        "action": "v4_increase",
        "chain": {"key": chain.key, "chainId": chain.chain_id},
        "tokenId": token_id,
        "position": pos,
        "owner": owner,
        "currency0": c0,
        "currency1": c1,
        "liquidity": str(liquidity),
        "amount0Max": base0,
        "amount1Max": base1,
        "deadline": deadline,
        "transaction": {
            "kind": "v4_increase",
            "to": pm,
            "data": calldata,
            "value": "0",
            "chainId": chain.chain_id,
            "from": owner,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Uniswap V4 LP transactions")
    sub = parser.add_subparsers(dest="command")

    m = sub.add_parser("mint", help="Mint a new V4 position")
    m.add_argument("--chain", required=True)
    m.add_argument("--token-a", required=True)
    m.add_argument("--token-b", required=True)
    m.add_argument("--fee", type=int, required=True)
    m.add_argument("--tick-spacing", type=int, required=True)
    m.add_argument("--tick-lower", type=int, required=True)
    m.add_argument("--tick-upper", type=int, required=True)
    m.add_argument("--amount-a", required=True)
    m.add_argument("--amount-b", required=True)
    m.add_argument("--slippage", type=float, default=0.5)
    m.add_argument("--hooks", default=ZERO_ADDRESS)
    m.add_argument("--recipient")
    m.add_argument("--deadline", type=int, default=600)
    m.add_argument("--rpc-url")
    m.add_argument("--request-only", action="store_true")
    m.add_argument("--output")

    d = sub.add_parser("decrease", help="Decrease liquidity")
    d.add_argument("--chain", required=True)
    d.add_argument("--token-id", type=int, required=True)
    d.add_argument("--liquidity-pct", type=float, required=True)
    d.add_argument("--slippage", type=float, default=0.5)
    d.add_argument("--deadline", type=int, default=600)
    d.add_argument("--rpc-url")
    d.add_argument("--output")

    inc = sub.add_parser("increase", help="Increase liquidity")
    inc.add_argument("--chain", required=True)
    inc.add_argument("--token-id", type=int, required=True)
    inc.add_argument("--amount0", required=True)
    inc.add_argument("--amount1", required=True)
    inc.add_argument("--slippage", type=float, default=0.5)
    inc.add_argument("--deadline", type=int, default=600)
    inc.add_argument("--rpc-url")
    inc.add_argument("--output")

    c = sub.add_parser("collect", help="Collect fees")
    c.add_argument("--chain", required=True)
    c.add_argument("--token-id", type=int, required=True)
    c.add_argument("--recipient")
    c.add_argument("--rpc-url")
    c.add_argument("--output")

    args = parser.parse_args()
    load_local_env()

    if args.command == "mint":
        result = build_v4_mint_transaction(
            chain_name=args.chain, token_a=args.token_a, token_b=args.token_b,
            fee=args.fee, tick_spacing=args.tick_spacing,
            tick_lower=args.tick_lower, tick_upper=args.tick_upper,
            amount_a=args.amount_a, amount_b=args.amount_b,
            slippage_pct=args.slippage, hooks=args.hooks,
            recipient=args.recipient, deadline_seconds=args.deadline,
            rpc_url=args.rpc_url, request_only=args.request_only,
        )
        print(f"V4 Mint tx: {args.token_a}/{args.token_b} fee={args.fee} ts={args.tick_spacing}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "decrease":
        result = build_v4_decrease_liquidity_transaction(
            chain_name=args.chain, token_id=args.token_id,
            liquidity_pct=args.liquidity_pct, slippage_pct=args.slippage,
            deadline_seconds=args.deadline, rpc_url=args.rpc_url,
        )
        print(f"V4 Decrease tx: position #{args.token_id} remove {args.liquidity_pct}%")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "increase":
        result = build_v4_increase_liquidity_transaction(
            chain_name=args.chain, token_id=args.token_id,
            amount0=args.amount0, amount1=args.amount1,
            slippage_pct=args.slippage, deadline_seconds=args.deadline,
            rpc_url=args.rpc_url,
        )
        print(f"V4 Increase tx: position #{args.token_id}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    elif args.command == "collect":
        result = build_v4_collect_transaction(
            chain_name=args.chain, token_id=args.token_id,
            recipient=args.recipient, rpc_url=args.rpc_url,
        )
        print(f"V4 Collect tx: position #{args.token_id}")
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dump_json(result)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
