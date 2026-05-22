"""Example: Get a swap quote and execute with confirmation."""
from uniswap_autopilot.swap.trading_api.quote import build_quote_payload, summarize_quote
from uniswap_autopilot.swap.trading_api.swap import build_swap_payload


def main():
    # 1. Get a quote
    quote = build_quote_payload(
        chain="base",
        input_token="ETH",
        output_token="USDC",
        amount="0.01",
    )
    print("=== Quote ===")
    print(summarize_quote(quote))

    # 2. Build swap transaction (dry-run by default)
    # swap_result = build_swap_payload(quote=quote, wallet="0x...")
    # print(swap_result)


if __name__ == "__main__":
    main()
