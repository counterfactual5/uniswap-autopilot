"""Example: Analyze an LP position and calculate impermanent loss."""
from uniswap_autopilot.analytics.il_calculator import calculate_il
from uniswap_autopilot.analytics.range_suggest import suggest_ranges


def main():
    # 1. Calculate impermanent loss for a V3 ETH-USDC position
    print("=== Impermanent Loss ===")
    # ETH-USDC pool, 0.3% fee tier, full-range position, entry @ $3000, now @ $3300
    il = calculate_il(
        price_entry=3000.0,
        price_current=3300.0,
        tick_lower=-887220,
        tick_upper=887220,
        decimals0=18,
        decimals1=6,
        liquidity=1000000000000,
    )
    print(f"  Price change: +{il['priceChangePct']:.1f}%")
    print(f"  IL: {il['ilPct']:.2f}%")
    print(f"  In range: {il['inRange']}")

    # 2. Suggest LP ranges for ETH-USDC on Ethereum
    print("\n=== Range Suggestions ===")
    ranges = suggest_ranges(
        chain_name="ethereum",
        token_a="ETH",
        token_b="USDC",
        fee_tier=3000,
    )
    for r in ranges:
        print(f"  ${float(r['lower']):,.0f} — ${float(r['upper']):,.0f}  ({r.get('profile', '?')})")


if __name__ == "__main__":
    main()
