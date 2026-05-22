#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


from uniswap_autopilot.common.common import dump_json, cache_token_from_search

# --- DexScreener (no key, 300 req/min) ---
DS_SEARCH = "https://api.dexscreener.com/latest/dex/search?q="
DS_TOKENS = "https://api.dexscreener.com/tokens/v1"

# --- GeckoTerminal (no key, ~10 req/min) ---
GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_HEADERS = {"Accept": "application/json;version=20230203", "User-Agent": "uniswap-autopilot/1.0"}

# Load chain config from chains.json
_DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
_CHAINS_CFG = json.loads(
    (_DATA_ROOT / "chains.json").read_text(encoding="utf-8")
)
SUPPORTED_CHAINS = sorted(_CHAINS_CFG.keys())
GT_NETWORK_IDS: dict[str, str] = {
    key: cfg["geckoterminalNetworkId"] for key, cfg in _CHAINS_CFG.items()
}

# Load token catalog for decimals lookup
_TOKEN_CATALOG: dict[str, dict[str, Any]] = json.loads(
    (_DATA_ROOT / "common-token-addresses.json").read_text(encoding="utf-8")
)


def _fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> Any:
    hdrs = {"Accept": "application/json", "User-Agent": "uniswap-autopilot/1.0"}
    if headers:
        hdrs.update(headers)
    request = Request(url, headers=hdrs)
    with urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _fmt_usd(val: float) -> str:
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    if val >= 1_000:
        return f"${val / 1_000:.1f}K"
    if val > 0:
        return f"${val:.4f}"
    return "-"


def _fmt_pct(val: float) -> str:
    if val == 0:
        return "-"
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.1f}%"


def _fmt_price(val: Any) -> str:
    if val is None:
        return "-"
    v = _safe_float(val, -1)
    if v < 0:
        return str(val)
    if v == 0:
        return "$0"
    if v < 0.0001:
        return f"${v:.10f}".rstrip("0").rstrip(".")
    if v < 1:
        return f"${v:.6f}"
    if v < 1000:
        return f"${v:.4f}"
    return f"${v:,.2f}"


# ============================================================
# GeckoTerminal helpers
# ============================================================

def _gt_pool_to_token(pool: dict, included: list[dict]) -> dict[str, Any]:
    attrs = pool.get("attributes", {})
    rels = pool.get("relationships", {})

    base_id = rels.get("base_token", {}).get("data", {}).get("id", "")
    quote_id = rels.get("quote_token", {}).get("data", {}).get("id", "")

    base_info = {"symbol": "?", "name": "", "address": "", "decimals": 18}
    quote_info = {"symbol": "?", "address": ""}
    for inc in included:
        if inc.get("type") == "token":
            inc_id = inc.get("id", "")
            inc_a = inc.get("attributes", {})
            if inc_id == base_id:
                base_info = {
                    "symbol": inc_a.get("symbol", "?"),
                    "name": inc_a.get("name", ""),
                    "address": inc_a.get("address", ""),
                    "decimals": int(inc_a.get("decimals") or 18),
                }
            elif inc_id == quote_id:
                quote_info = {
                    "symbol": inc_a.get("symbol", "?"),
                    "address": inc_a.get("address", ""),
                }

    price_usd = attrs.get("base_token_price_usd")
    liq = _safe_float(attrs.get("reserve_in_usd"))
    vol24 = _safe_float((attrs.get("volume_usd") or {}).get("h24"))
    mcap = _safe_float(attrs.get("market_cap_usd") or attrs.get("fdv_usd"))
    pct = attrs.get("price_change_percentage") or {}
    chg24 = _safe_float(pct.get("h24"))

    # Extract chain from pool id like "base_0x..." or from network
    pool_id = pool.get("id", "")
    chain = pool_id.split("_")[0] if "_" in pool_id else ""

    return {
        "chain": chain,
        "symbol": base_info["symbol"],
        "name": base_info["name"],
        "address": base_info["address"],
        "decimals": base_info["decimals"],
        "priceUsd": price_usd,
        "priceChange24h": chg24,
        "liquidityUsd": liq,
        "volume24h": vol24,
        "marketCap": mcap,
        "dexId": (rels.get("dex", {}).get("data", {}).get("id", "")),
        "pairAddress": attrs.get("address", ""),
        "quoteSymbol": quote_info["symbol"],
        "quoteAddress": quote_info["address"],
        "source": "geckoterminal",
    }


