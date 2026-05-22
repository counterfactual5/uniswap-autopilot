"""Example: Get a swap quote via Uniswap Trading API."""
from uniswap_autopilot.swap.trading_api.quote import build_quote_payload, summarize_quote


def main():
    # Get a swap quote: ETH → USDC on Base
    quote = build_quote_payload(
        wallet="0x0000000000000000000000000000000000000000",  # read-only, any address works for quote
        chain_id=8453,                                         # Base
        api_token_in="ETH",
        api_token_out="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC on Base
        base_amount="0.01",
        swap_type="EXACT_INPUT",
        slippage="0.5",
        routing_preference="CLASSIC",
    )
    print("=== Quote ===")
    print(summarize_quote(quote))


if __name__ == "__main__":
    main()
