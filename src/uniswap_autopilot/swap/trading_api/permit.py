#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


from uniswap_autopilot.common.common import dump_json, load_local_env
from uniswap_autopilot.swap.trading_api.swap import build_permit_handoff, load_quote_payload


def main() -> None:
    parser = argparse.ArgumentParser(description="从 quote 结果导出 permitData 签名包")
    parser.add_argument("--quote-file", required=True, help="trading_api_quote.py 输出的 JSON 文件路径")
    parser.add_argument("--output", required=True, help="导出的 permitData handoff JSON 路径")
    parser.add_argument("--typed-data-output", help="额外导出标准 EIP-712 typed data JSON 路径")
    args = parser.parse_args()

    try:
        load_local_env()
        quote_blob = load_quote_payload(args.quote_file)
        handoff = build_permit_handoff(raw_quote=quote_blob["rawQuote"], quote_file=args.quote_file)
        Path(args.output).write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if args.typed_data_output:
            Path(args.typed_data_output).write_text(
                json.dumps(handoff["typedData"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        dump_json(handoff)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
