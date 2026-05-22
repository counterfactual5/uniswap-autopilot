"""Example: Analyze an LP position and calculate impermanent loss."""
from uniswap_autopilot.analytics.il_calculator import calculate_il
from uniswap_autopilot.analytics.range_suggest import suggest_ranges


def main():
    # 1. Calculate impermanent loss at various price ratios
    print("=== Impermanent Loss ===")
    for ratio in [1.1, 1.2, 1.5, 2.0, 3.0]:
        il = calculate_il(price_ratio=ratio)
        print(f"  Price ratio {ratio:.1f}x → IL: {il:.2%}")

    # 2. Suggest LP ranges
    print("\n=== Range Suggestions (ETH @ $3000, 20% vol) ===")
    ranges = suggest_ranges(current_price=3000, volatility_pct=20, strategy="balanced")
    for r in ranges:
        print(f"  ${r['lower']:.0f} — ${r['upper']:.0f}  ({r['label']})")


if __name__ == "__main__":
    main()