def _gt_search(query: str, chain: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    if chain:
        net_id = GT_NETWORK_IDS.get(chain)
        if not net_id:
            return []
        url = f"{GT_BASE}/search/pools?query={quote(query)}&network={net_id}&include=base_token,quote_token,dex"
    else:
        return []
    data = _fetch_json(url, headers=GT_HEADERS)
    included = data.get("included", [])
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool in data.get("data", []):
        r = _gt_pool_to_token(pool, included)
        key = f"{r['chain']}:{r['address']}"
        if key in seen or not r["address"]:
            continue
        seen.add(key)
        results.append(r)
    results.sort(key=lambda r: r["liquidityUsd"], reverse=True)
    return results[:limit]


def _gt_trending(chain: str, limit: int = 15) -> list[dict[str, Any]]:
    net_id = GT_NETWORK_IDS.get(chain)
    if not net_id:
        return []
    url = f"{GT_BASE}/networks/{net_id}/trending_pools?include=base_token,quote_token,dex"
    data = _fetch_json(url, headers=GT_HEADERS)
    included = data.get("included", [])
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool in data.get("data", []):
        r = _gt_pool_to_token(pool, included)
        key = f"{r['chain']}:{r['address']}"
        if key in seen or not r["address"]:
            continue
        seen.add(key)
        results.append(r)
    return results[:limit]


def _gt_top_pools(chain: str, limit: int = 20) -> list[dict[str, Any]]:
    net_id = GT_NETWORK_IDS.get(chain)
    if not net_id:
        return []
    url = f"{GT_BASE}/networks/{net_id}/pools?include=base_token,quote_token,dex&page=1"
    data = _fetch_json(url, headers=GT_HEADERS)
    included = data.get("included", [])
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool in data.get("data", []):
        r = _gt_pool_to_token(pool, included)
        key = f"{r['chain']}:{r['address']}"
        if key in seen or not r["address"]:
            continue
        seen.add(key)
        results.append(r)
    return results[:limit]


# ============================================================
# DexScreener helpers
# ============================================================

def _ds_search(query: str, chain: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    url = f"{DS_SEARCH}{quote(query)}"
    data = _fetch_json(url)

    seen: set[str] = set()
    results: list[dict[str, Any]] = []

    for pair in data.get("pairs", []):
        chain_id = pair.get("chainId", "")
        if chain and chain_id != chain:
            continue

        base = pair.get("baseToken", {})
        addr = base.get("address", "")
        key = f"{chain_id}:{addr}"
        if key in seen:
            continue
        seen.add(key)

        liq = _safe_float((pair.get("liquidity") or {}).get("usd"))
        vol24 = _safe_float((pair.get("volume") or {}).get("h24"))
        mcap = _safe_float(pair.get("marketCap") or pair.get("fdv"))
        price_usd = pair.get("priceUsd")
        price_change = (pair.get("priceChange") or {}).get("h24")

        results.append({
            "chain": chain_id,
            "symbol": base.get("symbol", ""),
            "name": base.get("name", ""),
            "address": addr,
            "decimals": None,
            "priceUsd": price_usd,
            "priceChange24h": _safe_float(price_change),
            "liquidityUsd": liq,
            "volume24h": vol24,
            "marketCap": mcap,
            "dexId": pair.get("dexId", ""),
            "source": "dexscreener",
        })

    results.sort(key=lambda r: r["liquidityUsd"], reverse=True)
    return results[:limit]


def _ds_lookup(chain: str, address: str) -> dict[str, Any] | None:
    url = f"{DS_TOKENS}/{chain}/{address}"
    data = _fetch_json(url)

    pairs = data if isinstance(data, list) else data.get("pairs", [])
    if not pairs:
        return None

    best = max(pairs, key=lambda p: _safe_float((p.get("liquidity") or {}).get("usd")))
    base = best.get("baseToken", {})
    quote_token = best.get("quoteToken", {})

    liq = _safe_float((best.get("liquidity") or {}).get("usd"))
    vol24 = _safe_float((best.get("volume") or {}).get("h24"))
    mcap = _safe_float(best.get("marketCap") or best.get("fdv"))
    price_usd = best.get("priceUsd")
    price_change = (best.get("priceChange") or {})

    return {
        "chain": best.get("chainId", chain),
        "symbol": base.get("symbol", ""),
        "name": base.get("name", ""),
        "address": base.get("address", ""),
        "priceUsd": price_usd,
        "priceChange": {k: _safe_float(v) for k, v in price_change.items()},
        "liquidityUsd": liq,
        "volume24h": vol24,
        "marketCap": mcap,
        "dexId": best.get("dexId", ""),
        "pairAddress": best.get("pairAddress", ""),
        "quoteSymbol": quote_token.get("symbol", ""),
        "quoteAddress": quote_token.get("address", ""),
        "source": "dexscreener",
    }


# ============================================================
# Unified API
# ============================================================

def search_tokens(query: str, chain: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Per-chain: GeckoTerminal first (no 30-pair limit), then DexScreener
    if chain:
        try:
            for r in _gt_search(query, chain, limit=limit):
                key = f"{r['chain']}:{r['address']}"
                if key not in seen:
                    seen.add(key)
                    results.append(r)
        except Exception:
            pass

        if len(results) < limit:
            try:
                for r in _ds_search(query, chain, limit=limit):
                    key = f"{r['chain']}:{r['address']}"
                    if key not in seen:
                        seen.add(key)
                        results.append(r)
            except Exception:
                pass
    else:
        # No chain: DexScreener global search only (GeckoTerminal requires network)
        try:
            results = _ds_search(query, chain, limit=limit)
        except Exception:
            pass

    results.sort(key=lambda r: r["liquidityUsd"], reverse=True)

    # Fill in decimals from catalog for tokens missing it
    for r in results:
        if r.get("decimals") is not None:
            continue
        chain_key = r.get("chain", "")
        addr = r.get("address", "").lower()
        cat = _TOKEN_CATALOG.get(chain_key, {})
        for tok in cat.values():
            if tok.get("address", "").lower() == addr:
                r["decimals"] = tok.get("decimals")
                break

    return results[:limit]


def lookup_token(chain: str, address: str) -> dict[str, Any] | None:
    try:
        return _ds_lookup(chain, address)
    except Exception:
        return None


def trending_tokens(chain: str, limit: int = 15) -> list[dict[str, Any]]:
    try:
        return _gt_trending(chain, limit)
    except Exception:
        return []


def top_pools(chain: str, limit: int = 20) -> list[dict[str, Any]]:
    try:
        return _gt_top_pools(chain, limit)
    except Exception:
        return []


# ============================================================
# Risk badge
# ============================================================

def _compute_risk_badge(liquidity_usd: float, volume_24h: float) -> str:
    if liquidity_usd >= 500_000 and volume_24h >= 100_000:
        return "LOW"
    if liquidity_usd >= 100_000 and volume_24h >= 10_000:
        return "MED"
    if liquidity_usd >= 10_000 or volume_24h >= 1_000:
        return "HIGH"
    return "EXT"


# ============================================================
# Output formatters
# ============================================================

def _print_search_table(results: list[dict[str, Any]]) -> None:
    print(f"{'Chain':<10} {'Symbol':<10} {'Price':>14} {'24h':>8} {'Risk':>5} {'Liquidity':>12} {'Volume24h':>12} {'MarketCap':>12}  Name")
    print("-" * 124)
    for r in results:
        price = _fmt_price(r.get("priceUsd"))
        liq = _safe_float(r.get("liquidityUsd"))
        vol = _safe_float(r.get("volume24h"))
        mcap = _safe_float(r.get("marketCap"))
        chg = _fmt_pct(_safe_float(r.get("priceChange24h")))
        risk = r.get("riskBadge", "")

        print(f"{r['chain']:<10} {r['symbol']:<10} {price:>14} {chg:>8} {risk:>5} {_fmt_usd(liq):>12} {_fmt_usd(vol):>12} {_fmt_usd(mcap):>12}  {r.get('name', '')[:30]}")

    print()
    for r in results:
        src = r.get("source", "")
        dec = r.get("decimals")
        dec_str = f" dec={dec}" if dec is not None else ""
        print(f"  {r['chain']}.{r['symbol']}: {r['address']}{dec_str}  [{src}]")


def _print_lookup(result: dict[str, Any]) -> None:
    print(f"Chain:      {result['chain']}")
    print(f"Symbol:     {result['symbol']}")
    print(f"Name:       {result['name']}")
    print(f"Address:    {result['address']}")
    print(f"Price:      {_fmt_price(result.get('priceUsd'))}")
    chg = result.get("priceChange", {})
    if chg:
        parts = []
        for period, label in [("m5", "5m"), ("h1", "1h"), ("h6", "6h"), ("h24", "24h")]:
            v = chg.get(period)
            if v is not None:
                parts.append(f"{label}:{_fmt_pct(v)}")
        if parts:
            print(f"Changes:    {'  '.join(parts)}")
    print(f"Liquidity:  {_fmt_usd(_safe_float(result.get('liquidityUsd')))}")
    print(f"Volume24h:  {_fmt_usd(_safe_float(result.get('volume24h')))}")
    print(f"MarketCap:  {_fmt_usd(_safe_float(result.get('marketCap')))}")
    print(f"DEX:        {result.get('dexId', '-')}")
    print(f"Pair:       {result['symbol']}/{result.get('quoteSymbol', '?')} ({result.get('pairAddress', '-')})")


def _print_pool_table(results: list[dict[str, Any]], title: str) -> None:
    print(f"  {title}")
    print()
    print(f"{'Symbol':<10} {'Price':>14} {'24h':>8} {'Liquidity':>12} {'Volume24h':>12} {'MarketCap':>12}  Name")
    print("-" * 106)
    for r in results:
        price = _fmt_price(r.get("priceUsd"))
        liq = _safe_float(r.get("liquidityUsd"))
        vol = _safe_float(r.get("volume24h"))
        mcap = _safe_float(r.get("marketCap"))
        chg = _fmt_pct(_safe_float(r.get("priceChange24h")))
        print(f"{r['symbol']:<10} {price:>14} {chg:>8} {_fmt_usd(liq):>12} {_fmt_usd(vol):>12} {_fmt_usd(mcap):>12}  {r.get('name', '')[:30]}")

    print()
    for r in results:
        print(f"  {r['symbol']}: {r['address']}")


# ============================================================
# CLI
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="搜索/查询链上代币（DexScreener + GeckoTerminal，无需 API key）")
    sub = parser.add_subparsers(dest="command")

    # search
    s = sub.add_parser("search", help="按关键词搜索代币（symbol / name）")
    s.add_argument("query", help="搜索关键词")
    s.add_argument("--chain", "-c", help="过滤链名，如 base / ethereum / arbitrum")
    s.add_argument("--limit", "-n", type=int, default=10, help="返回数量，默认 10")
    s.add_argument("--min-liq", type=float, default=0, help="最低流动性（USD），默认 0")
    s.add_argument("--risk-filter", choices=["LOW", "MED", "HIGH", "EXT"], help="按风险等级过滤结果")
    s.add_argument("--min-tvl", type=float, default=0, help="最低 TVL（USD）")
    s.add_argument("--min-volume", type=float, default=0, help="最低 24h 交易量（USD）")
    s.add_argument("--output", help="输出 JSON 文件路径")

    # lookup
    lk = sub.add_parser("lookup", help="按地址查代币详情")
    lk.add_argument("--chain", "-c", required=True, help="链名，如 base / ethereum")
    lk.add_argument("address", help="代币合约地址")
    lk.add_argument("--output", help="输出 JSON 文件路径")

    # trending
    tr = sub.add_parser("trending", help="查看链上热门代币（GeckoTerminal trending pools）")
    tr.add_argument("--chain", "-c", required=True, help="链名")
    tr.add_argument("--limit", "-n", type=int, default=15, help="返回数量，默认 15")
    tr.add_argument("--output", help="输出 JSON 文件路径")

    # top
    tp = sub.add_parser("top", help="查看链上交易量最高的代币（GeckoTerminal top pools）")
    tp.add_argument("--chain", "-c", required=True, help="链名")
    tp.add_argument("--limit", "-n", type=int, default=15, help="返回数量，默认 15")
    tp.add_argument("--output", help="输出 JSON 文件路径")

    args = parser.parse_args()

    try:
        if args.command == "lookup":
            result = lookup_token(args.chain, args.address)
            if not result:
                print(json.dumps({"error": f"token not found: {args.chain}/{args.address}"}, ensure_ascii=False, indent=2), file=sys.stderr)
                sys.exit(1)
            _print_lookup(result)
            # Auto-cache lookup results
            if result.get("address") and result.get("symbol") and result.get("chain"):
                try:
                    cache_token_from_search(
                        chain_key=result["chain"],
                        symbol=result["symbol"],
                        address=result["address"],
                        decimals=18,  # DexScreener doesn't return decimals
                    )
                except Exception:
                    pass
            output = {"action": "token_lookup", "result": result}
            if args.output:
                Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            dump_json(output)

        elif args.command == "trending":
            results = trending_tokens(args.chain, args.limit)
            if not results:
                print(json.dumps({"error": f"no trending tokens found on {args.chain}"}, ensure_ascii=False, indent=2), file=sys.stderr)
                sys.exit(1)
            _print_pool_table(results, f"Trending on {args.chain} (GeckoTerminal)")
            output = {"action": "token_trending", "chain": args.chain, "count": len(results), "results": results}
            if args.output:
                Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            dump_json(output)

        elif args.command == "top":
            results = top_pools(args.chain, args.limit)
            if not results:
                print(json.dumps({"error": f"no top pools found on {args.chain}"}, ensure_ascii=False, indent=2), file=sys.stderr)
                sys.exit(1)
            _print_pool_table(results, f"Top pools on {args.chain} (GeckoTerminal)")
            output = {"action": "token_top", "chain": args.chain, "count": len(results), "results": results}
            if args.output:
                Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            dump_json(output)

        elif args.command == "search":
            results = search_tokens(args.query, args.chain, limit=50)
            # Add risk badges
            for r in results:
                r["riskBadge"] = _compute_risk_badge(_safe_float(r.get("liquidityUsd")), _safe_float(r.get("volume24h")))
            if args.min_liq:
                results = [r for r in results if _safe_float(r.get("liquidityUsd")) >= args.min_liq]
            if args.min_tvl:
                results = [r for r in results if _safe_float(r.get("liquidityUsd")) >= args.min_tvl]
            if args.min_volume:
                results = [r for r in results if _safe_float(r.get("volume24h")) >= args.min_volume]
            if args.risk_filter:
                results = [r for r in results if r.get("riskBadge") == args.risk_filter]
            results = results[:args.limit]
            if not results:
                print(json.dumps({"error": f"no results for '{args.query}'"}, ensure_ascii=False, indent=2), file=sys.stderr)
                sys.exit(1)
            # Auto-cache search results for future resolve_token lookups
            for r in results:
                if r.get("address") and r.get("symbol") and r.get("chain"):
                    try:
                        cache_token_from_search(
                            chain_key=r["chain"],
                            symbol=r["symbol"],
                            address=r["address"],
                            decimals=r.get("decimals") or 18,
                            is_stable=False,
                        )
                    except Exception:
                        pass
            _print_search_table(results)
            output = {"action": "token_search", "query": args.query, "chain": args.chain, "count": len(results), "results": results}
            if args.output:
                Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            dump_json(output)
        else:
            parser.print_help()
            sys.exit(1)

    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
