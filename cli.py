"""Command-line interface for ecommerce-jsonld-price-extractor-cli-lite."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from extractor import (
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    ExtractionResult,
    extract_from_target,
)

__version__ = "1.0.0"


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def format_table(results: Sequence[ExtractionResult]) -> str:
    """Render extraction results as a clean aligned text table."""
    headers = ["Product Name", "Currency", "Price", "Availability", "In Stock", "Extractor", "Source"]
    rows: List[List[str]] = []

    for r in results:
        if r.error:
            rows.append(
                [
                    _truncate(f"ERROR: {r.error}", 40),
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                    _truncate(r.source, 42),
                ]
            )
            continue

        in_stock_str = "yes" if r.in_stock is True else ("no" if r.in_stock is False else "unknown")
        rows.append(
            [
                _truncate(r.name or "-", 40),
                r.currency or "-",
                r.price_str() or "-",
                r.availability,
                in_stock_str,
                r.extractor_source or "-",
                _truncate(r.source, 42),
            ]
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def _fmt_row(cols: Sequence[str]) -> str:
        return " | ".join(col.ljust(widths[i]) for i, col in enumerate(cols))

    separator = "-+-".join("-" * w for w in widths)
    lines = [_fmt_row(headers), separator]
    lines.extend(_fmt_row(row) for row in rows)
    return "\n".join(lines)


def format_json(results: Sequence[ExtractionResult], compact: bool = False) -> str:
    """Render extraction results as clean structured JSON."""
    payload = [r.to_dict() for r in results]
    if compact:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _load_targets_from_file(path_arg: str) -> List[str]:
    if path_arg == "-":
        lines = sys.stdin.read().splitlines()
    else:
        lines = Path(path_arg).read_text(encoding="utf-8").splitlines()
    targets: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        targets.append(stripped)
    return targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ecommerce-price-extractor",
        description=(
            "Extract structured product name, currency, price, and stock status from "
            "local HTML files or URLs (JSON-LD -> OpenGraph -> Microdata -> Regex fallback)."
        ),
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="One or more HTML file paths or HTTP/HTTPS URLs to parse.",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="input_file",
        help="Read HTML file paths or URLs (one per line, # comments ignored, '-' for stdin).",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format: 'table' (default) or 'json'.",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit single-line compact JSON when --format json is selected.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Write formatted output to a UTF-8 file instead of stdout.",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP fetch timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="Custom HTTP User-Agent header for URL fetches.",
    )
    parser.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="Exit with code 1 if any target fails to load or yields no price.",
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")

    targets: List[str] = list(args.targets)
    if args.input_file:
        try:
            targets.extend(_load_targets_from_file(args.input_file))
        except OSError as exc:
            print(f"Error reading input list '{args.input_file}': {exc}", file=sys.stderr)
            return 2

    if not targets:
        parser.error("Provide at least one HTML file path or URL (positional or via --file).")

    results = [
        extract_from_target(t, timeout=args.timeout, user_agent=args.user_agent)
        for t in targets
    ]

    rendered = (
        format_json(results, compact=args.compact)
        if args.format == "json"
        else format_table(results)
    )

    if args.output:
        try:
            Path(args.output).write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Error writing output file '{args.output}': {exc}", file=sys.stderr)
            return 2
    else:
        print(rendered)

    if args.fail_on_missing:
        if any(r.error is not None or r.price is None for r in results):
            return 1
    elif any(r.error is not None for r in results):
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
