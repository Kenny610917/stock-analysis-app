#!/usr/bin/env python3
"""Bollinger Band + volume signal checker.

Input modes:
  1. Read a local CSV file.
  2. Fetch online daily bars with --symbol.

Input CSV columns:
  Required: date, close, volume
  Optional: open, high, low

Rows should be daily bars. If the date column is parseable and appears newest
first, the script reverses it automatically before calculating indicators.

This tool is for research and education only. It is not investment advice.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen


HEADER_ALIASES = {
    "date": (
        "date",
        "time",
        "timestamp",
        "trade_date",
        "\u65e5\u671f",
    ),
    "open": (
        "open",
        "o",
        "\u958b\u76e4",
        "\u958b\u76e4\u50f9",
    ),
    "high": (
        "high",
        "h",
        "\u6700\u9ad8",
        "\u6700\u9ad8\u50f9",
    ),
    "low": (
        "low",
        "l",
        "\u6700\u4f4e",
        "\u6700\u4f4e\u50f9",
    ),
    "close": (
        "close",
        "c",
        "adj_close",
        "adj close",
        "\u6536\u76e4",
        "\u6536\u76e4\u50f9",
        "\u6700\u5f8c",
    ),
    "volume": (
        "volume",
        "vol",
        "\u6210\u4ea4\u91cf",
        "\u6210\u4ea4\u80a1\u6578",
    ),
}


DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
)


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class AnalysisRow:
    bar: Bar
    middle: float | None = None
    upper: float | None = None
    lower: float | None = None
    bandwidth_pct: float | None = None
    volume_ma: float | None = None
    volume_ratio: float | None = None
    action: str = "NO_DATA"
    signal: str = "INSUFFICIENT_HISTORY"
    reason: str = "Need more rows to calculate indicators."


def normalize_header(value: str) -> str:
    return " ".join(value.strip().lower().replace("-", "_").split())


def parse_number(value: str, column: str) -> float:
    if value is None:
        raise ValueError(f"Missing numeric value for {column}")

    cleaned = str(value).strip().replace(",", "")
    if cleaned in {"", "-", "null", "None", "nan", "NaN"}:
        raise ValueError(f"Missing numeric value for {column}")

    if cleaned.endswith("%"):
        return float(cleaned[:-1]) / 100.0

    return float(cleaned)


def parse_date(value: str) -> datetime | None:
    text = str(value).strip()
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            continue
    return None


def find_column(headers: Iterable[str], logical_name: str) -> str | None:
    normalized_to_original = {normalize_header(header): header for header in headers}
    for alias in HEADER_ALIASES[logical_name]:
        normalized_alias = normalize_header(alias)
        if normalized_alias in normalized_to_original:
            return normalized_to_original[normalized_alias]
    return None


def read_bars(csv_path: Path) -> list[Bar]:
    with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row.")

        columns = {
            name: find_column(reader.fieldnames, name)
            for name in ("date", "open", "high", "low", "close", "volume")
        }

        missing = [name for name in ("date", "close", "volume") if columns[name] is None]
        if missing:
            raise ValueError(
                "Missing required CSV columns: "
                + ", ".join(missing)
                + ". Required aliases are date, close, volume."
            )

        bars: list[Bar] = []
        for row_number, row in enumerate(reader, start=2):
            try:
                close = parse_number(row[columns["close"]], "close")
                volume = parse_number(row[columns["volume"]], "volume")
                open_price = (
                    parse_number(row[columns["open"]], "open")
                    if columns["open"] is not None
                    else close
                )
                high = (
                    parse_number(row[columns["high"]], "high")
                    if columns["high"] is not None
                    else max(open_price, close)
                )
                low = (
                    parse_number(row[columns["low"]], "low")
                    if columns["low"] is not None
                    else min(open_price, close)
                )
                bars.append(
                    Bar(
                        date=str(row[columns["date"]]).strip() or str(row_number),
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Could not parse row {row_number}: {exc}") from exc

    if len(bars) < 2:
        raise ValueError("CSV must contain at least two data rows.")

    first_date = parse_date(bars[0].date)
    last_date = parse_date(bars[-1].date)
    if first_date and last_date and first_date > last_date:
        bars.reverse()

    return bars


def fetch_yahoo_bars(
    symbol: str,
    data_range: str,
    interval: str,
    timeout: int = 20,
) -> list[Bar]:
    query = urlencode(
        {
            "range": data_range,
            "interval": interval,
            "includePrePost": "false",
            "events": "div,splits",
        }
    )
    encoded_symbol = quote(symbol.strip().upper(), safe="")
    url = f"{YAHOO_CHART_URL.format(symbol=encoded_symbol)}?{query}"
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            )
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ValueError(f"Yahoo Finance returned HTTP {exc.code} for {symbol}.") from exc
    except URLError as exc:
        raise ValueError(f"Could not reach Yahoo Finance: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ValueError("Timed out while fetching Yahoo Finance data.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Yahoo Finance returned invalid JSON.") from exc

    chart = payload.get("chart", {})
    errors = chart.get("error")
    if errors:
        description = errors.get("description") or errors.get("code") or errors
        raise ValueError(f"Yahoo Finance error for {symbol}: {description}")

    results = chart.get("result") or []
    if not results:
        raise ValueError(f"No Yahoo Finance data returned for {symbol}.")

    result = results[0]
    meta = result.get("meta") or {}
    regular_market_price = meta.get("regularMarketPrice")
    timestamps = result.get("timestamp") or []
    quote_data = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote_data.get("open") or []
    highs = quote_data.get("high") or []
    lows = quote_data.get("low") or []
    closes = quote_data.get("close") or []
    volumes = quote_data.get("volume") or []

    bars: list[Bar] = []
    for index, timestamp in enumerate(timestamps):
        try:
            open_price = opens[index]
            high = highs[index]
            low = lows[index]
            close = closes[index]
            volume = volumes[index]
        except IndexError:
            continue

        if close is None and index == len(timestamps) - 1:
            close = regular_market_price

        if None in (open_price, high, low, close, volume):
            continue

        bars.append(
            Bar(
                date=datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d"),
                open=float(open_price),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume),
            )
        )

    if len(bars) < 2:
        raise ValueError(f"Not enough usable Yahoo Finance bars for {symbol}.")

    return bars


def write_bars(bars: list[Bar], output_path: Path) -> None:
    fieldnames = ["date", "open", "high", "low", "close", "volume"]
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for bar in bars:
            writer.writerow(
                {
                    "date": bar.date,
                    "open": maybe_round(bar.open),
                    "high": maybe_round(bar.high),
                    "low": maybe_round(bar.low),
                    "close": maybe_round(bar.close),
                    "volume": maybe_round(bar.volume, 0),
                }
            )


def rolling_mean(values: list[float]) -> float:
    return sum(values) / len(values)


def sample_std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = rolling_mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def maybe_round(value: float | None, digits: int = 4) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def analyze(
    bars: list[Bar],
    band_period: int,
    std_multiplier: float,
    volume_period: int,
    volume_multiplier: float,
) -> list[AnalysisRow]:
    if band_period < 2:
        raise ValueError("band_period must be at least 2.")
    if volume_period < 2:
        raise ValueError("volume_period must be at least 2.")

    rows = [AnalysisRow(bar=bar) for bar in bars]
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    warmup = max(band_period, volume_period)

    for index, row in enumerate(rows):
        if index + 1 < warmup:
            continue

        close_window = closes[index - band_period + 1 : index + 1]
        volume_window = volumes[index - volume_period + 1 : index + 1]
        middle = rolling_mean(close_window)
        deviation = sample_std(close_window)
        upper = middle + std_multiplier * deviation
        lower = middle - std_multiplier * deviation
        volume_ma = rolling_mean(volume_window)
        volume_ratio = row.bar.volume / volume_ma if volume_ma else None

        row.middle = middle
        row.upper = upper
        row.lower = lower
        row.volume_ma = volume_ma
        row.volume_ratio = volume_ratio
        row.bandwidth_pct = ((upper - lower) / middle * 100.0) if middle else None

        previous = rows[index - 1] if index > 0 else None
        if previous is None or previous.upper is None or previous.lower is None:
            row.action = "HOLD"
            row.signal = "READY"
            row.reason = "Indicators are available; no prior band state yet."
            continue

        classify_signal(row, previous, volume_multiplier)

    return rows


def classify_signal(
    row: AnalysisRow,
    previous: AnalysisRow,
    volume_multiplier: float,
) -> None:
    bar = row.bar
    prev_bar = previous.bar
    assert row.upper is not None
    assert row.middle is not None
    assert row.lower is not None
    assert previous.upper is not None
    assert previous.middle is not None
    assert previous.lower is not None

    volume_ratio = row.volume_ratio or 0.0
    volume_spike = volume_ratio >= volume_multiplier
    close_up = bar.close > prev_bar.close and bar.close >= bar.open
    close_down = bar.close < prev_bar.close and bar.close <= bar.open

    cross_up_lower = prev_bar.close < previous.lower and bar.close >= row.lower
    cross_down_upper = prev_bar.close > previous.upper and bar.close <= row.upper
    break_down_middle = prev_bar.close >= previous.middle and bar.close < row.middle

    if bar.close < row.lower and volume_spike and close_down:
        row.action = "SELL"
        row.signal = "BEARISH_LOWER_BAND_BREAKDOWN"
        row.reason = (
            "Close broke below the lower band with elevated volume; downside "
            "momentum is stronger than a normal oversold touch."
        )
    elif cross_up_lower and volume_spike and close_up:
        row.action = "BUY"
        row.signal = "LOWER_BAND_REBOUND"
        row.reason = (
            "Price recovered above the lower band while volume expanded; this "
            "is a mean-reversion buy setup."
        )
    elif bar.close > row.upper and volume_spike and close_up:
        row.action = "BUY"
        row.signal = "UPPER_BAND_VOLUME_BREAKOUT"
        row.reason = (
            "Close pushed above the upper band with elevated volume; this is a "
            "trend-breakout buy setup, not a low-risk pullback."
        )
    elif cross_down_upper and volume_spike and close_down:
        row.action = "SELL"
        row.signal = "UPPER_BAND_REVERSAL"
        row.reason = (
            "Price fell back inside the upper band on elevated volume; upside "
            "extension is losing control."
        )
    elif break_down_middle and volume_spike:
        row.action = "SELL"
        row.signal = "MIDDLE_BAND_VOLUME_BREAKDOWN"
        row.reason = (
            "Close lost the middle band with elevated volume; reduce risk or "
            "exit weak positions."
        )
    elif bar.close <= row.lower:
        row.action = "WAIT_CONFIRMATION"
        row.signal = "LOWER_BAND_TOUCH"
        row.reason = (
            "Price is near or below the lower band, but volume/price "
            "confirmation is not strong enough for a buy signal."
        )
    elif bar.close >= row.upper:
        row.action = "WATCH"
        row.signal = "UPPER_BAND_EXTENSION"
        row.reason = (
            "Price is near or above the upper band; wait for either breakout "
            "confirmation or reversal confirmation."
        )
    else:
        row.action = "HOLD"
        row.signal = "NO_EDGE"
        row.reason = "Price and volume do not meet a buy or sell rule."


def write_output(rows: list[AnalysisRow], output_path: Path) -> None:
    fieldnames = [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "bb_middle",
        "bb_upper",
        "bb_lower",
        "bb_bandwidth_pct",
        "volume_ma",
        "volume_ratio",
        "action",
        "signal",
        "reason",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            bar = row.bar
            writer.writerow(
                {
                    "date": bar.date,
                    "open": maybe_round(bar.open),
                    "high": maybe_round(bar.high),
                    "low": maybe_round(bar.low),
                    "close": maybe_round(bar.close),
                    "volume": maybe_round(bar.volume, 0),
                    "bb_middle": maybe_round(row.middle),
                    "bb_upper": maybe_round(row.upper),
                    "bb_lower": maybe_round(row.lower),
                    "bb_bandwidth_pct": maybe_round(row.bandwidth_pct, 2),
                    "volume_ma": maybe_round(row.volume_ma, 0),
                    "volume_ratio": maybe_round(row.volume_ratio, 2),
                    "action": row.action,
                    "signal": row.signal,
                    "reason": row.reason,
                }
            )


def print_latest(rows: list[AnalysisRow], last_count: int) -> None:
    selected = rows[-last_count:]
    for row in selected:
        bar = row.bar
        print(f"Date: {bar.date}")
        print(f"Close: {bar.close:.4f} | Volume: {bar.volume:.0f}")
        if row.middle is None:
            print(f"Action: {row.action} | Signal: {row.signal}")
            print(f"Reason: {row.reason}")
            print()
            continue

        print(
            "Bollinger: "
            f"lower={row.lower:.4f}, middle={row.middle:.4f}, upper={row.upper:.4f}"
        )
        print(
            "Volume: "
            f"ma={row.volume_ma:.0f}, ratio={row.volume_ratio:.2f}x"
        )
        print(f"Action: {row.action} | Signal: {row.signal}")
        print(f"Reason: {row.reason}")
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze stock data with Bollinger Bands and volume. Use either a "
            "local CSV path or --symbol for online Yahoo Finance data."
        )
    )
    parser.add_argument(
        "csv_path",
        type=Path,
        nargs="?",
        help="Input CSV with date, close, volume columns.",
    )
    parser.add_argument(
        "-s",
        "--symbol",
        help=(
            "Fetch online bars from Yahoo Finance, e.g. AAPL, NVDA, 2330.TW, "
            "0700.HK."
        ),
    )
    parser.add_argument(
        "--range",
        dest="data_range",
        default="6mo",
        help="Yahoo Finance range when using --symbol. Default: 6mo.",
    )
    parser.add_argument(
        "--interval",
        default="1d",
        help="Yahoo Finance interval when using --symbol. Default: 1d.",
    )
    parser.add_argument(
        "--save-prices",
        type=Path,
        help="Optional path to save fetched online OHLCV bars as CSV.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Optional output CSV path with indicators and signals.",
    )
    parser.add_argument(
        "--band-period",
        type=int,
        default=20,
        help="Rolling period for Bollinger Bands. Default: 20.",
    )
    parser.add_argument(
        "--std-multiplier",
        type=float,
        default=2.0,
        help="Standard deviation multiplier for bands. Default: 2.0.",
    )
    parser.add_argument(
        "--volume-period",
        type=int,
        default=20,
        help="Rolling period for average volume. Default: 20.",
    )
    parser.add_argument(
        "--volume-multiplier",
        type=float,
        default=1.5,
        help="Volume spike threshold versus volume MA. Default: 1.5.",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=1,
        help="How many latest rows to print. Default: 1.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.symbol:
            bars = fetch_yahoo_bars(
                symbol=args.symbol,
                data_range=args.data_range,
                interval=args.interval,
            )
            print(
                f"Fetched {len(bars)} bars for {args.symbol.upper()} "
                f"from Yahoo Finance."
            )
            if args.save_prices:
                write_bars(bars, args.save_prices)
                print(f"Wrote fetched price CSV: {args.save_prices}")
        elif args.csv_path:
            bars = read_bars(args.csv_path)
        else:
            raise ValueError("Provide either a CSV path or --symbol.")

        rows = analyze(
            bars=bars,
            band_period=args.band_period,
            std_multiplier=args.std_multiplier,
            volume_period=args.volume_period,
            volume_multiplier=args.volume_multiplier,
        )

        if args.output:
            write_output(rows, args.output)
            print(f"Wrote analysis CSV: {args.output}")

        print_latest(rows, max(1, args.last))
        print("Risk note: this is a rules-based technical signal, not investment advice.")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI should show a concise failure.
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
