#!/usr/bin/env python3
"""Local web app for stock technical analysis."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
TOOL_PATH = ROOT_DIR / "stock-analysis-skill-main" / "tools" / "bollinger_volume_strategy.py"
RISK_NOTE = "本工具只提供規則型技術訊號研究，重點是把入場、停損、出場條件事先規則化；仍需搭配基本面與風險承受度，不構成投資建議。"
SYMBOL_RE = re.compile(r"^[A-Za-z0-9.^=_-]{1,24}$")
DEFAULT_SCREEN_SYMBOLS = (
    "NVDA,AAPL,MSFT,AMZN,META,GOOGL,TSLA,AVGO,AMD,SMCI,PLTR,NFLX,"
    "ORCL,CRM,COST,TSM,ASML,QQQ,SPY"
)
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
TWSE_LISTED_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_OTC_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
UNIVERSE_CACHE: dict[str, dict[str, object]] = {
    "us_all": {"loaded_at": None, "symbols": []},
    "tw_listed": {"loaded_at": None, "symbols": []},
    "tw_otc": {"loaded_at": None, "symbols": []},
    "tw_all": {"loaded_at": None, "symbols": []},
}


ACTION_TEXT = {
    "BUY": "買進",
    "SELL": "賣出",
    "HOLD": "觀望",
    "WATCH": "觀察",
    "WAIT_CONFIRMATION": "等待確認",
    "NO_DATA": "資料不足",
}

BOLLINGER_SIGNAL_TEXT = {
    "BEARISH_LOWER_BAND_BREAKDOWN": "跌破下軌",
    "LOWER_BAND_REBOUND": "下軌反彈",
    "UPPER_BAND_VOLUME_BREAKOUT": "上軌放量突破",
    "UPPER_BAND_REVERSAL": "上軌反轉",
    "MIDDLE_BAND_VOLUME_BREAKDOWN": "跌破中線",
    "LOWER_BAND_TOUCH": "觸及下軌",
    "UPPER_BAND_EXTENSION": "上軌延伸",
    "NO_EDGE": "無明確優勢",
    "READY": "指標就緒",
    "INSUFFICIENT_HISTORY": "資料不足",
}

BOLLINGER_REASON_TEXT = {
    "BEARISH_LOWER_BAND_BREAKDOWN": "收盤跌破布林下軌且量能放大，下跌動能強於一般超跌觸及。",
    "LOWER_BAND_REBOUND": "價格重新站回下軌且量能擴張，形成均值回歸買進型態。",
    "UPPER_BAND_VOLUME_BREAKOUT": "收盤突破上軌且量能擴張，屬於趨勢突破型買進訊號。",
    "UPPER_BAND_REVERSAL": "價格從上軌外跌回區間內且量能放大，上攻延伸力道轉弱。",
    "MIDDLE_BAND_VOLUME_BREAKDOWN": "收盤跌破布林中線且量能放大，應降低風險或退出弱勢部位。",
    "LOWER_BAND_TOUCH": "價格接近或跌破布林下軌，但量價確認不足，先等待反彈確認。",
    "UPPER_BAND_EXTENSION": "價格接近或突破上軌，等待突破延續或反轉確認。",
    "NO_EDGE": "價格與量能未符合買進或賣出規則。",
    "READY": "指標已可用，但還沒有前一日布林狀態可比較。",
    "INSUFFICIENT_HISTORY": "資料筆數不足，暫時無法計算指標。",
}


def load_strategy_module():
    spec = importlib.util.spec_from_file_location("bollinger_volume_strategy", TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load strategy tool from {TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STRATEGY = load_strategy_module()


def query_one(params: dict[str, list[str]], name: str, default: str) -> str:
    value = params.get(name, [default])[0].strip()
    return value or default


def clamp_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def clamp_float(value: str, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except ValueError:
        return default
    return max(minimum, min(maximum, parsed))


def maybe_float(value) -> float | None:
    if value is None:
        return None
    return float(value)


def ema(previous: float | None, value: float, period: int) -> float:
    if previous is None:
        return value
    alpha = 2.0 / (period + 1.0)
    return previous + alpha * (value - previous)


def translate_bollinger(row) -> dict[str, str]:
    return {
        "actionText": ACTION_TEXT.get(row.action, row.action),
        "signalText": BOLLINGER_SIGNAL_TEXT.get(row.signal, row.signal),
        "reasonText": BOLLINGER_REASON_TEXT.get(row.signal, row.reason),
    }


def pct_text(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "無前日可比"
    return f"{value:+.2f}%"


def calculate_history_context(
    bars: list[object],
    index: int,
    requirements: dict[str, int],
) -> dict[str, object]:
    bar = bars[index]
    available_bars = index + 1
    required_bars = max(requirements.values()) if requirements else 1
    prev_close = bars[index - 1].close if index > 0 else None
    price_change_pct = (
        (bar.close - prev_close) / prev_close * 100.0
        if isinstance(prev_close, (int, float)) and prev_close
        else None
    )
    intraday_range_pct = (
        (bar.high - bar.low) / bar.close * 100.0
        if isinstance(bar.close, (int, float)) and bar.close
        else None
    )
    open_to_close_pct = (
        (bar.close - bar.open) / bar.open * 100.0
        if isinstance(bar.open, (int, float)) and bar.open
        else None
    )
    turnover = bar.close * bar.volume
    short_history = available_bars < required_bars
    requirement_text = (
        f"BOLL/均量 {requirements['bollinger_volume']} 筆、"
        f"KD {requirements['kd']} 筆、RSI {requirements['rsi']} 筆、"
        f"MACD {requirements['macd']} 筆、箱型/道氏 {requirements['structure']} 筆"
    )

    return {
        "availableBars": available_bars,
        "requiredBars": required_bars,
        "historyMode": "SHORT_HISTORY" if short_history else "STANDARD",
        "historyModeText": "新上市/短歷史" if short_history else "標準分析",
        "historyCompletenessPct": min(100.0, available_bars / required_bars * 100.0),
        "historyFirstDate": bars[0].date if bars else None,
        "historyLatestDate": bar.date,
        "historyRequirementText": requirement_text,
        "intradayRangePct": intraday_range_pct,
        "openToClosePct": open_to_close_pct,
        "shortPriceChangePct": price_change_pct,
        "historyTurnover": turnover,
    }


def apply_short_history_overlay(payload: dict[str, object]) -> dict[str, object]:
    if payload.get("historyMode") != "SHORT_HISTORY":
        return payload

    available = payload.get("availableBars")
    required = payload.get("requiredBars")
    requirement_text = payload.get("historyRequirementText")
    change_pct = payload.get("shortPriceChangePct")
    range_pct = payload.get("intradayRangePct")
    open_close_pct = payload.get("openToClosePct")
    price_summary = (
        f"較前一交易日 {pct_text(change_pct)}"
        if isinstance(change_pct, (int, float))
        else "尚無足夠前一日可比較"
    )
    range_summary = (
        f"當日振幅 {range_pct:.2f}%"
        if isinstance(range_pct, (int, float))
        else "當日振幅不足"
    )
    open_close_summary = (
        f"開收變化 {open_close_pct:+.2f}%"
        if isinstance(open_close_pct, (int, float))
        else "開收變化不足"
    )
    reason = (
        f"目前只抓到 {available} 筆日線，低於標準技術指標最低需求 {required} 筆。"
        f"{requirement_text}。先顯示可用的價格與成交量摘要："
        f"{price_summary}、{range_summary}、{open_close_summary}；"
        "不產生買賣訊號。"
    )
    plan_reason = (
        "新上市或剛恢復交易標的需要先累積資料。可先觀察每日高低點、成交量是否連續放大、"
        "以及是否形成至少 8-20 日區間；等資料足夠後再使用 BOLL、KD、RSI、MACD、箱型與道氏確認。"
    )

    payload.update(
        {
            "action": "WATCH",
            "actionText": "短歷史觀察",
            "signal": "SHORT_HISTORY",
            "signalText": "新上市資料不足",
            "reasonText": reason,
            "reliabilityScore": 10,
            "reliabilityText": "低",
            "consensusText": "短歷史",
            "reliabilityReason": reason,
            "planAction": "NO_TRADE",
            "planActionText": "先觀察",
            "setupText": "新上市資料不足",
            "entryTrigger": None,
            "stopLevel": payload.get("low"),
            "targetLevel": payload.get("high"),
            "riskPct": None,
            "rewardPct": None,
            "rewardRiskRatio": None,
            "invalidationText": "資料未滿足標準指標需求前，不建立規則型入場計畫",
            "planReason": plan_reason,
        }
    )
    return payload


def calculate_kd(
    bars: list[object],
    period: int,
    k_smoothing: int,
    d_smoothing: int,
) -> list[dict[str, object]]:
    kd_rows: list[dict[str, object]] = []
    previous: dict[str, object] | None = None
    previous_k = 50.0
    previous_d = 50.0

    for index, bar in enumerate(bars):
        if index + 1 < period:
            current = {
                "kdK": None,
                "kdD": None,
                "kdJ": None,
                "kdRsv": None,
                "kdSignal": "INSUFFICIENT_HISTORY",
                "kdSignalText": "資料不足",
                "kdBias": "NO_DATA",
                "kdBiasText": "資料不足",
                "kdReason": f"需要 {period} 筆資料才能計算 KD。",
                "kdZone": "NO_DATA",
                "kdZoneText": "資料不足",
                "kdSaturation": "NO_DATA",
                "kdSaturationText": "資料不足",
                "kdPersistenceDays": 0,
                "kdSaturationReason": "資料不足，無法判斷 KD 是否鈍化。",
                "kdDivergenceSignal": "NO_DATA",
                "kdDivergenceText": "資料不足",
                "kdDivergenceBias": "NO_DATA",
                "kdDivergenceReason": "資料不足，無法判斷 KD 背離。",
            }
            kd_rows.append(current)
            continue

        window = bars[index - period + 1 : index + 1]
        lowest_low = min(item.low for item in window)
        highest_high = max(item.high for item in window)
        if highest_high == lowest_low:
            rsv = 50.0
        else:
            rsv = (bar.close - lowest_low) / (highest_high - lowest_low) * 100.0

        k_value = previous_k + (rsv - previous_k) / k_smoothing
        d_value = previous_d + (k_value - previous_d) / d_smoothing
        j_value = 3.0 * k_value - 2.0 * d_value
        current = classify_kd(
            k_value=k_value,
            d_value=d_value,
            j_value=j_value,
            rsv=rsv,
            previous=previous,
        )
        current.update(
            calculate_kd_context(
                bars=bars,
                kd_rows=kd_rows,
                index=index,
                k_value=k_value,
                d_value=d_value,
                period=period,
            )
        )
        kd_rows.append(current)
        previous = current
        previous_k = k_value
        previous_d = d_value

    return kd_rows


def calculate_kd_context(
    bars: list[object],
    kd_rows: list[dict[str, object]],
    index: int,
    k_value: float,
    d_value: float,
    period: int,
    divergence_lookback: int = 20,
) -> dict[str, object]:
    if k_value >= 80.0:
        zone = "OVERBOUGHT"
        zone_text = "高位階"
    elif k_value <= 20.0:
        zone = "OVERSOLD"
        zone_text = "低位階"
    else:
        zone = "MID_RANGE"
        zone_text = "中性位階"

    persistence = 1
    for previous_row in reversed(kd_rows):
        previous_zone = previous_row.get("kdZone")
        if previous_zone == zone and zone in {"OVERBOUGHT", "OVERSOLD"}:
            persistence += 1
        else:
            break

    saturation = "NONE"
    saturation_text = "未鈍化"
    saturation_reason = "KD 尚未長時間停留在高檔或低檔，交叉訊號仍可作為短線動能參考。"
    if zone == "OVERBOUGHT" and persistence >= 3:
        saturation = "HIGH_SATURATION"
        saturation_text = "高檔鈍化"
        saturation_reason = (
            f"K 值連續 {persistence} 天高於 80，代表收盤持續接近 {period} 日區間高位；"
            "此時不宜只因高檔或死亡交叉就判定反轉，需搭配價格跌破、量價或其他指標確認。"
        )
    elif zone == "OVERSOLD" and persistence >= 3:
        saturation = "LOW_SATURATION"
        saturation_text = "低檔鈍化"
        saturation_reason = (
            f"K 值連續 {persistence} 天低於 20，代表收盤持續接近 {period} 日區間低位；"
            "此時不宜只因低檔或黃金交叉就判定反彈，需等收盤轉強或其他指標確認。"
        )

    start = max(0, index - divergence_lookback)
    previous_bars = bars[start:index]
    previous_k_values = [
        row.get("kdK")
        for row in kd_rows[start:index]
        if isinstance(row.get("kdK"), (int, float))
    ]
    divergence_signal = "NONE"
    divergence_text = "無明顯背離"
    divergence_bias = "NEUTRAL"
    divergence_reason = "價格與 KD 尚未出現明顯不一致。"

    if previous_bars and previous_k_values:
        prior_high = max(bar.high for bar in previous_bars)
        prior_low = min(bar.low for bar in previous_bars)
        prior_k_high = max(float(value) for value in previous_k_values)
        prior_k_low = min(float(value) for value in previous_k_values)
        current_bar = bars[index]
        if current_bar.high > prior_high and k_value < prior_k_high - 5.0 and k_value >= 50.0:
            divergence_signal = "BEARISH_DIVERGENCE"
            divergence_text = "高檔背離"
            divergence_bias = "CAUTION"
            divergence_reason = "價格創近波段新高，但 K 值未同步創高，代表上攻動能沒有跟上，需留意轉弱確認。"
        elif current_bar.low < prior_low and k_value > prior_k_low + 5.0 and k_value <= 50.0:
            divergence_signal = "BULLISH_DIVERGENCE"
            divergence_text = "低檔背離"
            divergence_bias = "WATCH_REBOUND"
            divergence_reason = "價格創近波段新低，但 K 值未同步創低，代表下跌動能可能收斂，仍需等待反彈確認。"

    return {
        "kdZone": zone,
        "kdZoneText": zone_text,
        "kdSaturation": saturation,
        "kdSaturationText": saturation_text,
        "kdPersistenceDays": persistence if zone in {"OVERBOUGHT", "OVERSOLD"} else 0,
        "kdSaturationReason": saturation_reason,
        "kdDivergenceSignal": divergence_signal,
        "kdDivergenceText": divergence_text,
        "kdDivergenceBias": divergence_bias,
        "kdDivergenceReason": divergence_reason,
    }


def classify_kd(
    k_value: float,
    d_value: float,
    j_value: float,
    rsv: float,
    previous: dict[str, object] | None,
) -> dict[str, object]:
    signal = "KD_READY"
    bias = "NEUTRAL"
    signal_text = "KD 就緒"
    bias_text = "中性"
    reason = "KD 已可用，但還沒有前一日 KD 狀態可比較。"

    previous_k = previous.get("kdK") if previous else None
    previous_d = previous.get("kdD") if previous else None

    if isinstance(previous_k, float) and isinstance(previous_d, float):
        golden_cross = previous_k <= previous_d and k_value > d_value
        death_cross = previous_k >= previous_d and k_value < d_value

        if golden_cross and k_value < 30.0:
            signal = "BULLISH_CROSS_FROM_LOW"
            bias = "BULLISH"
            signal_text = "低檔黃金交叉"
            bias_text = "偏多"
            reason = "K 值在低檔區向上穿越 D 值，反彈動能正在改善。"
        elif golden_cross:
            signal = "BULLISH_CROSS"
            bias = "BULLISH"
            signal_text = "黃金交叉"
            bias_text = "偏多"
            reason = "K 值向上穿越 D 值，短線動能轉強。"
        elif death_cross and k_value > 70.0:
            signal = "BEARISH_CROSS_FROM_HIGH"
            bias = "BEARISH"
            signal_text = "高檔死亡交叉"
            bias_text = "偏空"
            reason = "K 值在高檔區向下跌破 D 值，上攻動能轉弱。"
        elif death_cross:
            signal = "BEARISH_CROSS"
            bias = "BEARISH"
            signal_text = "死亡交叉"
            bias_text = "偏空"
            reason = "K 值向下跌破 D 值，短線動能轉弱。"
        elif k_value >= 80.0 and d_value >= 80.0:
            signal = "OVERBOUGHT_ZONE"
            bias = "CAUTION"
            signal_text = "高位階"
            bias_text = "留意風險"
            reason = "K 值與 D 值都高於 80，代表收盤價接近近期區間高位；可能是強勢延續，也需留意後續轉弱確認。"
        elif k_value <= 20.0 and d_value <= 20.0:
            signal = "OVERSOLD_ZONE"
            bias = "WATCH_REBOUND"
            signal_text = "低位階"
            bias_text = "觀察反彈"
            reason = "K 值與 D 值都低於 20，代表收盤價接近近期區間低位；可能是弱勢延續，也可觀察後續反彈確認。"
        elif k_value > d_value:
            signal = "BULLISH_MOMENTUM"
            bias = "BULLISH"
            signal_text = "偏多動能"
            bias_text = "偏多"
            reason = "K 值維持在 D 值之上，短線動能仍偏強。"
        elif k_value < d_value:
            signal = "BEARISH_MOMENTUM"
            bias = "BEARISH"
            signal_text = "偏空動能"
            bias_text = "偏空"
            reason = "K 值維持在 D 值之下，短線動能仍偏弱。"

    return {
        "kdK": k_value,
        "kdD": d_value,
        "kdJ": j_value,
        "kdRsv": rsv,
        "kdSignal": signal,
        "kdSignalText": signal_text,
        "kdBias": bias,
        "kdBiasText": bias_text,
        "kdReason": reason,
    }


def calculate_rsi(bars: list[object], period: int) -> list[dict[str, object]]:
    rsi_rows: list[dict[str, object]] = []
    avg_gain: float | None = None
    avg_loss: float | None = None
    previous_rsi: float | None = None

    for index, bar in enumerate(bars):
        if index == 0:
            rsi_rows.append(insufficient_rsi(period))
            continue

        change = bar.close - bars[index - 1].close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if index < period:
            rsi_rows.append(insufficient_rsi(period))
            continue

        if index == period:
            changes = [bars[i].close - bars[i - 1].close for i in range(1, period + 1)]
            avg_gain = sum(max(item, 0.0) for item in changes) / period
            avg_loss = sum(max(-item, 0.0) for item in changes) / period
        else:
            assert avg_gain is not None and avg_loss is not None
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0.0 and avg_gain == 0.0:
            rsi_value = 50.0
        elif avg_loss == 0.0:
            rsi_value = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_value = 100.0 - (100.0 / (1.0 + rs))

        current = classify_rsi(rsi_value, previous_rsi)
        rsi_rows.append(current)
        previous_rsi = rsi_value

    return rsi_rows


def insufficient_rsi(period: int) -> dict[str, object]:
    return {
        "rsi": None,
        "rsiSignal": "INSUFFICIENT_HISTORY",
        "rsiSignalText": "資料不足",
        "rsiBias": "NO_DATA",
        "rsiBiasText": "資料不足",
        "rsiReason": f"需要 {period} 筆漲跌資料才能計算 RSI。",
    }


def classify_rsi(rsi_value: float, previous_rsi: float | None) -> dict[str, object]:
    signal = "NEUTRAL"
    signal_text = "中性"
    bias = "NEUTRAL"
    bias_text = "中性"
    reason = "RSI 位於中性區，尚未出現明確超買或超賣訊號。"

    if previous_rsi is not None and previous_rsi <= 30.0 < rsi_value:
        signal = "BULLISH_RECOVERY"
        signal_text = "脫離超賣"
        bias = "BULLISH"
        bias_text = "偏多"
        reason = "RSI 從 30 以下重新站回，短線反彈動能改善。"
    elif previous_rsi is not None and previous_rsi >= 70.0 > rsi_value:
        signal = "BEARISH_ROLLOVER"
        signal_text = "脫離超買"
        bias = "BEARISH"
        bias_text = "偏空"
        reason = "RSI 從 70 以上回落，上攻動能降溫。"
    elif rsi_value >= 70.0:
        signal = "OVERBOUGHT"
        signal_text = "超買"
        bias = "CAUTION"
        bias_text = "留意風險"
        reason = "RSI 高於 70，價格可能偏熱，等待延續或反轉確認。"
    elif rsi_value <= 30.0:
        signal = "OVERSOLD"
        signal_text = "超賣"
        bias = "WATCH_REBOUND"
        bias_text = "觀察反彈"
        reason = "RSI 低於 30，價格偏弱但可能進入反彈觀察區。"
    elif rsi_value >= 50.0:
        signal = "BULLISH_SIDE"
        signal_text = "偏多區"
        bias = "BULLISH"
        bias_text = "偏多"
        reason = "RSI 高於 50，動能結構偏多。"
    else:
        signal = "BEARISH_SIDE"
        signal_text = "偏空區"
        bias = "BEARISH"
        bias_text = "偏空"
        reason = "RSI 低於 50，動能結構偏弱。"

    return {
        "rsi": rsi_value,
        "rsiSignal": signal,
        "rsiSignalText": signal_text,
        "rsiBias": bias,
        "rsiBiasText": bias_text,
        "rsiReason": reason,
    }


def calculate_macd(
    bars: list[object],
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> list[dict[str, object]]:
    macd_rows: list[dict[str, object]] = []
    fast_ema: float | None = None
    slow_ema: float | None = None
    signal_ema: float | None = None
    previous: dict[str, object] | None = None

    for index, bar in enumerate(bars):
        fast_ema = ema(fast_ema, bar.close, fast_period)
        slow_ema = ema(slow_ema, bar.close, slow_period)

        if index + 1 < slow_period:
            macd_rows.append(insufficient_macd(slow_period))
            continue

        macd_value = fast_ema - slow_ema
        signal_ema = ema(signal_ema, macd_value, signal_period)
        histogram = macd_value - signal_ema
        current = classify_macd(macd_value, signal_ema, histogram, previous)
        macd_rows.append(current)
        previous = current

    return macd_rows


def insufficient_macd(slow_period: int) -> dict[str, object]:
    return {
        "macd": None,
        "macdSignalLine": None,
        "macdHistogram": None,
        "macdSignal": "INSUFFICIENT_HISTORY",
        "macdSignalText": "資料不足",
        "macdBias": "NO_DATA",
        "macdBiasText": "資料不足",
        "macdReason": f"需要至少 {slow_period} 筆資料才能計算 MACD。",
    }


def classify_macd(
    macd_value: float,
    signal_line: float,
    histogram: float,
    previous: dict[str, object] | None,
) -> dict[str, object]:
    signal = "MACD_READY"
    signal_text = "MACD 就緒"
    bias = "NEUTRAL"
    bias_text = "中性"
    reason = "MACD 已可用，但還沒有前一日狀態可比較。"

    previous_macd = previous.get("macd") if previous else None
    previous_signal = previous.get("macdSignalLine") if previous else None

    if isinstance(previous_macd, float) and isinstance(previous_signal, float):
        bullish_cross = previous_macd <= previous_signal and macd_value > signal_line
        bearish_cross = previous_macd >= previous_signal and macd_value < signal_line

        if bullish_cross and macd_value < 0.0:
            signal = "BULLISH_CROSS_BELOW_ZERO"
            signal_text = "零軸下黃金交叉"
            bias = "BULLISH"
            bias_text = "偏多"
            reason = "MACD 在零軸下方黃金交叉，跌勢收斂並出現反彈動能。"
        elif bullish_cross:
            signal = "BULLISH_CROSS"
            signal_text = "黃金交叉"
            bias = "BULLISH"
            bias_text = "偏多"
            reason = "MACD 向上穿越訊號線，趨勢動能轉強。"
        elif bearish_cross and macd_value > 0.0:
            signal = "BEARISH_CROSS_ABOVE_ZERO"
            signal_text = "零軸上死亡交叉"
            bias = "BEARISH"
            bias_text = "偏空"
            reason = "MACD 在零軸上方死亡交叉，上升動能降溫。"
        elif bearish_cross:
            signal = "BEARISH_CROSS"
            signal_text = "死亡交叉"
            bias = "BEARISH"
            bias_text = "偏空"
            reason = "MACD 向下跌破訊號線，趨勢動能轉弱。"
        elif histogram > 0.0 and macd_value > 0.0:
            signal = "BULLISH_ABOVE_ZERO"
            signal_text = "零軸上偏多"
            bias = "BULLISH"
            bias_text = "偏多"
            reason = "MACD 與柱狀體位於正值區，趨勢動能偏多。"
        elif histogram < 0.0 and macd_value < 0.0:
            signal = "BEARISH_BELOW_ZERO"
            signal_text = "零軸下偏空"
            bias = "BEARISH"
            bias_text = "偏空"
            reason = "MACD 與柱狀體位於負值區，趨勢動能偏弱。"
        elif histogram > 0.0:
            signal = "BULLISH_HISTOGRAM"
            signal_text = "柱狀體轉強"
            bias = "BULLISH"
            bias_text = "偏多"
            reason = "MACD 柱狀體為正，短線動能偏向改善。"
        elif histogram < 0.0:
            signal = "BEARISH_HISTOGRAM"
            signal_text = "柱狀體轉弱"
            bias = "BEARISH"
            bias_text = "偏空"
            reason = "MACD 柱狀體為負，短線動能偏向轉弱。"

    return {
        "macd": macd_value,
        "macdSignalLine": signal_line,
        "macdHistogram": histogram,
        "macdSignal": signal,
        "macdSignalText": signal_text,
        "macdBias": bias,
        "macdBiasText": bias_text,
        "macdReason": reason,
    }


def bias_to_direction(bias: object) -> str | None:
    if bias == "BULLISH":
        return "BULLISH"
    if bias == "BEARISH":
        return "BEARISH"
    return None


def calculate_reliability(payload: dict[str, object]) -> dict[str, object]:
    votes: list[tuple[str, str]] = []
    action = payload.get("action")
    if action == "BUY":
        votes.append(("布林", "BULLISH"))
    elif action == "SELL":
        votes.append(("布林", "BEARISH"))

    for label, key in (
        ("KD", "kdBias"),
        ("RSI", "rsiBias"),
        ("MACD", "macdBias"),
        ("均線", "trendBias"),
        ("量價", "volumePriceBias"),
        ("箱型", "boxBias"),
        ("道氏", "dowBias"),
    ):
        direction = bias_to_direction(payload.get(key))
        if direction:
            votes.append((label, direction))

    bullish = sum(1 for _, direction in votes if direction == "BULLISH")
    bearish = sum(1 for _, direction in votes if direction == "BEARISH")
    total = bullish + bearish

    if total == 0:
        score = 30
        consensus = "方向不足"
        reliability = "低"
        reason = "目前沒有足夠同向指標，技術訊號只能作為觀察。"
    elif bullish and bearish:
        leading = max(bullish, bearish)
        score = max(35, round(50 + (leading - min(bullish, bearish)) * 10))
        consensus = "指標分歧"
        reliability = "低" if bullish == bearish else "中"
        reason = (
            f"{bullish} 個偏多、{bearish} 個偏空；不同指標互相牴觸，"
            "可靠度下修，應等待更清楚的確認。"
        )
    else:
        direction_text = "偏多" if bullish else "偏空"
        count = bullish or bearish
        score = min(95, 45 + count * 13)
        consensus = f"{direction_text}共振"
        reliability = "高" if count >= 3 else "中"
        names = "、".join(label for label, _ in votes)
        reason = f"{names} 形成{direction_text}同向訊號；同向指標越多，短線判讀可靠度越高。"

    liquidity_signal = payload.get("liquiditySignal")
    if liquidity_signal == "LOW_LIQUIDITY":
        score = max(20, score - 15)
        reliability = "低" if reliability != "高" else "中"
        reason = f"{reason} 但60日均成交金額偏低，需下修技術訊號可信度。"
    elif liquidity_signal == "THIN_LIQUIDITY":
        score = max(25, score - 5)
        reason = f"{reason} 流動性普通，進出場需留意滑價。"

    kd_divergence = payload.get("kdDivergenceSignal")
    kd_saturation = payload.get("kdSaturation")
    if kd_divergence == "BEARISH_DIVERGENCE" and bullish >= bearish:
        score = max(25, score - 8)
        reason = f"{reason} KD 出現高檔背離，偏多訊號需再等價格或量價確認。"
    elif kd_divergence == "BULLISH_DIVERGENCE" and bearish >= bullish:
        score = max(25, score - 5)
        reason = f"{reason} KD 出現低檔背離，偏空訊號需留意反彈確認。"

    if kd_saturation in {"HIGH_SATURATION", "LOW_SATURATION"}:
        score = max(25, score - 3)
        reason = f"{reason} KD 已進入鈍化，單純交叉訊號參考價值下降。"

    box_quality = payload.get("boxQualityScore")
    box_signal = payload.get("boxSignal")
    if box_signal == "BULLISH_BOX_BREAKOUT" and isinstance(box_quality, (int, float)):
        score = min(98, score + min(10, round(box_quality / 12)))
        reason = f"{reason} 箱頂放量突破，順勢訊號加分。"
    elif box_signal in {"UPPER_FALSE_BREAKOUT", "BOX_TOO_WIDE"}:
        score = max(20, score - 6)
        reason = f"{reason} 箱型訊號顯示假突破或箱體過寬，需下修可信度。"

    dow_volume_confirm = payload.get("dowVolumeConfirm")
    dow_reversal = payload.get("dowReversalSignal")
    if dow_volume_confirm == "DIVERGENT":
        score = max(20, score - 5)
        reason = f"{reason} 道氏量能確認不足，趨勢訊號需保守看待。"
    if dow_reversal in {"REVERSAL_DOWN_RISK", "REVERSAL_UP_RISK"}:
        score = max(20, score - 6)
        reason = f"{reason} 道氏結構出現反轉觀察訊號。"

    return {
        "reliabilityScore": score,
        "reliabilityText": reliability,
        "consensusText": consensus,
        "reliabilityReason": reason,
        "bullishVotes": bullish,
        "bearishVotes": bearish,
    }


def min_defined(*values: object) -> float | None:
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    return min(numbers) if numbers else None


def max_defined(*values: object) -> float | None:
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    return max(numbers) if numbers else None


def pct_distance(from_price: object, to_price: object) -> float | None:
    if not isinstance(from_price, (int, float)) or not isinstance(to_price, (int, float)):
        return None
    if from_price == 0:
        return None
    return (float(to_price) - float(from_price)) / float(from_price) * 100.0


def calculate_trade_plan(payload: dict[str, object]) -> dict[str, object]:
    action = payload.get("action")
    close = payload.get("close")
    high = payload.get("high")
    low = payload.get("low")
    lower = payload.get("lower")
    middle = payload.get("middle")
    upper = payload.get("upper")
    support = payload.get("support")
    resistance = payload.get("resistance")

    plan_action = "NO_TRADE"
    plan_action_text = "不進場"
    setup_text = "沒有符合完整入場規則"
    entry_trigger = None
    stop_level = None
    target_level = None
    plan_reason = "目前技術訊號不足以形成完整交易計畫，先等待下一個明確觸發條件。"

    if action == "BUY":
        plan_action = "LONG"
        plan_action_text = "做多計畫"
        setup_text = "買進訊號成立"
        entry_trigger = high
        stop_level = min_defined(support, lower, low)
        target_level = max_defined(middle, resistance, upper)
        plan_reason = "若價格突破當日高點，可視為入場觸發；停損放在近期支撐/布林下軌附近，出場先看中線、壓力或上軌。"
    elif action == "SELL":
        plan_action = "SHORT_OR_EXIT"
        plan_action_text = "賣出 / 降低風險"
        setup_text = "賣出訊號成立"
        entry_trigger = low
        stop_level = max_defined(resistance, upper, high)
        target_level = min_defined(middle, support, lower)
        plan_reason = "若價格跌破當日低點，可視為賣出或降低風險觸發；停損/失效點放在近期壓力或布林上軌附近。"
    elif action == "WAIT_CONFIRMATION":
        setup_text = "等待反彈確認"
        stop_level = min_defined(support, lower, low)
        target_level = middle if isinstance(middle, (int, float)) else resistance
        plan_reason = "價格接近弱勢區，但尚未出現足夠量價確認；需等收盤重新站回下軌或 KD/RSI 改善後才考慮。"
    elif action == "WATCH":
        setup_text = "等待突破或反轉確認"
        stop_level = middle if isinstance(middle, (int, float)) else support
        target_level = resistance if isinstance(resistance, (int, float)) else upper
        plan_reason = "價格接近強勢延伸區，需等待放量突破或反轉訊號，避免只因接近上軌而追價。"

    risk_pct = pct_distance(entry_trigger, stop_level)
    reward_pct = pct_distance(entry_trigger, target_level)
    reward_risk_ratio = (
        abs(reward_pct / risk_pct)
        if isinstance(risk_pct, float)
        and isinstance(reward_pct, float)
        and risk_pct != 0
        else None
    )
    invalidation_text = (
        f"跌破 {stop_level:.2f} 視為計畫失效"
        if plan_action == "LONG" and isinstance(stop_level, float)
        else f"突破 {stop_level:.2f} 視為風險回升"
        if plan_action == "SHORT_OR_EXIT" and isinstance(stop_level, float)
        else "尚未有入場，先以支撐/壓力作觀察線"
    )

    return {
        "planAction": plan_action,
        "planActionText": plan_action_text,
        "setupText": setup_text,
        "entryTrigger": entry_trigger,
        "stopLevel": stop_level,
        "targetLevel": target_level,
        "riskPct": risk_pct,
        "rewardPct": reward_pct,
        "rewardRiskRatio": reward_risk_ratio,
        "invalidationText": invalidation_text,
        "planReason": plan_reason,
    }


def calculate_market_structure(
    bars: list[object],
    short_period: int = 20,
    long_period: int = 60,
    sr_period: int = 20,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    closes = [bar.close for bar in bars]

    for index, bar in enumerate(bars):
        ma_short = (
            STRATEGY.rolling_mean(closes[index - short_period + 1 : index + 1])
            if index + 1 >= short_period
            else None
        )
        ma_long = (
            STRATEGY.rolling_mean(closes[index - long_period + 1 : index + 1])
            if index + 1 >= long_period
            else None
        )
        sr_window = bars[max(0, index - sr_period + 1) : index + 1]
        support = min(item.low for item in sr_window) if sr_window else None
        resistance = max(item.high for item in sr_window) if sr_window else None
        support_distance = (
            (bar.close - support) / bar.close * 100.0
            if support is not None and bar.close
            else None
        )
        resistance_distance = (
            (resistance - bar.close) / bar.close * 100.0
            if resistance is not None and bar.close
            else None
        )

        trend_signal = "INSUFFICIENT_HISTORY"
        trend_signal_text = "資料不足"
        trend_bias = "NO_DATA"
        trend_bias_text = "資料不足"
        trend_reason = f"需要至少 {long_period} 筆資料才能判斷均線趨勢。"

        if ma_short is not None and ma_long is not None:
            if bar.close >= ma_short and ma_short >= ma_long:
                trend_signal = "BULLISH_TREND"
                trend_signal_text = "均線偏多"
                trend_bias = "BULLISH"
                trend_bias_text = "偏多"
                trend_reason = "收盤價位於短均線之上，且短均線高於長均線，趨勢結構偏多。"
            elif bar.close <= ma_short and ma_short <= ma_long:
                trend_signal = "BEARISH_TREND"
                trend_signal_text = "均線偏空"
                trend_bias = "BEARISH"
                trend_bias_text = "偏空"
                trend_reason = "收盤價位於短均線之下，且短均線低於長均線，趨勢結構偏空。"
            elif bar.close >= ma_short:
                trend_signal = "MIXED_RECOVERY"
                trend_signal_text = "短線轉強"
                trend_bias = "NEUTRAL"
                trend_bias_text = "中性"
                trend_reason = "收盤價站上短均線，但短長均線尚未形成同向排列。"
            else:
                trend_signal = "MIXED_WEAKNESS"
                trend_signal_text = "短線轉弱"
                trend_bias = "NEUTRAL"
                trend_bias_text = "中性"
                trend_reason = "收盤價跌破短均線，但短長均線尚未形成同向排列。"

        structure_note = "支撐壓力距離中性。"
        if support_distance is not None and support_distance <= 2.0:
            structure_note = "價格接近近期支撐，若跌破需留意轉弱。"
        if resistance_distance is not None and resistance_distance <= 2.0:
            structure_note = "價格接近近期壓力，若突破需觀察量能確認。"

        rows.append(
            {
                "maShort": ma_short,
                "maLong": ma_long,
                "support": support,
                "resistance": resistance,
                "supportDistancePct": support_distance,
                "resistanceDistancePct": resistance_distance,
                "trendSignal": trend_signal,
                "trendSignalText": trend_signal_text,
                "trendBias": trend_bias,
                "trendBiasText": trend_bias_text,
                "trendReason": f"{trend_reason} {structure_note}",
            }
        )

    return rows


def classify_direction(value: float | None, up_threshold: float, down_threshold: float) -> str:
    if value is None:
        return "NO_DATA"
    if value >= up_threshold:
        return "UP"
    if value <= down_threshold:
        return "DOWN"
    return "FLAT"


def calculate_volume_price(
    bars: list[object],
    symbol: str,
    volume_period: int = 20,
    turnover_period: int = 60,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    volumes = [bar.volume for bar in bars]
    turnovers = [bar.close * bar.volume for bar in bars]
    is_taiwan_symbol = symbol.endswith((".TW", ".TWO"))
    low_turnover = 1_000_000
    thin_turnover = 10_000_000 if is_taiwan_symbol else 5_000_000

    for index, bar in enumerate(bars):
        prev_close = bars[index - 1].close if index > 0 else None
        price_change_pct = (
            (bar.close - prev_close) / prev_close * 100.0
            if isinstance(prev_close, (int, float)) and prev_close
            else None
        )
        volume_ma = (
            STRATEGY.rolling_mean(volumes[index - volume_period + 1 : index + 1])
            if index + 1 >= volume_period
            else None
        )
        volume_ratio = bar.volume / volume_ma if volume_ma else None
        turnover_ma = (
            STRATEGY.rolling_mean(turnovers[index - turnover_period + 1 : index + 1])
            if index + 1 >= turnover_period
            else None
        )

        price_direction = classify_direction(price_change_pct, 0.5, -0.5)
        volume_direction = classify_direction(volume_ratio, 1.2, 0.8)
        signal = f"{price_direction}_{volume_direction}"
        signal_text = "量價資料不足"
        bias = "NO_DATA"
        bias_text = "資料不足"
        reason = "需要前一日收盤價與均量，才能判斷量價關係。"

        if volume_ratio is None and price_change_pct is not None:
            signal = f"{price_direction}_SHORT_VOLUME"
            bias = "WATCH"
            bias_text = "短歷史觀察"
            if price_direction == "UP":
                signal_text = "價漲 / 均量不足"
            elif price_direction == "DOWN":
                signal_text = "價跌 / 均量不足"
            else:
                signal_text = "價平 / 均量不足"
            reason = (
                f"已有前一日價格可比較，但尚未滿 {volume_period} 日均量，"
                "成交量只能看絕對值與後續是否連續放大，不能判定標準量價訊號。"
            )
        elif signal == "UP_UP":
            signal_text = "價漲量增"
            bias = "BULLISH"
            bias_text = "偏多"
            reason = "價格上漲且成交量高於均量，代表趨勢推動較有量能確認。"
        elif signal == "DOWN_DOWN":
            signal_text = "價跌量縮"
            bias = "BEARISH"
            bias_text = "偏空"
            reason = "價格下跌且成交量萎縮，買氣偏冷，弱勢仍需觀察是否延續。"
        elif signal == "UP_DOWN":
            signal_text = "價漲量縮"
            bias = "CAUTION"
            bias_text = "留意背離"
            reason = "價格上漲但成交量縮小，上攻力道沒有同步確認，若位階偏高需留意轉弱。"
        elif signal == "DOWN_UP":
            signal_text = "價跌量增"
            bias = "CAUTION"
            bias_text = "留意背離"
            reason = "價格下跌但成交量放大，可能是賣壓擴大，也可能是低檔承接，需搭配支撐與反彈確認。"
        elif signal == "FLAT_UP":
            signal_text = "價平量增"
            bias = "WATCH"
            bias_text = "觀察突破"
            reason = "價格變動不大但成交量放大，買賣力道拉扯，等待突破方向確認。"
        elif signal == "UP_FLAT":
            signal_text = "價漲量平"
            bias = "NEUTRAL"
            bias_text = "中性偏多"
            reason = "價格上漲但成交量未明顯放大，趨勢偏上但動能確認普通。"
        elif signal == "DOWN_FLAT":
            signal_text = "價跌量平"
            bias = "NEUTRAL"
            bias_text = "中性偏空"
            reason = "價格下跌但成交量大致持平，趨勢偏弱但尚未出現明顯放量賣壓。"
        elif signal == "FLAT_DOWN":
            signal_text = "價平量縮"
            bias = "NEUTRAL"
            bias_text = "觀望"
            reason = "價格持平且成交量萎縮，市場參與度下降，方向仍不明確。"
        elif signal == "FLAT_FLAT":
            signal_text = "價平量平"
            bias = "NEUTRAL"
            bias_text = "觀望"
            reason = "價格與成交量都缺乏明顯變化，技術方向仍不明確。"

        liquidity_signal = "NO_DATA"
        liquidity_text = "資料不足"
        liquidity_reason = "需要至少 60 日資料才可估算平均成交金額。"
        if turnover_ma is not None:
            if turnover_ma < low_turnover:
                liquidity_signal = "LOW_LIQUIDITY"
                liquidity_text = "流動性偏低"
                liquidity_reason = "60 日均成交金額低於 100 萬，買賣可能較不活絡，需留意流動性風險。"
            elif turnover_ma < thin_turnover:
                liquidity_signal = "THIN_LIQUIDITY"
                liquidity_text = "流動性普通"
                liquidity_reason = "60 日均成交金額不高，進出場仍應留意成交量與滑價。"
            else:
                liquidity_signal = "OK"
                liquidity_text = "流動性正常"
                liquidity_reason = "60 日均成交金額高於低流動性門檻。"

        rows.append(
            {
                "priceChangePct": price_change_pct,
                "priceDirection": price_direction,
                "volumeDirection": volume_direction,
                "turnover": turnovers[index],
                "turnoverMa": turnover_ma,
                "volumePriceSignal": signal,
                "volumePriceSignalText": signal_text,
                "volumePriceBias": bias,
                "volumePriceBiasText": bias_text,
                "volumePriceReason": reason,
                "liquiditySignal": liquidity_signal,
                "liquidityText": liquidity_text,
                "liquidityReason": liquidity_reason,
            }
        )

    return rows


def calculate_box_structure(
    bars: list[object],
    lookback: int = 20,
    min_days: int = 8,
    breakout_buffer_pct: float = 0.3,
    volume_period: int = 20,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    volumes = [bar.volume for bar in bars]
    closes = [bar.close for bar in bars]
    buffer = breakout_buffer_pct / 100.0

    for index, bar in enumerate(bars):
        if index < min_days:
            rows.append(
                {
                    "boxSignal": "INSUFFICIENT_HISTORY",
                    "boxSignalText": "資料不足",
                    "boxBias": "NO_DATA",
                    "boxBiasText": "資料不足",
                    "boxUpper": None,
                    "boxLower": None,
                    "boxMid": None,
                    "boxWidthPct": None,
                    "boxDays": 0,
                    "boxQualityScore": 0,
                    "boxBreakoutLevel": None,
                    "boxStopLevel": None,
                    "boxTargetLevel": None,
                    "boxReason": f"需要至少 {min_days + 1} 根 K 棒才能建立箱型觀察區間。",
                }
            )
            continue

        window = bars[max(0, index - lookback) : index]
        box_days = len(window)
        highs = [item.high for item in window]
        lows = [item.low for item in window]
        box_upper = max(highs)
        box_lower = min(lows)
        box_mid = (box_upper + box_lower) / 2.0
        width_pct = (box_upper - box_lower) / box_lower * 100.0 if box_lower else None
        box_height = box_upper - box_lower
        top_tolerance = max(box_upper * 0.006, box_height * 0.08)
        bottom_tolerance = max(box_lower * 0.006, box_height * 0.08)
        top_touches = sum(1 for item in window if box_upper - item.high <= top_tolerance)
        bottom_touches = sum(1 for item in window if item.low - box_lower <= bottom_tolerance)
        volume_ma = (
            STRATEGY.rolling_mean(volumes[index - volume_period : index])
            if index >= volume_period
            else None
        )
        volume_ratio = bar.volume / volume_ma if volume_ma else None
        ma_short = (
            STRATEGY.rolling_mean(closes[index - 20 : index])
            if index >= 20
            else None
        )
        ma_long = (
            STRATEGY.rolling_mean(closes[index - 60 : index])
            if index >= 60
            else None
        )
        uptrend = (
            isinstance(ma_short, (int, float))
            and isinstance(ma_long, (int, float))
            and ma_short >= ma_long
            and bar.close >= ma_long
        )

        quality = 0
        if box_days >= 15:
            quality += 20
        elif box_days >= min_days:
            quality += 12
        if top_touches >= 2 and bottom_touches >= 2:
            quality += 22
        elif top_touches >= 1 and bottom_touches >= 1:
            quality += 12
        if isinstance(width_pct, float):
            if 3.0 <= width_pct <= 15.0:
                quality += 22
            elif width_pct < 3.0:
                quality += 12
            elif width_pct <= 25.0:
                quality += 8
            else:
                quality -= 10
        if uptrend:
            quality += 18

        upper_break = box_upper * (1.0 + buffer)
        lower_break = box_lower * (1.0 - buffer)
        width_value = box_upper - box_lower
        target_level = box_upper + width_value
        near_top = box_upper and (box_upper - bar.close) / box_upper * 100.0 <= 2.0
        near_bottom = box_lower and (bar.close - box_lower) / box_lower * 100.0 <= 2.0
        volume_confirmed = isinstance(volume_ratio, (int, float)) and volume_ratio >= 1.2

        signal = "BOX_RANGE"
        signal_text = "箱內整理"
        bias = "NEUTRAL"
        bias_text = "觀望"
        reason = (
            f"近 {box_days} 根 K 棒箱頂 {box_upper:.2f}、箱底 {box_lower:.2f}，"
            f"箱體寬度 {width_pct:.1f}%；箱型品質 {max(0, min(100, quality))}/100。"
        )

        if isinstance(width_pct, float) and width_pct > 25.0:
            signal = "BOX_TOO_WIDE"
            signal_text = "箱體過寬"
            bias = "CAUTION"
            bias_text = "風險偏高"
            reason += " 箱體過寬，停損距離大，較不適合直接用箱頂突破策略。"
        elif bar.close > upper_break:
            if volume_confirmed:
                signal = "BULLISH_BOX_BREAKOUT"
                signal_text = "箱頂放量突破"
                bias = "BULLISH"
                bias_text = "偏多"
                quality += 15
                reason += " 收盤突破箱頂且成交量高於均量，屬於較有效的順勢突破觀察。"
            else:
                signal = "BULLISH_BOX_BREAKOUT_WEAK_VOLUME"
                signal_text = "箱頂突破量能不足"
                bias = "WATCH"
                bias_text = "等待確認"
                quality += 5
                reason += " 收盤突破箱頂，但成交量未明顯放大，需防假突破或回測箱頂。"
        elif bar.high > upper_break and bar.close <= box_upper:
            signal = "UPPER_FALSE_BREAKOUT"
            signal_text = "上緣假突破"
            bias = "CAUTION"
            bias_text = "留意風險"
            reason += " 盤中突破箱頂但收盤回到箱內，需留意假突破。"
        elif bar.close < lower_break:
            signal = "BEARISH_BOX_BREAKDOWN"
            signal_text = "跌破箱底"
            bias = "BEARISH"
            bias_text = "偏空"
            quality += 10 if volume_confirmed else 0
            reason += " 收盤跌破箱底，箱型支撐失效，應優先控管風險。"
        elif bar.low < lower_break and bar.close >= box_lower:
            signal = "LOWER_FALSE_BREAKDOWN"
            signal_text = "下緣假跌破"
            bias = "WATCH"
            bias_text = "觀察反彈"
            reason += " 盤中跌破箱底但收盤回到箱內，可觀察是否形成支撐。"
        elif near_top:
            signal = "BOX_NEAR_TOP"
            signal_text = "接近箱頂"
            bias = "WATCH"
            bias_text = "等待突破"
            reason += " 價格接近箱頂，若後續放量突破才提高順勢買進可信度。"
        elif near_bottom:
            signal = "BOX_NEAR_BOTTOM"
            signal_text = "接近箱底"
            bias = "WATCH"
            bias_text = "觀察支撐"
            reason += " 價格接近箱底，若跌破代表箱型支撐失效。"
        else:
            reason += " 價格仍在箱體中段，方向尚未明確。"

        if not uptrend and signal.startswith("BULLISH"):
            quality -= 8
            reason += " 目前中期趨勢未明顯偏多，箱型突破分數下修。"

        rows.append(
            {
                "boxSignal": signal,
                "boxSignalText": signal_text,
                "boxBias": bias,
                "boxBiasText": bias_text,
                "boxUpper": box_upper,
                "boxLower": box_lower,
                "boxMid": box_mid,
                "boxWidthPct": width_pct,
                "boxDays": box_days,
                "boxQualityScore": max(0, min(100, round(quality))),
                "boxBreakoutLevel": upper_break,
                "boxStopLevel": box_lower,
                "boxTargetLevel": target_level,
                "boxReason": reason,
            }
        )

    return rows


def calculate_dow_theory(
    bars: list[object],
    volume_price_rows: list[dict[str, object]],
    major_period: int = 60,
    secondary_period: int = 20,
    minor_period: int = 5,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    closes = [bar.close for bar in bars]

    for index, bar in enumerate(bars):
        if index + 1 < major_period:
            rows.append(
                {
                    "dowSignal": "INSUFFICIENT_HISTORY",
                    "dowSignalText": "資料不足",
                    "dowBias": "NO_DATA",
                    "dowBiasText": "資料不足",
                    "dowPrimaryTrend": "NO_DATA",
                    "dowPrimaryTrendText": "資料不足",
                    "dowSecondaryTrend": "NO_DATA",
                    "dowSecondaryTrendText": "資料不足",
                    "dowPhase": "NO_DATA",
                    "dowPhaseText": "資料不足",
                    "dowVolumeConfirm": "NO_DATA",
                    "dowVolumeConfirmText": "資料不足",
                    "dowReversalSignal": "NO_DATA",
                    "dowReversalText": "資料不足",
                    "dowScore": 0,
                    "dowReason": f"需要至少 {major_period} 根 K 棒才能判斷道氏主要趨勢。",
                }
            )
            continue

        ma_major = STRATEGY.rolling_mean(closes[index - major_period + 1 : index + 1])
        ma_secondary = STRATEGY.rolling_mean(closes[index - secondary_period + 1 : index + 1])
        ma_minor = STRATEGY.rolling_mean(closes[index - minor_period + 1 : index + 1])

        current_window = bars[index - secondary_period + 1 : index + 1]
        previous_window = bars[max(0, index - secondary_period * 2 + 1) : index - secondary_period + 1]
        current_high = max(item.high for item in current_window)
        current_low = min(item.low for item in current_window)
        prior_high = max(item.high for item in previous_window) if previous_window else current_high
        prior_low = min(item.low for item in previous_window) if previous_window else current_low

        higher_high = current_high > prior_high * 1.003
        higher_low = current_low > prior_low * 1.003
        lower_high = current_high < prior_high * 0.997
        lower_low = current_low < prior_low * 0.997
        price_above_major = bar.close >= ma_major
        price_below_major = bar.close <= ma_major

        primary_trend = "SIDEWAYS"
        primary_text = "主要趨勢不明"
        bias = "NEUTRAL"
        bias_text = "觀望"
        signal = "DOW_SIDEWAYS"
        signal_text = "道氏趨勢不明"
        score = 45

        if price_above_major and ma_secondary >= ma_major and higher_high and higher_low:
            primary_trend = "PRIMARY_UPTREND"
            primary_text = "主要上升趨勢"
            bias = "BULLISH"
            bias_text = "偏多"
            signal = "DOW_UPTREND_CONFIRMED"
            signal_text = "高低點墊高"
            score = 72
        elif price_below_major and ma_secondary <= ma_major and lower_high and lower_low:
            primary_trend = "PRIMARY_DOWNTREND"
            primary_text = "主要下降趨勢"
            bias = "BEARISH"
            bias_text = "偏空"
            signal = "DOW_DOWNTREND_CONFIRMED"
            signal_text = "高低點下移"
            score = 28
        elif price_above_major and ma_secondary >= ma_major:
            primary_trend = "UPTREND_UNCONFIRMED"
            primary_text = "上升趨勢待確認"
            bias = "BULLISH"
            bias_text = "偏多"
            signal = "DOW_UPTREND_UNCONFIRMED"
            signal_text = "均線偏多但結構未完整"
            score = 62
        elif price_below_major and ma_secondary <= ma_major:
            primary_trend = "DOWNTREND_UNCONFIRMED"
            primary_text = "下降趨勢待確認"
            bias = "BEARISH"
            bias_text = "偏空"
            signal = "DOW_DOWNTREND_UNCONFIRMED"
            signal_text = "均線偏空但結構未完整"
            score = 38

        secondary_trend = "SECONDARY_SIDEWAYS"
        secondary_text = "次級整理"
        if ma_minor >= ma_secondary and bar.close >= ma_secondary:
            secondary_trend = "SECONDARY_UP"
            secondary_text = "次級反彈/推升"
        elif ma_minor <= ma_secondary and bar.close <= ma_secondary:
            secondary_trend = "SECONDARY_DOWN"
            secondary_text = "次級回檔/下探"

        distance_major = (bar.close - ma_major) / ma_major * 100.0 if ma_major else 0.0
        phase = "RANGE"
        phase_text = "整理階段"
        if primary_trend in {"PRIMARY_UPTREND", "UPTREND_UNCONFIRMED"}:
            if distance_major > 18:
                phase = "EXCESS"
                phase_text = "過熱階段"
            elif distance_major > 5:
                phase = "PUBLIC_PARTICIPATION"
                phase_text = "大眾參與階段"
            else:
                phase = "ACCUMULATION"
                phase_text = "累積/初升階段"
        elif primary_trend in {"PRIMARY_DOWNTREND", "DOWNTREND_UNCONFIRMED"}:
            if distance_major < -18:
                phase = "PANIC"
                phase_text = "恐慌/超跌階段"
            elif distance_major < -5:
                phase = "MARKDOWN"
                phase_text = "大眾賣出階段"
            else:
                phase = "DISTRIBUTION"
                phase_text = "分配/轉弱階段"

        volume_price_signal = volume_price_rows[index].get("volumePriceSignal") if index < len(volume_price_rows) else None
        volume_confirm = "NEUTRAL"
        volume_confirm_text = "量能未明確確認"
        if primary_trend in {"PRIMARY_UPTREND", "UPTREND_UNCONFIRMED"}:
            if volume_price_signal in {"UP_UP", "DOWN_DOWN"}:
                volume_confirm = "CONFIRMED"
                volume_confirm_text = "量能順多方趨勢"
                score += 8
            elif volume_price_signal in {"UP_DOWN", "DOWN_UP"}:
                volume_confirm = "DIVERGENT"
                volume_confirm_text = "量能與多方趨勢背離"
                score -= 8
        elif primary_trend in {"PRIMARY_DOWNTREND", "DOWNTREND_UNCONFIRMED"}:
            if volume_price_signal in {"DOWN_UP", "UP_DOWN"}:
                volume_confirm = "CONFIRMED"
                volume_confirm_text = "量能順空方趨勢"
                score -= 8
            elif volume_price_signal in {"DOWN_DOWN", "UP_UP"}:
                volume_confirm = "DIVERGENT"
                volume_confirm_text = "量能與空方趨勢背離"
                score += 5

        reversal_signal = "NONE"
        reversal_text = "尚無明確反轉"
        if primary_trend in {"PRIMARY_UPTREND", "UPTREND_UNCONFIRMED"} and bar.close < prior_low and volume_confirm == "DIVERGENT":
            reversal_signal = "REVERSAL_DOWN_RISK"
            reversal_text = "上升趨勢反轉風險"
            score -= 12
        elif primary_trend in {"PRIMARY_DOWNTREND", "DOWNTREND_UNCONFIRMED"} and bar.close > prior_high and volume_confirm == "DIVERGENT":
            reversal_signal = "REVERSAL_UP_RISK"
            reversal_text = "下降趨勢反轉觀察"
            score += 10

        if primary_trend == "SIDEWAYS":
            structure_text = "高低點未形成明確墊高或下移。"
        elif primary_trend in {"PRIMARY_UPTREND", "UPTREND_UNCONFIRMED"}:
            structure_text = "以收盤價和均線觀察，主要方向偏上；需留意是否持續出現更高高點與更高低點。"
        else:
            structure_text = "以收盤價和均線觀察，主要方向偏下；需留意是否持續出現更低高點與更低低點。"

        rows.append(
            {
                "dowSignal": signal,
                "dowSignalText": signal_text,
                "dowBias": bias,
                "dowBiasText": bias_text,
                "dowPrimaryTrend": primary_trend,
                "dowPrimaryTrendText": primary_text,
                "dowSecondaryTrend": secondary_trend,
                "dowSecondaryTrendText": secondary_text,
                "dowPhase": phase,
                "dowPhaseText": phase_text,
                "dowVolumeConfirm": volume_confirm,
                "dowVolumeConfirmText": volume_confirm_text,
                "dowReversalSignal": reversal_signal,
                "dowReversalText": reversal_text,
                "dowScore": max(0, min(100, round(score))),
                "dowReason": (
                    f"{structure_text} 次級趨勢為{secondary_text}，目前屬{phase_text}；"
                    f"{volume_confirm_text}。道氏理論重視趨勢延續，需等結構與成交量一起確認才視為反轉。"
                ),
            }
        )

    return rows


def row_to_dict(
    row,
    kd_row: dict[str, object],
    rsi_row: dict[str, object],
    macd_row: dict[str, object],
    structure_row: dict[str, object],
    volume_price_row: dict[str, object],
    box_row: dict[str, object],
    dow_row: dict[str, object],
) -> dict[str, object]:
    bar = row.bar
    payload = {
        "date": bar.date,
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": int(round(bar.volume)),
        "middle": maybe_float(row.middle),
        "upper": maybe_float(row.upper),
        "lower": maybe_float(row.lower),
        "bandwidthPct": maybe_float(row.bandwidth_pct),
        "volumeMa": int(round(row.volume_ma)) if row.volume_ma is not None else None,
        "volumeRatio": maybe_float(row.volume_ratio),
        "action": row.action,
        "signal": row.signal,
        "reason": row.reason,
        **translate_bollinger(row),
        **kd_row,
        **rsi_row,
        **macd_row,
        **structure_row,
        **volume_price_row,
        **box_row,
        **dow_row,
    }
    payload.update(calculate_reliability(payload))
    payload.update(calculate_trade_plan(payload))
    return payload


def analyze_from_query(params: dict[str, list[str]]) -> dict[str, object]:
    symbol = query_one(params, "symbol", "NVDA").upper()
    if not SYMBOL_RE.match(symbol):
        raise ValueError("Symbol can contain only letters, numbers, dot, dash, underscore, caret, equals.")

    data_range = query_one(params, "range", "6mo")
    last_count = clamp_int(query_one(params, "last", "5"), 5, 1, 30)
    band_period = clamp_int(query_one(params, "bandPeriod", "20"), 20, 2, 120)
    volume_period = clamp_int(query_one(params, "volumePeriod", "20"), 20, 2, 120)
    kd_period = clamp_int(query_one(params, "kdPeriod", "9"), 9, 2, 60)
    k_smoothing = clamp_int(query_one(params, "kSmoothing", "3"), 3, 1, 20)
    d_smoothing = clamp_int(query_one(params, "dSmoothing", "3"), 3, 1, 20)
    rsi_period = clamp_int(query_one(params, "rsiPeriod", "14"), 14, 2, 60)
    macd_fast = clamp_int(query_one(params, "macdFast", "12"), 12, 2, 120)
    macd_slow = clamp_int(query_one(params, "macdSlow", "26"), 26, 3, 180)
    macd_signal = clamp_int(query_one(params, "macdSignal", "9"), 9, 1, 60)
    std_multiplier = clamp_float(query_one(params, "stdMultiplier", "2.0"), 2.0, 0.5, 5.0)
    volume_multiplier = clamp_float(query_one(params, "volumeMultiplier", "1.5"), 1.5, 0.5, 10.0)
    if macd_fast >= macd_slow:
        macd_fast = max(2, macd_slow - 1)

    bars = STRATEGY.fetch_yahoo_bars(
        symbol=symbol,
        data_range=data_range,
        interval="1d",
    )
    rows = STRATEGY.analyze(
        bars=bars,
        band_period=band_period,
        std_multiplier=std_multiplier,
        volume_period=volume_period,
        volume_multiplier=volume_multiplier,
    )
    kd_rows = calculate_kd(
        bars=bars,
        period=kd_period,
        k_smoothing=k_smoothing,
        d_smoothing=d_smoothing,
    )
    rsi_rows = calculate_rsi(bars=bars, period=rsi_period)
    macd_rows = calculate_macd(
        bars=bars,
        fast_period=macd_fast,
        slow_period=macd_slow,
        signal_period=macd_signal,
    )
    structure_rows = calculate_market_structure(bars=bars)
    volume_price_rows = calculate_volume_price(
        bars=bars,
        symbol=symbol,
        volume_period=volume_period,
    )
    box_rows = calculate_box_structure(
        bars=bars,
        volume_period=volume_period,
    )
    dow_rows = calculate_dow_theory(
        bars=bars,
        volume_price_rows=volume_price_rows,
    )

    history_requirements = {
        "bollinger_volume": max(band_period, volume_period),
        "kd": kd_period,
        "rsi": rsi_period + 1,
        "macd": macd_slow,
        "structure": 20,
    }
    selected = []
    start_index = max(0, len(rows) - last_count)
    for offset, (
        row,
        kd_row,
        rsi_row,
        macd_row,
        structure_row,
        volume_price_row,
        box_row,
        dow_row,
    ) in enumerate(
        zip(
            rows[-last_count:],
            kd_rows[-last_count:],
            rsi_rows[-last_count:],
            macd_rows[-last_count:],
            structure_rows[-last_count:],
            volume_price_rows[-last_count:],
            box_rows[-last_count:],
            dow_rows[-last_count:],
        )
    ):
        payload = row_to_dict(row, kd_row, rsi_row, macd_row, structure_row, volume_price_row, box_row, dow_row)
        payload.update(calculate_history_context(bars, start_index + offset, history_requirements))
        selected.append(apply_short_history_overlay(payload))

    return {
        "symbol": symbol,
        "source": "Yahoo Finance",
        "range": data_range,
        "interval": "1d",
        "barsFetched": len(bars),
        "historyMode": selected[-1].get("historyMode") if selected else "NO_DATA",
        "historyRequirementText": selected[-1].get("historyRequirementText") if selected else "",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "bandPeriod": band_period,
            "stdMultiplier": std_multiplier,
            "volumePeriod": volume_period,
            "volumeMultiplier": volume_multiplier,
            "kdPeriod": kd_period,
            "kSmoothing": k_smoothing,
            "dSmoothing": d_smoothing,
            "rsiPeriod": rsi_period,
            "macdFast": macd_fast,
            "macdSlow": macd_slow,
            "macdSignal": macd_signal,
            "last": last_count,
        },
        "latest": selected[-1],
        "rows": selected,
        "riskNote": RISK_NOTE,
    }


def parse_symbols(value: str) -> list[str]:
    symbols: list[str] = []
    for raw_symbol in re.split(r"[\s,;]+", value.upper()):
        symbol = raw_symbol.strip()
        if not symbol:
            continue
        if SYMBOL_RE.match(symbol) and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def looks_like_common_equity(symbol: str, name: str) -> bool:
    lowered = name.lower()
    blocked_words = (
        "warrant",
        "right",
        "unit",
        "preferred",
        "preference",
        "note",
        "debenture",
        "bond",
        "etf",
        "etn",
        "fund",
        "index",
    )
    if any(word in lowered for word in blocked_words):
        return False
    if not SYMBOL_RE.match(symbol):
        return False
    if len(symbol) > 8:
        return False
    return True


def fetch_text(url: str) -> str:
    request = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 stock-analysis-screener"},
    )
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def cached_symbols(cache_key: str, force_refresh: bool) -> list[str] | None:
    cache = UNIVERSE_CACHE.setdefault(cache_key, {"loaded_at": None, "symbols": []})
    symbols = cache.get("symbols")
    loaded_at = cache.get("loaded_at")
    if (
        not force_refresh
        and isinstance(symbols, list)
        and symbols
        and isinstance(loaded_at, datetime)
        and (datetime.now(timezone.utc) - loaded_at).total_seconds() < 60 * 60 * 12
    ):
        return list(symbols)
    return None


def store_cached_symbols(cache_key: str, symbols: list[str]) -> list[str]:
    cache = UNIVERSE_CACHE.setdefault(cache_key, {"loaded_at": None, "symbols": []})
    cache["symbols"] = symbols
    cache["loaded_at"] = datetime.now(timezone.utc)
    return list(symbols)


def fetch_json_records(url: str) -> list[dict[str, object]]:
    payload = json.loads(fetch_text(url))
    if not isinstance(payload, list):
        raise ValueError("股票池資料格式不是 JSON 陣列。")
    return [record for record in payload if isinstance(record, dict)]


def fetch_us_market_symbols(force_refresh: bool = False) -> list[str]:
    cached = cached_symbols("us_all", force_refresh)
    if cached is not None:
        return cached

    symbols: list[str] = []

    nasdaq_text = fetch_text(NASDAQ_LISTED_URL)
    for line in nasdaq_text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 8 or parts[0] == "File Creation Time":
            continue
        symbol, name, _market, test_issue, _status, _round_lot, etf, _next_shares = parts[:8]
        symbol = symbol.strip().replace(".", "-")
        if test_issue != "N" or etf != "N":
            continue
        if looks_like_common_equity(symbol, name) and symbol not in symbols:
            symbols.append(symbol)

    other_text = fetch_text(OTHER_LISTED_URL)
    for line in other_text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) < 7 or parts[0] == "File Creation Time":
            continue
        symbol, name, _exchange, _cqs_symbol, etf, _round_lot, test_issue = parts[:7]
        symbol = symbol.strip().replace(".", "-")
        if test_issue != "N" or etf != "N":
            continue
        if looks_like_common_equity(symbol, name) and symbol not in symbols:
            symbols.append(symbol)

    symbols.sort()
    return store_cached_symbols("us_all", symbols)


def taiwan_code_from_record(record: dict[str, object], code_keys: tuple[str, ...]) -> str | None:
    for key in code_keys:
        value = record.get(key)
        if value is None:
            continue
        code = str(value).strip()
        if re.fullmatch(r"\d{4,6}", code):
            return code
    return None


def fetch_taiwan_market_symbols(market: str, force_refresh: bool = False) -> list[str]:
    if market == "listed":
        cache_key = "tw_listed"
        suffix = ".TW"
        url = TWSE_LISTED_URL
        code_keys = ("公司代號", "SecuritiesCompanyCode")
    elif market == "otc":
        cache_key = "tw_otc"
        suffix = ".TWO"
        url = TPEX_OTC_URL
        code_keys = ("SecuritiesCompanyCode", "公司代號")
    else:
        raise ValueError("不支援的台股市場。")

    cached = cached_symbols(cache_key, force_refresh)
    if cached is not None:
        return cached

    symbols: list[str] = []
    for record in fetch_json_records(url):
        code = taiwan_code_from_record(record, code_keys)
        if code:
            symbols.append(f"{code}{suffix}")

    symbols = sorted(set(symbols))
    return store_cached_symbols(cache_key, symbols)


def fetch_taiwan_all_symbols(force_refresh: bool = False) -> list[str]:
    cached = cached_symbols("tw_all", force_refresh)
    if cached is not None:
        return cached

    symbols = fetch_taiwan_market_symbols("listed", force_refresh) + fetch_taiwan_market_symbols("otc", force_refresh)
    return store_cached_symbols("tw_all", sorted(set(symbols)))


def strategy_score(row: dict[str, object], strategy: str) -> tuple[int, str, str]:
    reliability = int(row.get("reliabilityScore") or 0)
    bullish = int(row.get("bullishVotes") or 0)
    bearish = int(row.get("bearishVotes") or 0)
    action = row.get("action")
    plan_action = row.get("planAction")
    close = row.get("close")
    ma_short = row.get("maShort")
    ma_long = row.get("maLong")
    lower = row.get("lower")
    rsi = row.get("rsi")
    kd_k = row.get("kdK")
    kd_d = row.get("kdD")
    support_distance = row.get("supportDistancePct")
    volume_price_bias = row.get("volumePriceBias")
    volume_price_signal = row.get("volumePriceSignal")
    liquidity_signal = row.get("liquiditySignal")
    kd_divergence = row.get("kdDivergenceSignal")
    kd_saturation = row.get("kdSaturation")
    box_signal = row.get("boxSignal")
    box_bias = row.get("boxBias")
    box_quality = int(row.get("boxQualityScore") or 0)
    box_width = row.get("boxWidthPct")
    dow_signal = row.get("dowSignal")
    dow_bias = row.get("dowBias")
    dow_score = int(row.get("dowScore") or 0)
    dow_volume_confirm = row.get("dowVolumeConfirm")
    dow_reversal = row.get("dowReversalSignal")
    dow_phase = row.get("dowPhase")

    score = reliability
    match = "觀察"
    reason = str(row.get("reliabilityReason") or "")

    if strategy == "bullish_consensus":
        score += bullish * 10 - bearish * 14
        if action == "BUY" or plan_action == "LONG":
            score += 18
        if isinstance(close, (int, float)) and isinstance(ma_short, (int, float)) and close >= ma_short:
            score += 8
        if volume_price_bias == "BULLISH":
            score += 8
        if volume_price_bias == "CAUTION":
            score -= 6
        if kd_divergence == "BEARISH_DIVERGENCE":
            score -= 10
        if box_bias == "BULLISH":
            score += 10
        if box_signal in {"UPPER_FALSE_BREAKOUT", "BOX_TOO_WIDE"}:
            score -= 8
        match = "偏多共振" if bullish > bearish else "未形成偏多共振"
        reason = f"{bullish} 個偏多、{bearish} 個偏空；優先找多指標同向且可靠度高的標的。"
    elif strategy == "oversold_rebound":
        oversold = 0
        if action == "WAIT_CONFIRMATION" or action == "BUY":
            oversold += 1
        if isinstance(rsi, (int, float)) and rsi <= 45:
            oversold += 1
        if isinstance(kd_k, (int, float)) and isinstance(kd_d, (int, float)) and kd_k <= 30 and kd_k <= kd_d:
            oversold += 1
        if isinstance(close, (int, float)) and isinstance(lower, (int, float)) and close <= lower * 1.03:
            oversold += 1
        if volume_price_signal == "DOWN_UP":
            oversold += 1
        if kd_divergence == "BULLISH_DIVERGENCE":
            oversold += 1
        score = reliability + oversold * 15 - bearish * 4
        match = "低檔反彈觀察" if oversold >= 2 else "反彈條件不足"
        reason = f"{oversold} 個低檔/反彈條件成立；價跌量增只代表可能有承接或賣壓，仍需等 KD/RSI 或收盤轉強確認。"
    elif strategy == "trend_pullback":
        score += 8 if row.get("trendBias") == "BULLISH" else -10
        if volume_price_bias == "BULLISH":
            score += 8
        if volume_price_bias == "CAUTION":
            score -= 5
        if kd_saturation == "HIGH_SATURATION" and kd_divergence != "BEARISH_DIVERGENCE":
            score += 4
        if kd_divergence == "BEARISH_DIVERGENCE":
            score -= 10
        if isinstance(support_distance, (int, float)) and 0 <= support_distance <= 5:
            score += 12
        if isinstance(close, (int, float)) and isinstance(ma_short, (int, float)) and isinstance(ma_long, (int, float)):
            if close >= ma_long and close <= ma_short * 1.03:
                score += 10
        if box_signal == "BOX_NEAR_BOTTOM":
            score += 6
        match = "多頭回檔觀察" if score >= 70 else "回檔條件不足"
        reason = "優先找長線趨勢未破、價格靠近支撐或短均線的回檔標的。"
    elif strategy == "box_breakout":
        score = round(box_quality * 0.55)
        if box_signal == "BULLISH_BOX_BREAKOUT":
            score += 30
            match = "箱頂放量突破"
        elif box_signal == "BULLISH_BOX_BREAKOUT_WEAK_VOLUME":
            score += 18
            match = "箱頂突破待量能確認"
        elif box_signal == "BOX_NEAR_TOP":
            score += 14
            match = "接近箱頂觀察"
        elif box_signal == "BOX_RANGE":
            score += 5
            match = "箱內整理"
        elif box_signal in {"UPPER_FALSE_BREAKOUT", "BOX_TOO_WIDE", "BEARISH_BOX_BREAKDOWN"}:
            score -= 18
            match = "箱型風險偏高"
        else:
            match = "箱型條件不足"
        if row.get("trendBias") == "BULLISH":
            score += 16
        if reliability >= 70:
            score += 8
        if volume_price_bias == "BULLISH":
            score += 8
        if isinstance(box_width, (int, float)) and box_width > 25:
            score -= 12
        reason = (
            f"箱型品質 {box_quality}/100；優先找上升趨勢中箱體較窄、持續時間較久、"
            "且突破箱頂時有成交量確認的標的。"
        )
    elif strategy == "dow_trend_follow":
        score = round(dow_score * 0.65) + round(reliability * 0.25)
        if dow_signal == "DOW_UPTREND_CONFIRMED":
            score += 18
            match = "道氏主要上升趨勢"
        elif dow_signal == "DOW_UPTREND_UNCONFIRMED":
            score += 8
            match = "上升趨勢待確認"
        elif dow_signal == "DOW_SIDEWAYS":
            score -= 8
            match = "趨勢不明"
        elif dow_bias == "BEARISH":
            score -= 20
            match = "道氏趨勢偏空"
        else:
            match = "道氏條件不足"
        if dow_volume_confirm == "CONFIRMED":
            score += 8
        elif dow_volume_confirm == "DIVERGENT":
            score -= 10
        if dow_reversal in {"REVERSAL_DOWN_RISK", "REVERSAL_UP_RISK"}:
            score -= 12
        if dow_phase == "EXCESS":
            score -= 8
        if volume_price_bias == "BULLISH":
            score += 5
        reason = (
            f"道氏分數 {dow_score}/100；優先找主要趨勢偏多、次級趨勢未破、"
            "且成交量順著主要趨勢確認的標的。"
        )
    elif strategy == "avoid_weakness":
        score = reliability + bearish * 12 - bullish * 8
        if row.get("trendBias") == "BEARISH":
            score += 12
        if volume_price_bias == "CAUTION":
            score += 8
        if kd_divergence == "BEARISH_DIVERGENCE":
            score += 10
        if kd_saturation == "LOW_SATURATION":
            score += 5
        if box_signal in {"BEARISH_BOX_BREAKDOWN", "UPPER_FALSE_BREAKOUT", "BOX_TOO_WIDE"}:
            score += 10
        if dow_bias == "BEARISH":
            score += 10
        if dow_reversal == "REVERSAL_DOWN_RISK":
            score += 10
        if liquidity_signal == "LOW_LIQUIDITY":
            score += 12
        if action in {"SELL", "WAIT_CONFIRMATION"}:
            score += 10
        match = "風險偏高" if bearish >= 2 else "風險普通"
        reason = f"{bearish} 個偏空、{bullish} 個偏多；此策略用來找應避開或降風險的標的。"
    else:
        score += (bullish - bearish) * 8
        match = str(row.get("consensusText") or "綜合排序")

    if liquidity_signal == "LOW_LIQUIDITY" and strategy != "avoid_weakness":
        score -= 12
    elif liquidity_signal == "THIN_LIQUIDITY" and strategy != "avoid_weakness":
        score -= 4

    return max(0, min(100, round(score))), match, reason


def build_screen_candidate(symbol: str, data_range: str, strategy: str) -> dict[str, object]:
    analysis = analyze_from_query(
        {
            "symbol": [symbol],
            "range": [data_range],
            "last": ["1"],
        }
    )
    latest = dict(analysis["latest"])
    score, match, reason = strategy_score(latest, strategy)
    latest.update(
        {
            "symbol": symbol,
            "strategyScore": score,
            "strategyMatchText": match,
            "strategyReason": reason,
        }
    )
    return latest


def screen_from_query(params: dict[str, list[str]]) -> dict[str, object]:
    strategy = query_one(params, "strategy", "bullish_consensus")
    data_range = query_one(params, "range", "6mo")
    universe = query_one(params, "universe", "custom")
    symbols_value = query_one(params, "symbols", DEFAULT_SCREEN_SYMBOLS)
    limit = clamp_int(query_one(params, "limit", "10"), 10, 1, 30)
    max_symbols = clamp_int(query_one(params, "maxSymbols", "100"), 100, 1, 8000)
    workers = clamp_int(query_one(params, "workers", "8"), 8, 1, 16)
    force_refresh = query_one(params, "refreshUniverse", "0") == "1"
    if universe == "us_all":
        symbols = fetch_us_market_symbols(force_refresh=force_refresh)
    elif universe == "tw_listed":
        symbols = fetch_taiwan_market_symbols("listed", force_refresh=force_refresh)
    elif universe == "tw_otc":
        symbols = fetch_taiwan_market_symbols("otc", force_refresh=force_refresh)
    elif universe == "tw_all":
        symbols = fetch_taiwan_all_symbols(force_refresh=force_refresh)
    else:
        symbols = parse_symbols(symbols_value)
    total_universe = len(symbols)
    symbols = symbols[:max_symbols]
    if not symbols:
        raise ValueError("請至少輸入一個有效股票代號。")

    candidates: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as executor:
        future_to_symbol = {
            executor.submit(build_screen_candidate, symbol, data_range, strategy): symbol for symbol in symbols
        }
        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                candidates.append(future.result())
            except Exception as exc:  # noqa: BLE001 - one bad symbol should not stop the screen.
                errors.append({"symbol": symbol, "error": str(exc)})

    candidates.sort(key=lambda item: (-(item.get("strategyScore") or 0), str(item.get("symbol") or "")))
    return {
        "strategy": strategy,
        "universe": universe,
        "range": data_range,
        "workers": workers,
        "totalUniverse": total_universe,
        "symbolsScanned": len(symbols),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "rows": candidates[:limit],
        "errors": errors,
        "riskNote": RISK_NOTE,
    }


class AppHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".webmanifest": "application/manifest+json",
        ".js": "text/javascript",
        ".css": "text/css",
        ".png": "image/png",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/analyze":
            self.handle_analyze(parsed.query)
            return
        if parsed.path == "/api/screen":
            self.handle_screen(parsed.query)
            return
        if parsed.path == "/health":
            self.send_json({"ok": True})
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def handle_analyze(self, query: str) -> None:
        try:
            payload = analyze_from_query(parse_qs(query))
            self.send_json(payload)
        except Exception as exc:  # noqa: BLE001 - API should return concise errors.
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)

    def handle_screen(self, query: str) -> None:
        try:
            payload = screen_from_query(parse_qs(query))
            self.send_json(payload)
        except Exception as exc:  # noqa: BLE001 - API should return concise errors.
            self.send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    import argparse

    default_host = os.environ.get("HOST", "127.0.0.1")
    default_port = int(os.environ.get("PORT", "8765"))

    parser = argparse.ArgumentParser(description="Run the Stock analysis web app.")
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Stock analysis app running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
