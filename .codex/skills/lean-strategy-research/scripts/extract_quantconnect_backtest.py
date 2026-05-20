"""Extract summary fields from QuantConnect embedded backtest HTML.

This script is intentionally conservative. It helps collect provenance and
statistic fields, but agents should still open the source page before making
investment or implementation claims.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_WATCH_SYMBOLS = ("EWY", "FLKR", "KOSPI", "KOSDAQ")
KEY_STATS = (
    "Compounding Annual Return",
    "Net Profit",
    "Sharpe Ratio",
    "Sortino Ratio",
    "Drawdown",
    "Win Rate",
    "Loss Rate",
    "Total Orders",
    "Estimated Strategy Capacity",
    "Lowest Capacity Asset",
    "Portfolio Turnover",
)


def read_source(source: str) -> str:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(
            source,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 LEapsQuantEngine research helper "
                    "(contact: local operator)"
                )
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")

    return Path(source).read_text(encoding="utf-8", errors="replace")


def text_only(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def extract_stats(raw_html: str) -> dict[str, str]:
    pattern = re.compile(
        r'<div[^>]*class=["\'][^"\']*statistic-name[^"\']*["\'][^>]*>'
        r"(.*?)</div>\s*"
        r'<div[^>]*class=["\'][^"\']*statistic-value[^"\']*["\'][^>]*>'
        r"(.*?)</div>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    stats: dict[str, str] = {}
    for name, value in pattern.findall(raw_html):
        clean_name = text_only(name)
        clean_value = text_only(value)
        if clean_name:
            if clean_name == "Lowest Capacity Asset" and clean_value:
                clean_value = clean_value.split()[0]
            stats[clean_name] = clean_value
    return stats


def extract_generated_on(raw_html: str) -> str | None:
    match = re.search(r"<!--\s*Generated on\s+(.+?)\s*-->", raw_html, re.I)
    return match.group(1).strip() if match else None


def extract_algorithm_classes(raw_html: str) -> list[str]:
    decoded = html.unescape(raw_html)
    classes = re.findall(
        r"class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*QCAlgorithm\s*\)",
        decoded,
    )
    return sorted(set(classes))


def extract_symbol_mentions(raw_html: str, watch_symbols: tuple[str, ...]) -> dict[str, int]:
    decoded = html.unescape(raw_html).upper()
    mentions: dict[str, int] = {}
    for symbol in watch_symbols:
        count = len(re.findall(rf"\b{re.escape(symbol.upper())}\b", decoded))
        if count:
            mentions[symbol.upper()] = count
    return mentions


def summarize(source: str, raw_html: str, watch_symbols: tuple[str, ...]) -> dict[str, Any]:
    stats = extract_stats(raw_html)
    return {
        "source": source,
        "generated_on": extract_generated_on(raw_html),
        "qc_algorithm_classes": extract_algorithm_classes(raw_html),
        "symbol_mentions": extract_symbol_mentions(raw_html, watch_symbols),
        "key_stats": {name: stats[name] for name in KEY_STATS if name in stats},
        "all_stats": stats,
    }


def print_markdown(result: dict[str, Any]) -> None:
    print(f"Source: {result['source']}")
    print(f"Generated: {result.get('generated_on') or 'unknown'}")
    classes = ", ".join(result.get("qc_algorithm_classes") or []) or "unknown"
    print(f"QCAlgorithm classes: {classes}")
    mentions = result.get("symbol_mentions") or {}
    print(f"Watched symbols: {mentions or 'none detected'}")
    print()
    print("| Metric | Value |")
    print("| --- | --- |")
    for name, value in result.get("key_stats", {}).items():
        print(f"| {name} | {value} |")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract statistics from QuantConnect embedded backtest HTML."
    )
    parser.add_argument("sources", nargs="+", help="HTML file path or URL.")
    parser.add_argument(
        "--watch-symbol",
        action="append",
        default=[],
        help="Symbol or token to count in the page. May be repeated.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    watch_symbols = tuple(args.watch_symbol) or DEFAULT_WATCH_SYMBOLS
    results = [
        summarize(source, read_source(source), watch_symbols)
        for source in args.sources
    ]

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for index, result in enumerate(results):
            if index:
                print("\n---\n")
            print_markdown(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
