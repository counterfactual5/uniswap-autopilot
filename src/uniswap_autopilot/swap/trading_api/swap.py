#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


from uniswap_autopilot.common.common import dump_json, load_local_env, post_json, require_api_key

UNISWAPX_ROUTINGS = {"DUTCH_V2", "DUTCH_V3", "PRIORITY"}
HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
DOMAIN_FIELD_TYPES = {
    "name": "string",
    "version": "string",
    "chainId": "uint256",
    "verifyingContract": "address",
    "salt": "bytes32",
}


def load_quote_payload(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_quote = data.get("rawQuote")
    if not raw_quote:
        raise ValueError("quote file must contain rawQuote from trading_api_quote.py")
    quote = raw_quote.get("quote")
    if not quote:
        raise ValueError("rawQuote.quote is missing")
    return data


def is_uniswapx_route(raw_quote: dict[str, Any]) -> bool:
    return str(raw_quote.get("routing") or "") in UNISWAPX_ROUTINGS


def infer_primary_type(types: dict[str, Any]) -> str:
    if not types:
        raise ValueError("permitData.types is missing")

    referenced: set[str] = set()
    for fields in types.values():
        for field in fields or []:
            field_type = field.get("type")
            if isinstance(field_type, str) and field_type in types:
                referenced.add(field_type)

    candidates = [type_name for type_name in types if type_name not in referenced]
    if len(candidates) == 1:
        return candidates[0]
    if "PermitSingle" in types:
        return "PermitSingle"
    raise ValueError("could not infer primaryType from permitData.types")


def build_eip712_domain_types(domain: dict[str, Any]) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    for name in domain:
        field_type = DOMAIN_FIELD_TYPES.get(name)
        if not field_type:
            raise ValueError(f"unsupported EIP-712 domain field: {name}")
        fields.append({"name": name, "type": field_type})
    return fields


def normalize_permit_typed_data(permit_data: dict[str, Any]) -> dict[str, Any]:
    domain = permit_data.get("domain")
    types = permit_data.get("types")
    values = permit_data.get("values")
    if not isinstance(domain, dict) or not isinstance(types, dict) or not isinstance(values, dict):
        raise ValueError("permitData must contain domain, types, and values")

    normalized_types = {type_name: fields for type_name, fields in types.items()}
    if "EIP712Domain" not in normalized_types:
        normalized_types["EIP712Domain"] = build_eip712_domain_types(domain)

    return {
        "types": normalized_types,
        "primaryType": infer_primary_type(types),
        "domain": domain,
        "message": values,
    }


def validate_signature(signature: str) -> str:
    cleaned = signature.strip()
    if not HEX_RE.fullmatch(cleaned):
        raise ValueError("signature must be a 0x-prefixed hex string")
    return cleaned


def load_signature_value(signature: str | None = None, signature_file: str | None = None) -> str | None:
    if signature and signature_file:
        raise ValueError("use only one of --signature or --signature-file")
    if signature:
        return validate_signature(signature)
    if not signature_file:
        return None

    raw = Path(signature_file).read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("signature file is empty")
    if raw.startswith("{"):
        blob = json.loads(raw)
        value = blob.get("signature")
        if not isinstance(value, str):
            raise ValueError("signature json file must contain a string 'signature' field")
        return validate_signature(value)
    return validate_signature(raw)


def build_permit_handoff(
    raw_quote: dict[str, Any],
    quote_file: str | None = None,
) -> dict[str, Any]:
    permit_data = raw_quote.get("permitData")
    if not permit_data:
        raise ValueError("quote does not contain permitData")

    routing = str(raw_quote.get("routing") or "UNKNOWN")
    uniswapx = is_uniswapx_route(raw_quote)
    typed_data = normalize_permit_typed_data(permit_data)
    handoff = {
        "routing": routing,
        "quoteFile": quote_file,
        "signatureRule": {
            "signPermitDataLocally": True,
            "sendSignatureToSwap": True,
            "sendPermitDataToSwap": not uniswapx,
        },
        "permitData": permit_data,
        "typedData": typed_data,
        "instructions": [
            "Use typedData as the canonical EIP-712 JSON for wallet signing.",
            "Save the resulting 0x signature into a text file or a JSON file with a 'signature' field.",
            "Pass that file to swap_dry_run.py via --signature-file to continue /swap.",
        ],
    }
    return handoff


def build_swap_payload(
    raw_quote: dict[str, Any],
    signature: str | None = None,
    simulate_transaction: bool = False,
    refresh_gas_price: bool = False,
) -> dict[str, Any]:
    quote = raw_quote.get("quote")
    if not quote:
        raise ValueError("rawQuote.quote is missing")

    permit_data = raw_quote.get("permitData")
    if permit_data and not signature:
        raise ValueError("quote 返回了 permitData，调用 /swap 前必须提供 --signature")
    if signature and not permit_data:
        raise ValueError("quote 未返回 permitData，不应传入 --signature")

    payload: dict[str, Any] = {
        "quote": quote,
        "refreshGasPrice": refresh_gas_price,
        "simulateTransaction": simulate_transaction,
    }
    if permit_data:
        payload["signature"] = signature
        if not is_uniswapx_route(raw_quote):
            payload["permitData"] = permit_data
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="基于 quote 结果调用 /swap 做 dry-run")
    parser.add_argument("--quote-file", required=True, help="trading_api_quote.py 输出的 JSON 文件路径")
    parser.add_argument("--signature", help="当 quote 返回 permitData 时，传入签名")
    parser.add_argument("--signature-file", help="从文件读取签名；支持纯文本 0x... 或 JSON {'signature': '0x...'}")
    parser.add_argument("--permit-output", help="仅导出 permitData handoff JSON，不调用 /swap")
    parser.add_argument("--typed-data-output", help="导出标准 EIP-712 typed data JSON，便于 cast/钱包签名")
    parser.add_argument(
        "--simulate-transaction",
        action="store_true",
        help="让 /swap 在服务端做 simulateTransaction",
    )
    parser.add_argument(
        "--refresh-gas-price",
        action="store_true",
        help="让 /swap 重新抓 gas price",
    )
    parser.add_argument(
        "--request-only",
        action="store_true",
        help="只打印将发送的 /swap body，不实际调用",
    )
    parser.add_argument("--output", help="把完整 JSON 输出写入文件")
    args = parser.parse_args()

    try:
        load_local_env()
        quote_blob = load_quote_payload(args.quote_file)
        raw_quote = quote_blob["rawQuote"]
        if args.permit_output or args.typed_data_output:
            handoff = build_permit_handoff(raw_quote=raw_quote, quote_file=args.quote_file)
            if args.permit_output:
                Path(args.permit_output).write_text(
                    json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            if args.typed_data_output:
                Path(args.typed_data_output).write_text(
                    json.dumps(handoff["typedData"], ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            dump_json(handoff if args.permit_output else handoff["typedData"])
            return

        signature = load_signature_value(args.signature, args.signature_file)
        payload = build_swap_payload(
            raw_quote=raw_quote,
            signature=signature,
            simulate_transaction=args.simulate_transaction,
            refresh_gas_price=args.refresh_gas_price,
        )

        response: dict[str, Any] = {
            "action": "swap_dry_run",
            "requestOnly": args.request_only,
            "requestPayload": payload,
        }

        if not args.request_only:
            api_key = require_api_key()
            response["swapResponse"] = post_json("swap", payload, api_key)

        if args.output:
            Path(args.output).write_text(
                json.dumps(response, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        dump_json(response)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
