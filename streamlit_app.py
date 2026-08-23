"""Pump Scanner — manual-first decision support for Solana Pump.fun tokens."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from scanner import (
    Rules,
    ScanResult,
    ScannerError,
    analyze_bot_trades,
    discover_live_mints,
    discover_recent_mints,
    discover_top_movers,
    scan_token,
)

st.set_page_config(page_title="Pump Scanner", page_icon=":material/radar:", layout="wide")

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
POSITIONS_FILE = DATA_DIR / "positions.json"

DEFAULT_WATCHLIST = [
    "8okX9n4GJYcUpRSCmZk8UU35T1y3cKTFBezHAAa35KPP",
    "26w7qBovfKv4zprC7wmPQx9McchzdzNoZYz7SXpupump",
    "Ce2gx9KGXJ6C9Mp5b5x1sn9Mg87JwEbrQby4Zqo3pump",
]
DEFAULT_POSITIONS = []


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


st.session_state.setdefault("watchlist", load_json(WATCHLIST_FILE, DEFAULT_WATCHLIST))
st.session_state.setdefault("positions", load_json(POSITIONS_FILE, DEFAULT_POSITIONS))
st.session_state.setdefault("last_scan", None)
st.session_state.setdefault("last_auto_scan_at", 0.0)
st.session_state.setdefault("last_auto_scan_at_by_profile", {})


@st.cache_data(ttl="30s", max_entries=100, show_spinner=False)
def cached_scan(mint: str, rules_payload: dict, manual_payload: dict) -> dict:
    result = scan_token(mint, Rules(**rules_payload), manual=manual_payload)
    return result.to_dict()


@st.cache_data(ttl="20s", max_entries=10, show_spinner=False)
def cached_recent_mints(limit: int, sort_by: str) -> list[dict]:
    return discover_recent_mints(limit, sort_by)


@st.cache_data(ttl="30s", max_entries=5, show_spinner=False)
def cached_top_movers(limit: int) -> list[dict]:
    return discover_top_movers(limit)


@st.cache_data(ttl="10s", max_entries=5, show_spinner=False)
def cached_live_mints(limit: int) -> list[dict]:
    return discover_live_mints(limit)


@st.cache_data(ttl="30s", max_entries=150, show_spinner=False)
def cached_quick_scan(coin_payload: dict, rules_payload: dict) -> dict:
    quick_rules = Rules(**rules_payload)
    # Discovery profiles either show holders for context or apply their own
    # explicit range. The sidebar minimum remains a deep-scan guardrail.
    quick_rules.minimum_holder_count = 0
    result = scan_token(
        coin_payload["mint"],
        quick_rules,
        coin_data=coin_payload,
        include_onchain=False,
    )
    return result.to_dict()


@st.cache_data(ttl="30s", max_entries=20, show_spinner=False)
def cached_bot_trade_samples(mints: tuple[str, ...]) -> dict[str, dict]:
    """Fetch raw trade samples only for the aggregate BOT-trades shortlist."""
    if not mints:
        return {}

    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(5, len(mints))) as executor:
        futures = {executor.submit(analyze_bot_trades, mint): mint for mint in mints}
        for future in as_completed(futures):
            mint = futures[future]
            try:
                results[mint] = future.result()
            except (ScannerError, TypeError, ValueError) as exc:
                results[mint] = {
                    "bot_data_status": "UNAVAILABLE",
                    "bot_data_error": str(exc),
                }
    return results


@st.fragment(run_every="1m")
def auto_scan_scheduler(interval_minutes: int, profile_key: str) -> None:
    """Use a lightweight one-minute timer to trigger the selected sweep cadence."""
    if interval_minutes <= 0:
        st.caption(":gray-badge[Automatic sweep off]")
        return

    now = time.time()
    timestamps = dict(st.session_state.last_auto_scan_at_by_profile)
    last_run = float(timestamps.get(profile_key, 0.0) or 0.0)
    if last_run <= 0:
        timestamps[profile_key] = now
        st.session_state.last_auto_scan_at_by_profile = timestamps
        last_run = now
    remaining_seconds = interval_minutes * 60 - (now - last_run)
    if remaining_seconds <= 0:
        timestamps[profile_key] = now
        st.session_state.last_auto_scan_at_by_profile = timestamps
        st.rerun()

    remaining_minutes = max(1, int((remaining_seconds + 59) // 60))
    st.caption(
        f":green-badge[Automatic sweep every {interval_minutes} min] "
        f"Next sweep in about {remaining_minutes} min. Keep Discover open."
    )


def money(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1f}%"


def age_label(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    if value < 60:
        return f"{value:.0f}m"
    if value < 1_440:
        return f"{int(value // 60)}h {int(value % 60)}m"
    return f"{int(value // 1_440)}d {int((value % 1_440) // 60)}h"


def pump_viewer_count_label(is_live: object, viewers: object) -> str:
    """Format Pump's active livestream viewer count for the detail view."""
    if is_live is None or pd.isna(is_live) or not bool(is_live):
        return "Not live"
    if viewers is None or pd.isna(viewers):
        return "Unavailable"
    return f"{int(viewers):,}"


def numeric_value(value: object) -> float | None:
    """Return a finite scalar without letting missing dataframe values leak through."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def band_fit(
    value: float,
    outer_low: float,
    ideal_low: float,
    ideal_high: float,
    outer_high: float,
) -> float:
    """Score 0–1 inside a preferred band, tapering toward the outer limits."""
    if value < outer_low or value > outer_high:
        return 0.0
    if ideal_low <= value <= ideal_high:
        return 1.0
    if value < ideal_low:
        width = max(ideal_low - outer_low, 1e-9)
        return max(0.0, min(1.0, (value - outer_low) / width))
    width = max(outer_high - ideal_high, 1e-9)
    return max(0.0, min(1.0, (outer_high - value) / width))


def preferred_band(
    outer_low: float,
    preferred_low: float,
    preferred_high: float,
    outer_high: float,
) -> tuple[float, float]:
    """Clamp a preferred scoring band inside user-selected outer limits."""
    ideal_low = max(outer_low, min(preferred_low, outer_high))
    ideal_high = max(ideal_low, min(preferred_high, outer_high))
    return ideal_low, ideal_high


def early_runner_metrics(row: dict[str, object], settings: dict[str, float]) -> dict[str, object]:
    """Measure low-cap momentum, turnover, and buy pressure for an early-runner row."""
    market_cap = numeric_value(row.get("market_cap_usd"))
    age = numeric_value(row.get("age_minutes"))
    move_5m = numeric_value(row.get("price_change_5m"))
    move_1h = numeric_value(row.get("price_change_1h"))
    volume = numeric_value(row.get("volume_5m"))
    buys = numeric_value(row.get("buys_5m"))
    sells = numeric_value(row.get("sells_5m"))
    required = (market_cap, age, move_5m, volume, buys, sells)
    if any(value is None for value in required) or market_cap <= 0:
        return {
            "early_score": None,
            "early_signal": "NEEDS DATA",
            "buy_share_pct": None,
            "volume_to_cap": None,
            "trade_count": None,
        }

    trade_count = buys + sells
    buy_share = buys / trade_count if trade_count > 0 else 0.0
    turnover = volume / market_cap

    cap_ideal = preferred_band(
        settings["minimum_market_cap"],
        10_000.0,
        20_000.0,
        settings["maximum_market_cap"],
    )
    age_ideal = preferred_band(
        settings["minimum_age"],
        20.0,
        75.0,
        settings["maximum_age"],
    )
    momentum_ideal = preferred_band(
        settings["minimum_momentum"],
        15.0,
        50.0,
        settings["maximum_momentum"],
    )

    cap_fit = band_fit(
        market_cap,
        settings["minimum_market_cap"],
        cap_ideal[0],
        cap_ideal[1],
        settings["maximum_market_cap"],
    )
    age_fit = band_fit(
        age,
        settings["minimum_age"],
        age_ideal[0],
        age_ideal[1],
        settings["maximum_age"],
    )
    momentum_fit = band_fit(
        move_5m,
        settings["minimum_momentum"],
        momentum_ideal[0],
        momentum_ideal[1],
        settings["maximum_momentum"],
    )
    if move_1h is None:
        one_hour_fit = 0.5 if age < 60 else 0.0
    else:
        one_hour_fit = band_fit(move_1h, -10.0, 15.0, 120.0, 250.0)

    minimum_turnover = settings.get("minimum_turnover", 0.12)
    target_turnover = max(0.30, minimum_turnover * 2.0)
    turnover_fit = max(
        0.0,
        min(1.0, (turnover - minimum_turnover) / max(target_turnover - minimum_turnover, 1e-9)),
    )
    buy_pressure_fit = max(0.0, min(1.0, (buy_share - 0.50) / 0.20))
    buy_pressure_fit *= min(1.0, trade_count / 75.0)
    activity_fit = max(0.0, min(1.0, (trade_count - 25.0) / 75.0))
    score = (
        25.0 * cap_fit
        + 10.0 * age_fit
        + 15.0 * momentum_fit
        + 10.0 * one_hour_fit
        + 15.0 * turnover_fit
        + 15.0 * buy_pressure_fit
        + 10.0 * activity_fit
    )
    if move_5m > 65.0:
        score -= min(10.0, (move_5m - 65.0) / 35.0 * 10.0)
    if turnover > 2.0:
        score -= 10.0
    if bool(row.get("complete")):
        score -= 10.0

    score = round(max(0.0, min(100.0, score)), 1)
    extended = move_5m > 65.0 or turnover > 2.0
    signal = (
        "EXTENDED"
        if extended
        else "HIGH MATCH"
        if score >= 70 and trade_count >= 50
        else "WATCH"
        if score >= 55
        else "LOW"
    )
    return {
        "early_score": score,
        "early_signal": signal,
        "buy_share_pct": round(buy_share * 100.0, 1),
        "volume_to_cap": round(turnover, 2),
        "trade_count": int(trade_count),
    }


def top_mover_metrics(row: dict[str, object]) -> dict[str, object]:
    """Rank current dollar flow and participation ahead of price momentum."""
    move_5m = numeric_value(row.get("price_change_5m"))
    volume_5m = numeric_value(row.get("volume_5m"))
    volume_1h = numeric_value(row.get("volume_1h"))
    buys_5m = numeric_value(row.get("buys_5m"))
    sells_5m = numeric_value(row.get("sells_5m"))
    required = (move_5m, volume_5m, volume_1h, buys_5m, sells_5m)
    if (
        any(value is None for value in required)
        or volume_5m < 0
        or volume_1h < 0
        or buys_5m < 0
        or sells_5m < 0
    ):
        return {
            "mover_score": None,
            "trades_5m": None,
            "trades_per_minute": None,
            "average_trade_usd": None,
        }

    trades_5m = buys_5m + sells_5m
    recent_volume_fit = min(1.0, math.log1p(volume_5m) / math.log1p(250_000.0))
    hourly_volume_fit = min(1.0, math.log1p(volume_1h) / math.log1p(1_000_000.0))
    activity_fit = min(1.0, math.log1p(trades_5m) / math.log1p(1_000.0))
    momentum_fit = max(0.0, min(1.0, (move_5m + 10.0) / 110.0))
    score = (
        45.0 * recent_volume_fit
        + 20.0 * hourly_volume_fit
        + 25.0 * activity_fit
        + 10.0 * momentum_fit
    )
    return {
        "mover_score": round(max(0.0, min(100.0, score)), 1),
        "trades_5m": int(trades_5m),
        "trades_per_minute": round(trades_5m / 5.0, 1),
        "average_trade_usd": (
            round(volume_5m / trades_5m, 2) if trades_5m > 0 else None
        ),
    }


def bot_trade_metrics(
    row: dict[str, object],
    settings: dict[str, float],
    raw_sample: dict[str, object] | None = None,
) -> dict[str, object]:
    """Rank automation-like activity without claiming that a wallet is definitively a bot."""
    market_cap = numeric_value(row.get("market_cap_usd"))
    liquidity = numeric_value(row.get("liquidity_usd"))
    age = numeric_value(row.get("age_minutes"))
    volume_5m = numeric_value(row.get("volume_5m"))
    buys_5m = numeric_value(row.get("buys_5m"))
    sells_5m = numeric_value(row.get("sells_5m"))
    buys_1h = numeric_value(row.get("buys_1h"))
    sells_1h = numeric_value(row.get("sells_1h"))
    required = (market_cap, age, volume_5m, buys_5m, sells_5m, buys_1h, sells_1h)
    if any(value is None for value in required) or market_cap <= 0:
        return {
            "bot_score": None,
            "bot_signal": "NEEDS DATA",
            "bot_flags": "Missing aggregate market data",
        }

    trades_5m = buys_5m + sells_5m
    trades_1h = buys_1h + sells_1h
    if trades_5m <= 0 or trades_1h < trades_5m:
        return {
            "bot_score": None,
            "bot_signal": "NEEDS DATA",
            "bot_flags": "Inconsistent trade windows",
        }

    observed_minutes = max(5.0, min(age, 60.0))
    trades_per_minute = trades_5m / 5.0
    sustained_trades_per_minute = trades_1h / observed_minutes
    buy_share = buys_5m / trades_5m
    average_trade_usd = volume_5m / trades_5m
    liquidity_to_cap = liquidity / market_cap if liquidity is not None else None

    raw_sample = raw_sample or {}
    raw_status = str(raw_sample.get("bot_data_status") or "UNAVAILABLE")
    raw_trade_count = numeric_value(raw_sample.get("raw_trade_count_1m"))
    raw_buy_count = numeric_value(raw_sample.get("raw_buys_1m"))
    raw_buy_share = numeric_value(raw_sample.get("raw_buy_share_pct"))
    median_buy = numeric_value(raw_sample.get("median_buy_usd"))
    micro_share = numeric_value(raw_sample.get("micro_buy_share_pct"))
    top_wallet_share = numeric_value(raw_sample.get("top_wallet_buy_share_pct"))
    top3_share = numeric_value(raw_sample.get("top3_wallet_buy_share_pct"))
    raw_flow_qualifies = bool(
        raw_status == "SAMPLED"
        and raw_trade_count is not None
        and raw_trade_count >= settings["minimum_raw_trades"]
        and raw_buy_count is not None
        and raw_buy_count >= settings["minimum_raw_buys"]
        and raw_buy_share is not None
        and raw_buy_share >= settings["minimum_buy_share"]
    )

    recent_rate_fit = min(1.0, trades_per_minute / 60.0)
    sustained_rate_fit = min(1.0, sustained_trades_per_minute / 50.0)
    buy_direction_fit = max(0.0, min(1.0, (buy_share - 0.50) / 0.47))
    micro_fit = (
        max(0.0, min(1.0, (micro_share - 25.0) / 50.0))
        if raw_flow_qualifies and micro_share is not None else 0.0
    )
    wallet_fit = (
        max(0.0, min(1.0, (top_wallet_share - 25.0) / 70.0))
        if raw_flow_qualifies and top_wallet_share is not None else 0.0
    )
    top3_fit = (
        max(0.0, min(1.0, (top3_share - 50.0) / 50.0))
        if raw_flow_qualifies and top3_share is not None else 0.0
    )
    liquidity_gap_fit = (
        max(0.0, min(1.0, 1.0 - liquidity_to_cap / 0.03))
        if market_cap >= 1_000_000 and liquidity_to_cap is not None else 0.0
    )

    score = round(max(0.0, min(100.0, (
        20.0 * recent_rate_fit
        + 10.0 * sustained_rate_fit
        + 10.0 * buy_direction_fit
        + 20.0 * micro_fit
        + 25.0 * wallet_fit
        + 5.0 * top3_fit
        + 10.0 * liquidity_gap_fit
    ))), 1)

    flags: list[str] = []
    micro_swarm = bool(
        raw_flow_qualifies
        and median_buy is not None
        and median_buy <= settings["maximum_median_buy"]
        and micro_share is not None
        and micro_share >= settings["minimum_micro_share"]
    )
    concentrated_usd = bool(
        raw_flow_qualifies
        and top_wallet_share is not None
        and top_wallet_share >= settings["minimum_top_wallet_share"]
    )
    if micro_swarm:
        flags.append("MICRO-BUY SWARM")
    if concentrated_usd:
        flags.append("WALLET-USD CONCENTRATION")
    if buy_share * 100.0 >= settings["minimum_buy_share"]:
        flags.append("BUY-SIDE IMBALANCE")
    if market_cap >= 1_000_000 and liquidity_to_cap is not None and liquidity_to_cap < 0.03:
        flags.append("THIN MC/LIQUIDITY")
    if bool(raw_sample.get("raw_sample_truncated")):
        flags.append("100+ RAW TRADES/MIN")

    if raw_status != "SAMPLED":
        signal = "AGGREGATE ONLY"
    elif micro_swarm and concentrated_usd:
        signal = "BOT-LIKE RISK"
    elif score >= 70:
        signal = "AUTOMATION-LIKE"
    elif score >= 55:
        signal = "BOT WATCH"
    else:
        signal = "LOW"

    return {
        "bot_score": score,
        "bot_signal": signal,
        "bot_flags": " · ".join(flags) or "No strong raw-trade flag",
        "trades_per_minute": round(trades_per_minute, 1),
        "sustained_trades_per_minute": round(sustained_trades_per_minute, 1),
        "bot_buy_share_pct": round(buy_share * 100.0, 1),
        "average_trade_usd": round(average_trade_usd, 2),
        "median_buy_usd": median_buy,
        "micro_buy_share_pct": micro_share,
        "top_wallet_buy_share_pct": top_wallet_share,
        "top3_wallet_buy_share_pct": top3_share,
        "liquidity_to_cap_pct": (
            round(liquidity_to_cap * 100.0, 2) if liquidity_to_cap is not None else None
        ),
        "bot_sample_status": raw_status,
    }


def verdict_badge(verdict: str) -> str:
    colors = {
        "ENTRY ELIGIBLE": "green",
        "WATCH": "orange",
        "PASS": "red",
        "HARD PASS": "red",
    }
    return f":{colors.get(verdict, 'gray')}-badge[{verdict}]"


with st.sidebar:
    st.markdown("## :material/tune: Screening rules")
    minimum_age = st.number_input("Minimum age (minutes)", 0.0, 1_440.0, 15.0, 5.0)
    minimum_liquidity = st.number_input("Minimum graduated liquidity ($)", 0.0, 10_000_000.0, 15_000.0, 5_000.0)
    maximum_top10 = st.number_input("Maximum raw top 10 (%)", 0.0, 100.0, 30.0, 1.0)
    maximum_creator = st.number_input("Maximum creator (%)", 0.0, 100.0, 10.0, 1.0)
    maximum_bundler = st.number_input("Maximum bundlers (%)", 0.0, 100.0, 5.0, 1.0)
    maximum_5m = st.number_input("Maximum five-minute rise (%)", 0.0, 10_000.0, 35.0, 5.0)
    minimum_holders = st.number_input(
        "Deep-scan minimum holders",
        0,
        1_000_000,
        100,
        25,
        help="Applies to Deep scan, Watchlist, and Positions. Early runners has its own holder window.",
    )
    reject_modes = st.pills(
        "Reject detected launch modes",
        ["BOOST", "Mayhem"],
        default=["BOOST", "Mayhem"],
        selection_mode="multi",
        help="The quick market feed may not report a launch mode. Deep scan and verify Pump's audit panel.",
    )
    st.caption("Rules are guardrails, not guarantees. The scanner never places trades.")

rules = Rules(
    minimum_age_minutes=minimum_age,
    minimum_liquidity_usd=minimum_liquidity,
    maximum_top10_percent=maximum_top10,
    maximum_creator_percent=maximum_creator,
    maximum_bundler_percent=maximum_bundler,
    maximum_five_minute_change=maximum_5m,
    minimum_holder_count=int(minimum_holders),
    reject_boost="BOOST" in (reject_modes or []),
    reject_mayhem="Mayhem" in (reject_modes or []),
)

with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
    st.markdown("# :material/radar: Pump Scanner")
    if st.button("Refresh live data", icon=":material/refresh:", type="tertiary"):
        st.cache_data.clear()
        st.rerun()
st.caption("Automatically discovers fresh Pump.fun mints, then lets you deep-scan the candidates worth inspecting.")

discover_tab, scan_tab, watch_tab, positions_tab = st.tabs(
    ["Discover", "Deep scan", "Watchlist", "Positions"], on_change="rerun"
)

if discover_tab.open:
    with discover_tab:
        st.markdown("## Token discovery")
        st.caption(
            "Scan fresh launches, top movers, or active Pump livestreams. The quick pass uses Pump's "
            "indexed holder total; run a deep scan for concentration checks before treating any candidate as entry-eligible."
        )
        scan_profile = st.segmented_control(
            "Ranking profile",
            ["Early runners", "BOT trades", "Aggressive", "Top movers", "Live now", "Risk-first"],
            default="Early runners",
            key="scan_profile_v5",
            help="BOT trades flags automation-like activity and manipulation risk; it does not confirm wallet ownership.",
        )
        early_ungraduated_only = True
        early_settings = {
            "minimum_market_cap": 8_000.0,
            "maximum_market_cap": 30_000.0,
            "minimum_age": 15.0,
            "maximum_age": 120.0,
            "minimum_momentum": 5.0,
            "maximum_momentum": 800.0,
            "minimum_volume": 1_000.0,
            "minimum_turnover": 0.12,
            "minimum_buy_ratio": 1.40,
            "minimum_trades": 25.0,
            "minimum_holders": 1.0,
            "maximum_holders": 1_000.0,
        }
        bot_settings = {
            "minimum_market_cap": 5_000.0,
            "maximum_market_cap": 150_000_000.0,
            "minimum_age": 0.0,
            "maximum_age": 360.0,
            "minimum_5m_trades": 200.0,
            "minimum_buy_share": 80.0,
            "minimum_raw_trades": 40.0,
            "minimum_raw_buys": 30.0,
            "maximum_median_buy": 0.25,
            "minimum_micro_share": 50.0,
            "minimum_top_wallet_share": 50.0,
            "raw_shortlist_limit": 10.0,
        }
        top_mover_settings = {
            "minimum_momentum": 0.0,
            "minimum_volume_5m": 1_000.0,
            "minimum_volume_1h": 10_000.0,
            "minimum_trades_5m": 10.0,
        }
        with st.container(horizontal=True, vertical_alignment="bottom"):
            discovery_limit = st.select_slider(
                "Mints per sweep",
                options=[10, 20, 30, 40, 50],
                value=50,
                key="discovery_limit_v2",
            )
            if scan_profile == "Early runners":
                early_ungraduated_only = st.toggle(
                    "Ungraduated only",
                    value=True,
                    help="Matches the pre-graduation stage of the example runner.",
                )
                graduated_only = False
            elif scan_profile == "BOT trades":
                graduated_only = st.toggle(
                    "Graduated only",
                    value=True,
                    key="bot_graduated_only_v1",
                    help="The NTDA pattern graduated immediately; turn this off to inspect pre-graduation swarms too.",
                )
            else:
                graduated_only = st.toggle("Graduated only", value=False)
            auto_scan_label = st.selectbox(
                "Automatic sweep",
                ["Off"] + [f"Every {minute} min" for minute in range(1, 16)],
                index=1 if scan_profile in {"Early runners", "BOT trades"} else 0,
                key=f"auto_scan_label_{scan_profile.lower().replace(' ', '_')}_v3",
                help="Runs while the Discover view remains open.",
            )
            if scan_profile == "Aggressive":
                minimum_momentum = st.number_input(
                    "Minimum 5m move (%)",
                    -50.0,
                    500.0,
                    0.0,
                    1.0,
                    key=f"minimum_momentum_{scan_profile}",
                )
                minimum_volume = st.number_input(
                    "Minimum 24h volume ($)",
                    0.0,
                    10_000_000.0,
                    250.0,
                    250.0,
                    key=f"minimum_volume_{scan_profile}",
                )
            else:
                minimum_momentum = 0.0
                minimum_volume = 0.0
            if st.button("Sweep now", icon=":material/radar:", type="primary"):
                timestamps = dict(st.session_state.last_auto_scan_at_by_profile)
                timestamps[scan_profile] = time.time()
                st.session_state.last_auto_scan_at_by_profile = timestamps
                st.cache_data.clear()
                st.rerun()

        if scan_profile == "Top movers":
            with st.expander("Top-mover activity filters", expanded=True):
                mover_cols = st.columns(4)
                with mover_cols[0]:
                    top_minimum_momentum = st.number_input(
                        "Minimum 5m move (%)",
                        -50.0,
                        500.0,
                        0.0,
                        1.0,
                        key="top_minimum_momentum_v1",
                    )
                with mover_cols[1]:
                    top_minimum_volume_5m = st.number_input(
                        "Minimum 5m volume ($)",
                        min_value=0.0,
                        max_value=10_000_000.0,
                        value=1_000.0,
                        step=500.0,
                        key="top_minimum_volume_5m_v1",
                        help="Recent dollar volume, not lifetime or 24-hour volume.",
                    )
                with mover_cols[2]:
                    top_minimum_volume_1h = st.number_input(
                        "Minimum 1h volume ($)",
                        min_value=0.0,
                        max_value=100_000_000.0,
                        value=10_000.0,
                        step=2_500.0,
                        key="top_minimum_volume_1h_v1",
                        help="Requires sustained recent dollar flow instead of a single five-minute burst.",
                    )
                with mover_cols[3]:
                    top_minimum_trades_5m = st.number_input(
                        "Minimum trades in 5m",
                        min_value=0,
                        value=10,
                        step=5,
                        key="top_minimum_trades_5m_v1",
                        help="Total buys plus sells reported during the latest five-minute window.",
                    )
                top_mover_settings.update({
                    "minimum_momentum": float(top_minimum_momentum),
                    "minimum_volume_5m": float(top_minimum_volume_5m),
                    "minimum_volume_1h": float(top_minimum_volume_1h),
                    "minimum_trades_5m": float(top_minimum_trades_5m),
                })

        if scan_profile == "Early runners":
            with st.expander("Early-runner filters", expanded=True):
                cap_range = st.slider(
                    "Market-cap window ($)",
                    3_000,
                    100_000,
                    (8_000, 30_000),
                    1_000,
                )
                age_range = st.slider(
                    "Token-age window (minutes)",
                    1,
                    360,
                    (15, 120),
                    1,
                )
                momentum_range = st.slider(
                    "5-minute move window (%)",
                    -20.0,
                    800.0,
                    (5.0, 800.0),
                    1.0,
                    key="early_momentum_range_v2",
                )
                holder_range = st.slider(
                    "Holder-count window",
                    1,
                    1_000,
                    (1, 1_000),
                    1,
                    key="early_holder_range_v1",
                    help="Inclusive Pump.fun holder count. Tokens with an unavailable count are excluded.",
                )
                filter_cols = st.columns(4)
                with filter_cols[0]:
                    early_minimum_volume = st.number_input(
                        "Minimum 5m volume ($)",
                        min_value=0.0,
                        value=1_000.0,
                        step=500.0,
                    )
                with filter_cols[1]:
                    early_minimum_turnover = st.number_input(
                        "Minimum 5m volume / market cap",
                        min_value=0.0,
                        value=0.12,
                        step=0.05,
                        help="Recent turnover helps separate active demand from stale lifetime volume.",
                    )
                with filter_cols[2]:
                    early_minimum_buy_ratio = st.number_input(
                        "Minimum buys / sells",
                        min_value=0.0,
                        value=1.40,
                        step=0.05,
                        help="Uses the most recent five-minute trade counts.",
                    )
                with filter_cols[3]:
                    early_minimum_trades = st.number_input(
                        "Minimum trades",
                        min_value=0,
                        value=25,
                        step=5,
                    )
                early_settings.update({
                    "minimum_market_cap": float(cap_range[0]),
                    "maximum_market_cap": float(cap_range[1]),
                    "minimum_age": float(age_range[0]),
                    "maximum_age": float(age_range[1]),
                    "minimum_momentum": float(momentum_range[0]),
                    "maximum_momentum": float(momentum_range[1]),
                    "minimum_volume": float(early_minimum_volume),
                    "minimum_turnover": float(early_minimum_turnover),
                    "minimum_buy_ratio": float(early_minimum_buy_ratio),
                    "minimum_trades": float(early_minimum_trades),
                    "minimum_holders": float(holder_range[0]),
                    "maximum_holders": float(holder_range[1]),
                })

        if scan_profile == "BOT trades":
            with st.expander("BOT-trade risk filters", expanded=True):
                st.caption(
                    "Reference pattern: NTDA · "
                    "ufteZkSALGhT9NwE34UHNUyxinJGgAFLMmmtfZepump"
                )
                bot_cols = st.columns(4)
                with bot_cols[0]:
                    bot_minimum_market_cap = st.number_input(
                        "Minimum market cap ($)",
                        min_value=0.0,
                        value=5_000.0,
                        step=1_000.0,
                        key="bot_minimum_market_cap_v1",
                    )
                    bot_minimum_5m_trades = st.number_input(
                        "Minimum trades in 5m",
                        min_value=0,
                        value=200,
                        step=25,
                        key="bot_minimum_5m_trades_v1",
                    )
                    bot_minimum_raw_trades = st.number_input(
                        "Minimum raw trades in 1m",
                        min_value=0,
                        value=40,
                        step=10,
                        key="bot_minimum_raw_trades_v1",
                    )
                    bot_minimum_raw_buys = st.number_input(
                        "Minimum raw buys in 1m",
                        min_value=0,
                        value=30,
                        step=10,
                        key="bot_minimum_raw_buys_v1",
                    )
                with bot_cols[1]:
                    bot_maximum_market_cap = st.number_input(
                        "Maximum market cap ($)",
                        min_value=0.0,
                        value=150_000_000.0,
                        step=1_000_000.0,
                        key="bot_maximum_market_cap_v1",
                    )
                    bot_minimum_buy_share = st.number_input(
                        "Minimum 5m buy share (%)",
                        min_value=0.0,
                        max_value=100.0,
                        value=80.0,
                        step=5.0,
                        key="bot_minimum_buy_share_v1",
                    )
                with bot_cols[2]:
                    bot_age_range = st.slider(
                        "Token-age window (minutes)",
                        0,
                        1_440,
                        (0, 360),
                        5,
                        key="bot_age_range_v1",
                    )
                    bot_maximum_median_buy = st.number_input(
                        "Maximum median buy ($)",
                        min_value=0.0,
                        value=0.25,
                        step=0.05,
                        format="%.2f",
                        key="bot_maximum_median_buy_v1",
                    )
                with bot_cols[3]:
                    bot_minimum_micro_share = st.number_input(
                        "Minimum buys below $0.10 (%)",
                        min_value=0.0,
                        max_value=100.0,
                        value=50.0,
                        step=5.0,
                        key="bot_minimum_micro_share_v1",
                    )
                    bot_minimum_top_wallet_share = st.number_input(
                        "Minimum top-wallet buy USD (%)",
                        min_value=0.0,
                        max_value=100.0,
                        value=50.0,
                        step=5.0,
                        key="bot_minimum_top_wallet_share_v1",
                    )
                bot_settings.update({
                    "minimum_market_cap": float(bot_minimum_market_cap),
                    "maximum_market_cap": float(bot_maximum_market_cap),
                    "minimum_age": float(bot_age_range[0]),
                    "maximum_age": float(bot_age_range[1]),
                    "minimum_5m_trades": float(bot_minimum_5m_trades),
                    "minimum_buy_share": float(bot_minimum_buy_share),
                    "minimum_raw_trades": float(bot_minimum_raw_trades),
                    "minimum_raw_buys": float(bot_minimum_raw_buys),
                    "maximum_median_buy": float(bot_maximum_median_buy),
                    "minimum_micro_share": float(bot_minimum_micro_share),
                    "minimum_top_wallet_share": float(bot_minimum_top_wallet_share),
                })

        auto_scan_minutes = 0 if auto_scan_label == "Off" else int(auto_scan_label.split()[1])
        auto_scan_scheduler(auto_scan_minutes, scan_profile)
        if scan_profile == "Early runners":
            st.info(
                "Early runners targets the same broad entry-stage signature: young, low-cap tokens "
                "with strong five-minute turnover and buy pressure. A HIGH MATCH is an attention "
                "signal, not a buy signal. Some launch-mode and audit fields are unavailable in "
                "the quick feed, so deep-scan every candidate."
            )
        elif scan_profile == "BOT trades":
            st.error(
                "BOT trades is a manipulation-risk detector modeled on NTDA's micro-buy swarm and "
                "wallet-dollar concentration. BOT-LIKE RISK means avoid or investigate—it is not a buy signal."
            )
        elif scan_profile == "Aggressive":
            st.warning(
                "Aggressive discovery includes thin, ungraduated and weakly confirmed launches. "
                "Treat the mover rank as an attention signal—not an entry signal."
            )
        elif scan_profile == "Top movers":
            st.info(
                "Top movers requires current five-minute and one-hour dollar volume plus recent "
                "trade activity, then ranks flow and participation ahead of price change. Results "
                "cover the scanned five-minute trending candidates, not the entire market, and are not a buy signal."
            )
        elif scan_profile == "Live now":
            st.info(
                "Live now shows the current Pump.fun viewer count for indexed active livestreams. "
                "Counts change quickly."
            )
        else:
            st.caption("Pump.fun viewer counts appear only while a token has an active livestream. Choose Live now to scan those rooms directly.")

        try:
            with st.spinner("Scanning Pump tokens..."):
                if scan_profile == "Live now":
                    coins = cached_live_mints(discovery_limit)
                elif scan_profile == "Top movers":
                    coins = cached_top_movers(discovery_limit)
                else:
                    feed_sort = (
                        "last_trade_timestamp"
                        if scan_profile in {"Early runners", "BOT trades", "Aggressive"}
                        else "created_timestamp"
                    )
                    coins = cached_recent_mints(discovery_limit, feed_sort)
                if graduated_only:
                    coins = [coin for coin in coins if coin.get("complete")]
                discovery_rows = [cached_quick_scan(coin, rules.__dict__) for coin in coins]
        except ScannerError as exc:
            st.error(str(exc))
            discovery_rows = []

        if discovery_rows:
            discovery_df = pd.DataFrame(discovery_rows)
            mints_scanned_count = len(discovery_df)
            missing_early_data_count = 0
            missing_bot_aggregate_count = 0
            missing_bot_sample_count = 0
            missing_top_mover_data_count = 0
            holder_count = pd.to_numeric(
                discovery_df["holder_count"], errors="coerce"
            ).replace([float("inf"), float("-inf")], float("nan"))
            missing_holder_count = int(holder_count.isna().sum())
            if scan_profile == "Early runners":
                early_metrics = pd.DataFrame([
                    early_runner_metrics(row, early_settings)
                    for row in discovery_df.to_dict("records")
                ])
                missing_early_data_count = int(early_metrics["early_score"].isna().sum())
                for column in early_metrics:
                    discovery_df[column] = early_metrics[column]

                market_cap = pd.to_numeric(
                    discovery_df["market_cap_usd"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                age_minutes = pd.to_numeric(
                    discovery_df["age_minutes"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                move_5m = pd.to_numeric(
                    discovery_df["price_change_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                volume_5m = pd.to_numeric(
                    discovery_df["volume_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                buys_5m = pd.to_numeric(
                    discovery_df["buys_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                sells_5m = pd.to_numeric(
                    discovery_df["sells_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                trade_count = buys_5m + sells_5m
                buy_ratio = buys_5m.div(sells_5m.where(sells_5m > 0))
                buy_ratio = buy_ratio.mask((sells_5m == 0) & (buys_5m > 0), float("inf"))
                turnover = volume_5m.div(market_cap.where(market_cap > 0))
                complete = discovery_df["complete"].fillna(False).astype(bool)
                liquidity = pd.to_numeric(discovery_df["liquidity_usd"], errors="coerce")
                graduated_quality = (
                    (liquidity >= 8_000.0)
                    & (liquidity.div(market_cap.where(market_cap > 0)) >= 0.15)
                )

                early_mask = (
                    market_cap.between(
                        early_settings["minimum_market_cap"],
                        early_settings["maximum_market_cap"],
                    )
                    & age_minutes.between(
                        early_settings["minimum_age"],
                        early_settings["maximum_age"],
                    )
                    & move_5m.between(
                        early_settings["minimum_momentum"],
                        early_settings["maximum_momentum"],
                    )
                    & (volume_5m >= early_settings["minimum_volume"])
                    & (turnover >= early_settings["minimum_turnover"])
                    & (trade_count >= early_settings["minimum_trades"])
                    & (buy_ratio >= early_settings["minimum_buy_ratio"])
                    & holder_count.between(
                        early_settings["minimum_holders"],
                        early_settings["maximum_holders"],
                    )
                    & discovery_df["early_score"].notna()
                )
                if rules.reject_boost:
                    early_mask &= ~discovery_df["boost_mode"].fillna(False).astype(bool)
                if rules.reject_mayhem:
                    early_mask &= ~discovery_df["mayhem_mode"].fillna(False).astype(bool)
                if early_ungraduated_only:
                    early_mask &= ~complete
                else:
                    early_mask &= (~complete) | graduated_quality

                discovery_df = discovery_df[early_mask].copy()
                discovery_df["mover_score"] = None
                discovery_df = discovery_df.sort_values(
                    ["early_score", "score"],
                    ascending=[False, False],
                    na_position="last",
                )
            elif scan_profile == "BOT trades":
                market_cap = pd.to_numeric(
                    discovery_df["market_cap_usd"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                age_minutes = pd.to_numeric(
                    discovery_df["age_minutes"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                buys_5m = pd.to_numeric(
                    discovery_df["buys_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                sells_5m = pd.to_numeric(
                    discovery_df["sells_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                buys_1h = pd.to_numeric(
                    discovery_df["buys_1h"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                sells_1h = pd.to_numeric(
                    discovery_df["sells_1h"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                volume_5m = pd.to_numeric(
                    discovery_df["volume_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                trades_5m = buys_5m + sells_5m
                trades_1h = buys_1h + sells_1h
                buy_share_pct = buys_5m.div(trades_5m.where(trades_5m > 0)) * 100.0
                complete = discovery_df["complete"].fillna(False).astype(bool)
                aggregate_missing = (
                    market_cap.isna()
                    | age_minutes.isna()
                    | volume_5m.isna()
                    | buys_5m.isna()
                    | sells_5m.isna()
                    | buys_1h.isna()
                    | sells_1h.isna()
                    | (trades_1h < trades_5m)
                )
                missing_bot_aggregate_count = int(aggregate_missing.sum())

                bot_mask = (
                    market_cap.between(
                        bot_settings["minimum_market_cap"],
                        bot_settings["maximum_market_cap"],
                    )
                    & age_minutes.between(
                        bot_settings["minimum_age"],
                        bot_settings["maximum_age"],
                    )
                    & (trades_5m >= bot_settings["minimum_5m_trades"])
                    & (trades_1h >= trades_5m)
                    & (buy_share_pct >= bot_settings["minimum_buy_share"])
                    & volume_5m.notna()
                )
                if graduated_only:
                    bot_mask &= complete

                discovery_df = discovery_df[bot_mask].copy().reset_index(drop=True)
                if not discovery_df.empty:
                    shortlist_trades = (
                        pd.to_numeric(discovery_df["buys_5m"], errors="coerce")
                        + pd.to_numeric(discovery_df["sells_5m"], errors="coerce")
                    )
                    shortlist = discovery_df.assign(_trades_5m=shortlist_trades).sort_values(
                        "_trades_5m", ascending=False
                    ).head(int(bot_settings["raw_shortlist_limit"]))
                    raw_samples = cached_bot_trade_samples(tuple(shortlist["mint"].astype(str)))
                    bot_metrics = pd.DataFrame([
                        bot_trade_metrics(
                            row,
                            bot_settings,
                            raw_samples.get(str(row.get("mint"))),
                        )
                        for row in discovery_df.to_dict("records")
                    ])
                    for column in bot_metrics:
                        discovery_df[column] = bot_metrics[column].to_numpy()
                    missing_bot_sample_count = int(
                        (discovery_df["bot_sample_status"] != "SAMPLED").sum()
                    )
                    discovery_df = discovery_df.sort_values(
                        ["bot_score", "trades_per_minute", "score"],
                        ascending=[False, False, False],
                        na_position="last",
                    )
                discovery_df["mover_score"] = None
            elif scan_profile == "Aggressive":
                discovery_df = discovery_df[
                    (discovery_df["price_change_5m"].fillna(-999.0) >= minimum_momentum)
                    & (discovery_df["volume_24h"].fillna(0.0) >= minimum_volume)
                ].copy()
                discovery_df["mover_score"] = (
                    discovery_df["price_change_5m"].clip(-25, 150).fillna(-25)
                    + 0.35 * discovery_df["price_change_1h"].clip(-50, 300).fillna(0)
                    + 8 * (discovery_df["volume_24h"].fillna(0) / 10_000).clip(0, 10)
                ).round(1)
                discovery_df = discovery_df.sort_values(
                    ["mover_score", "score"], ascending=[False, False], na_position="last"
                )
            elif scan_profile == "Top movers":
                top_metrics = pd.DataFrame([
                    top_mover_metrics(row)
                    for row in discovery_df.to_dict("records")
                ])
                missing_top_mover_data_count = int(top_metrics["mover_score"].isna().sum())
                for column in top_metrics:
                    discovery_df[column] = top_metrics[column].to_numpy()

                move_5m = pd.to_numeric(
                    discovery_df["price_change_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                volume_5m = pd.to_numeric(
                    discovery_df["volume_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                volume_1h = pd.to_numeric(
                    discovery_df["volume_1h"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                trades_5m = pd.to_numeric(
                    discovery_df["trades_5m"], errors="coerce"
                ).replace([float("inf"), float("-inf")], float("nan"))
                top_mover_mask = (
                    (move_5m >= top_mover_settings["minimum_momentum"])
                    & (volume_5m >= top_mover_settings["minimum_volume_5m"])
                    & (volume_1h >= top_mover_settings["minimum_volume_1h"])
                    & (trades_5m >= top_mover_settings["minimum_trades_5m"])
                    & discovery_df["mover_score"].notna()
                )
                discovery_df = discovery_df[top_mover_mask].copy()
                discovery_df = discovery_df.sort_values(
                    ["mover_score", "volume_5m", "volume_1h", "trades_5m", "score"],
                    ascending=[False, False, False, False, False],
                    na_position="last",
                )
            elif scan_profile == "Live now":
                discovery_df["mover_score"] = None
                discovery_df = discovery_df.sort_values(
                    ["live_viewers", "score"], ascending=[False, False], na_position="last"
                )
            else:
                discovery_df["mover_score"] = None
                discovery_df = discovery_df.sort_values(
                    ["score", "market_cap_usd"], ascending=[False, False], na_position="last"
                )
            if discovery_df.empty:
                if scan_profile == "Early runners":
                    with st.container(horizontal=True):
                        st.metric("Mints scanned", mints_scanned_count, border=True)
                        st.metric("Matches", 0, border=True)
                        st.metric("Missing recent data", missing_early_data_count, border=True)
                        st.metric("Missing holders", missing_holder_count, border=True)
                    if missing_holder_count == mints_scanned_count:
                        st.info(
                            "Pump.fun did not return holder totals for this sweep, so the holder "
                            "filter could not rank any tokens. Try another sweep shortly."
                        )
                    elif missing_early_data_count == mints_scanned_count:
                        st.info(
                            "None of the scanned tokens had enough five-minute market and order-flow "
                            "data to rank. Try another sweep in a minute."
                        )
                    else:
                        st.info(
                            "No tokens matched every early-runner filter in this sweep. "
                            "Widen the holder, cap, or momentum window, or lower the turnover/order-flow minimums."
                        )
                elif scan_profile == "BOT trades":
                    with st.container(horizontal=True):
                        st.metric("Mints scanned", mints_scanned_count, border=True)
                        st.metric("BOT-pattern matches", 0, border=True)
                        st.metric("Missing BOT data", missing_bot_aggregate_count, border=True)
                    if missing_bot_aggregate_count == mints_scanned_count:
                        st.info(
                            "None of the scanned tokens had complete five-minute and one-hour "
                            "market data for BOT-pattern screening. Try another sweep shortly."
                        )
                    else:
                        st.info(
                            "No active token matched the NTDA-like cadence in this sweep. "
                            "The default requires at least 200 trades in five minutes and 80% buys."
                        )
                elif scan_profile == "Top movers":
                    with st.container(horizontal=True):
                        st.metric("Mints scanned", mints_scanned_count, border=True)
                        st.metric("Active-volume matches", 0, border=True)
                        st.metric("Missing recent data", missing_top_mover_data_count, border=True)
                    if missing_top_mover_data_count == mints_scanned_count:
                        st.info(
                            "None of the scanned trending tokens had complete five-minute and one-hour "
                            "volume, price, and trade-count data. Try another sweep shortly."
                        )
                    else:
                        st.info(
                            "No scanned trending token cleared every Top-mover activity threshold. "
                            "Lower a recent-volume, trade-count, or move minimum and sweep again."
                        )
                else:
                    st.info("No fresh mints cleared the mover thresholds. Lower the 5m move or volume minimum and sweep again.")
                st.stop()
            ranked_missing_holder_count = int(
                pd.to_numeric(discovery_df["holder_count"], errors="coerce").isna().sum()
            )
            if scan_profile == "Early runners":
                rank_columns = [
                    "early_signal", "early_score", "buy_share_pct", "volume_to_cap", "trade_count"
                ]
            elif scan_profile == "BOT trades":
                rank_columns = [
                    "bot_signal", "bot_score", "bot_flags", "trades_per_minute",
                    "sustained_trades_per_minute", "bot_buy_share_pct", "median_buy_usd",
                    "micro_buy_share_pct", "top_wallet_buy_share_pct", "liquidity_to_cap_pct",
                ]
            elif scan_profile == "Aggressive":
                rank_columns = ["mover_score"]
            elif scan_profile == "Top movers":
                rank_columns = [
                    "mover_score", "trades_5m", "trades_per_minute", "average_trade_usd"
                ]
            else:
                rank_columns = []
            display_columns = [
                "symbol", "holder_count", "verdict", *rank_columns, "score", "market_cap_usd", "liquidity_usd",
                "price_change_5m", "price_change_1h", "volume_5m", "volume_1h", "volume_24h", "age", "live_viewers",
                "complete", "mint",
            ]
            discovery_df["age"] = discovery_df["age_minutes"].map(age_label)
            live_stream_count = int(discovery_df["is_currently_live"].fillna(False).sum())
            graduated_count = int(discovery_df["complete"].fillna(False).sum())
            cleared_hard_rules = int(
                (~discovery_df["verdict"].isin(["PASS", "HARD PASS"])).sum()
            )
            for column in display_columns:
                if column not in discovery_df:
                    discovery_df[column] = None
            discovery_df["live_viewers"] = pd.to_numeric(
                discovery_df["live_viewers"], errors="coerce"
            )
            discovery_df["holder_count"] = pd.to_numeric(
                discovery_df["holder_count"], errors="coerce"
            )
            discovery_df = discovery_df[display_columns]

            with st.container(horizontal=True):
                st.metric("Mints scanned", mints_scanned_count, border=True)
                st.metric("Matches", len(discovery_df), border=True)
                st.metric(
                    "Live streams",
                    live_stream_count,
                    border=True,
                )
                st.metric("Graduated", graduated_count, border=True)
                st.metric(
                    "Cleared hard rules",
                    cleared_hard_rules,
                    border=True,
                )

            if scan_profile == "Early runners" and missing_early_data_count:
                st.caption(
                    f"{missing_early_data_count} scanned token(s) were skipped because recent "
                    "market or five-minute order-flow data was unavailable."
                )
            if scan_profile == "Early runners" and missing_holder_count:
                st.caption(
                    f"{missing_holder_count} scanned token(s) had no Pump.fun holder total and were "
                    "excluded from the holder-count filter. Missing is unknown, not zero."
                )
            if scan_profile != "Early runners" and ranked_missing_holder_count:
                st.caption(
                    f"{ranked_missing_holder_count} ranked token(s) have no Pump.fun holder total. "
                    "Blank holder counts are unknown, not zero."
                )
            if scan_profile == "BOT trades" and missing_bot_aggregate_count:
                st.caption(
                    f"{missing_bot_aggregate_count} scanned token(s) were skipped because complete "
                    "five-minute or one-hour market data was unavailable."
                )
            if scan_profile == "BOT trades" and missing_bot_sample_count:
                st.caption(
                    f"{missing_bot_sample_count} aggregate match(es) could not receive a fresh raw-trade "
                    "sample. They remain labeled AGGREGATE ONLY."
                )
            if scan_profile == "Top movers" and missing_top_mover_data_count:
                st.caption(
                    f"{missing_top_mover_data_count} scanned token(s) were skipped because complete "
                    "five-minute or one-hour volume, price, or trade-count data was unavailable."
                )

            st.dataframe(
                discovery_df,
                hide_index=True,
                key="discovery_table",
                column_config={
                    "symbol": st.column_config.TextColumn("Token", pinned=True),
                    "holder_count": st.column_config.NumberColumn(
                        "Holders",
                        format="%d",
                        help="Pump.fun holder count; blank means the count is temporarily unavailable.",
                    ),
                    "verdict": st.column_config.TextColumn("Quick decision"),
                    "early_signal": st.column_config.TextColumn("Similarity"),
                    "early_score": st.column_config.ProgressColumn(
                        "Early rank",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                        help="Similarity rank from cap, age, momentum, recent turnover, and recent order flow.",
                    ),
                    "buy_share_pct": st.column_config.NumberColumn("Buy share", format="%.1f%%"),
                    "volume_to_cap": st.column_config.NumberColumn(
                        "5m volume / MC",
                        format="%.2fx",
                    ),
                    "trade_count": st.column_config.NumberColumn("Trades", format="%.0f"),
                    "bot_signal": st.column_config.TextColumn("BOT risk"),
                    "bot_score": st.column_config.ProgressColumn(
                        "BOT-risk rank",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                        help="Automation-like risk rank; it is not proof that a wallet is operated by a bot.",
                    ),
                    "bot_flags": st.column_config.TextColumn("Raw-trade flags", width="large"),
                    "trades_5m": st.column_config.NumberColumn("5m trades", format="%d"),
                    "trades_per_minute": st.column_config.NumberColumn("5m trades/min", format="%.1f"),
                    "sustained_trades_per_minute": st.column_config.NumberColumn(
                        "Up to 1h trades/min", format="%.1f"
                    ),
                    "bot_buy_share_pct": st.column_config.NumberColumn("5m buy share", format="%.1f%%"),
                    "average_trade_usd": st.column_config.NumberColumn("Avg 5m trade", format="$%.2f"),
                    "median_buy_usd": st.column_config.NumberColumn("Median raw buy", format="$%.4f"),
                    "micro_buy_share_pct": st.column_config.NumberColumn("Buys under $0.10", format="%.1f%%"),
                    "top_wallet_buy_share_pct": st.column_config.NumberColumn(
                        "Top wallet buy USD", format="%.1f%%"
                    ),
                    "liquidity_to_cap_pct": st.column_config.NumberColumn("Liquidity / MC", format="%.2f%%"),
                    "mover_score": st.column_config.NumberColumn(
                        "Volume/activity rank" if scan_profile == "Top movers" else "Mover rank",
                        format="%.1f",
                        help=(
                            "45% five-minute volume, 20% one-hour volume, 25% five-minute trade count, and 10% price momentum."
                            if scan_profile == "Top movers"
                            else None
                        ),
                    ),
                    "score": st.column_config.ProgressColumn("Risk score", min_value=0, max_value=100),
                    "market_cap_usd": st.column_config.NumberColumn("Market cap", format="$%.0f"),
                    "liquidity_usd": st.column_config.NumberColumn("Liquidity", format="$%.0f"),
                    "price_change_5m": st.column_config.NumberColumn("5m", format="%+.1f%%"),
                    "price_change_1h": st.column_config.NumberColumn("1h", format="%+.1f%%"),
                    "volume_5m": st.column_config.NumberColumn("5m volume", format="$%.0f"),
                    "volume_1h": st.column_config.NumberColumn("1h volume", format="$%.0f"),
                    "volume_24h": st.column_config.NumberColumn("24h volume", format="$%.0f"),
                    "age": st.column_config.TextColumn("Token age"),
                    "live_viewers": st.column_config.NumberColumn(
                        "Pump.fun viewer count",
                        format="%.0f",
                        help="Current viewer count reported by Pump.fun for an active livestream. Blank means the token is not live or Pump did not return a count.",
                    ),
                    "complete": st.column_config.CheckboxColumn("Graduated"),
                    "mint": st.column_config.TextColumn("Mint"),
                },
            )

            if scan_profile == "Early runners":
                candidate_labels = {
                    (
                        f"${row['symbol']} · {row['early_signal']} "
                        f"{row['early_score']:.0f}/100 · {money(row['market_cap_usd'])} "
                        f"· {str(row['mint'])[-6:]}"
                    ): row["mint"]
                    for row in discovery_df.to_dict("records")
                }
            elif scan_profile == "BOT trades":
                candidate_labels = {
                    (
                        f"${row['symbol']} · {row['bot_signal']} "
                        f"{row['bot_score']:.0f}/100 · {money(row['market_cap_usd'])} "
                        f"· {str(row['mint'])[-6:]}"
                    ): row["mint"]
                    for row in discovery_df.to_dict("records")
                }
            elif scan_profile == "Top movers":
                candidate_labels = {
                    (
                        f"${row['symbol']} · {row['mover_score']:.0f}/100 activity · "
                        f"{money(row['volume_5m'])} in 5m · {str(row['mint'])[-6:]}"
                    ): row["mint"]
                    for row in discovery_df.to_dict("records")
                }
            else:
                candidate_labels = {
                    (
                        f"${row['symbol']} · {row['score']}/100 risk · "
                        f"{money(row['market_cap_usd'])} · {str(row['mint'])[-6:]}"
                    ): row["mint"]
                    for row in discovery_df.to_dict("records")
                }
            selected_label = st.selectbox("Candidate to inspect", list(candidate_labels))
            selected_mint = candidate_labels[selected_label]
            with st.container(horizontal=True):
                if st.button("Deep scan candidate", icon=":material/search_check:"):
                    try:
                        with st.spinner("Running on-chain concentration checks..."):
                            st.session_state.last_scan = cached_scan(selected_mint, rules.__dict__, {})
                        st.toast("Deep scan complete — open the Deep scan tab")
                    except ScannerError as exc:
                        st.error(str(exc))
                if st.button("Add candidate to watchlist", icon=":material/bookmark_add:"):
                    if selected_mint not in st.session_state.watchlist:
                        st.session_state.watchlist.append(selected_mint)
                        save_json(WATCHLIST_FILE, st.session_state.watchlist)
                        st.toast("Added to watchlist")
                st.link_button("Open Pump", f"https://pump.fun/coin/{selected_mint}", icon=":material/open_in_new:")

if scan_tab.open:
  with scan_tab:
    with st.form("scan_form", border=True):
        mint = st.text_input("Solana mint address", placeholder="Paste a Pump.fun mint ending in pump")
        st.markdown("**Optional Pump audit overrides**")
        manual_cols = st.columns(5)
        with manual_cols[0]:
            holder_count = st.number_input(
                "Holders",
                min_value=0,
                value=0,
                help="Enter 0 to use Pump.fun's indexed holder count.",
            )
        with manual_cols[1]:
            top10 = st.number_input("Top 10 (%)", 0.0, 100.0, 0.0, help="Enter 0 to use the conservative RPC estimate")
        with manual_cols[2]:
            creator = st.number_input("Creator (%)", 0.0, 100.0, 0.0)
        with manual_cols[3]:
            snipers = st.number_input("Snipers (%)", 0.0, 100.0, 0.0, help="Enter 0 to leave unknown")
        with manual_cols[4]:
            bundlers = st.number_input("Bundlers (%)", 0.0, 100.0, 0.0, help="Enter 0 to leave unknown")
        submitted = st.form_submit_button("Scan token", icon=":material/search:", type="primary")

    if submitted:
        manual = {
            "holder_count": int(holder_count) if holder_count else None,
            "top10_percent": top10 or None,
            "creator_percent": creator if creator else None,
            "sniper_percent": snipers or None,
            "bundler_percent": bundlers or None,
        }
        try:
            with st.spinner("Reading live market and on-chain data..."):
                st.session_state.last_scan = cached_scan(mint, rules.__dict__, manual)
        except ScannerError as exc:
            st.error(str(exc))

    if st.session_state.last_scan:
        result = ScanResult(**st.session_state.last_scan)
        with st.container(border=True):
            with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
                st.markdown(f"## ${result.symbol} · {result.name}")
                st.markdown(verdict_badge(result.verdict))
            with st.container(horizontal=True):
                st.metric("Score", f"{result.score}/100", border=True)
                st.metric("Market cap", money(result.market_cap_usd), border=True)
                st.metric("Liquidity", money(result.liquidity_usd), border=True)
                st.metric("5-minute move", percent(result.price_change_5m), border=True)
                st.metric("Token age", age_label(result.age_minutes), border=True)
                st.metric(
                    "Holders",
                    f"{result.holder_count:,}" if result.holder_count is not None else "Unavailable",
                    border=True,
                    help="Holder total reported by Pump.fun, unless manually overridden.",
                )
                st.metric(
                    "Pump.fun viewer count",
                    pump_viewer_count_label(result.is_currently_live, result.live_viewers),
                    delta="LIVE" if result.is_currently_live else None,
                    border=True,
                    help="Current viewer count reported by Pump.fun. 'Not live' means there is no active livestream; 'Unavailable' means Pump did not return a number.",
                )
            if result.reasons:
                st.markdown("**Decision signals**")
                for reason in result.reasons:
                    st.write(f"- {reason}")
            if result.warnings:
                st.markdown("**Needs attention**")
                for warning in result.warnings:
                    st.write(f"- {warning}")
            with st.container(horizontal=True):
                if st.button("Add to watchlist", icon=":material/bookmark_add:"):
                    if result.mint not in st.session_state.watchlist:
                        st.session_state.watchlist.append(result.mint)
                        save_json(WATCHLIST_FILE, st.session_state.watchlist)
                        st.toast("Added to watchlist")
                if result.pair_url:
                    st.link_button("Open chart", result.pair_url, icon=":material/candlestick_chart:")
                st.link_button("Open Pump", f"https://pump.fun/coin/{result.mint}", icon=":material/open_in_new:")

if watch_tab.open:
  with watch_tab:
    with st.form("watchlist_add", border=False):
        new_watch = st.text_input("Add mint to watchlist", placeholder="Solana mint")
        add_watch = st.form_submit_button("Add", icon=":material/add:")
    if add_watch and new_watch.strip() and new_watch.strip() not in st.session_state.watchlist:
        st.session_state.watchlist.append(new_watch.strip())
        save_json(WATCHLIST_FILE, st.session_state.watchlist)
        st.rerun()

    rows = []
    for watch_mint in st.session_state.watchlist:
        try:
            rows.append(cached_scan(watch_mint, rules.__dict__, {}))
        except ScannerError:
            rows.append({"mint": watch_mint, "symbol": "Error", "verdict": "PASS", "score": 0})
    if rows:
        watch_df = pd.DataFrame(rows)
        display_columns = [
            "symbol", "holder_count", "verdict", "score", "market_cap_usd", "liquidity_usd",
            "price_change_5m", "price_change_1h", "volume_24h", "age", "live_viewers",
            "mint",
        ]
        watch_df["age"] = watch_df["age_minutes"].map(age_label)
        for column in display_columns:
            if column not in watch_df:
                watch_df[column] = None
        watch_df["live_viewers"] = pd.to_numeric(
            watch_df["live_viewers"], errors="coerce"
        )
        watch_df["holder_count"] = pd.to_numeric(
            watch_df["holder_count"], errors="coerce"
        )
        watch_df = watch_df[display_columns].sort_values("score", ascending=False)
        st.dataframe(
            watch_df,
            hide_index=True,
            column_config={
                "symbol": st.column_config.TextColumn("Token", pinned=True),
                "holder_count": st.column_config.NumberColumn(
                    "Holders",
                    format="%d",
                    help="Pump.fun holder count; blank means the count is temporarily unavailable.",
                ),
                "verdict": st.column_config.TextColumn("Decision"),
                "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100),
                "market_cap_usd": st.column_config.NumberColumn("Market cap", format="$%.0f"),
                "liquidity_usd": st.column_config.NumberColumn("Liquidity", format="$%.0f"),
                "price_change_5m": st.column_config.NumberColumn("5m", format="%+.1f%%"),
                "price_change_1h": st.column_config.NumberColumn("1h", format="%+.1f%%"),
                "volume_24h": st.column_config.NumberColumn("24h volume", format="$%.0f"),
                "age": st.column_config.TextColumn("Token age"),
                "live_viewers": st.column_config.NumberColumn(
                    "Pump.fun viewer count",
                    format="%.0f",
                    help="Current viewer count reported by Pump.fun for an active livestream. Blank means the token is not live or Pump did not return a count.",
                ),
                "mint": st.column_config.TextColumn("Mint"),
            },
        )
        remove_mint = st.selectbox("Remove from watchlist", ["—"] + list(st.session_state.watchlist))
        if st.button("Remove selected", icon=":material/delete:", disabled=remove_mint == "—"):
            st.session_state.watchlist.remove(remove_mint)
            save_json(WATCHLIST_FILE, st.session_state.watchlist)
            st.rerun()

if positions_tab.open:
  with positions_tab:
    positions = st.session_state.positions
    if not positions:
        st.info("No open positions saved.")
    for index, position in enumerate(positions):
        try:
            live = ScanResult(**cached_scan(position["mint"], rules.__dict__, {}))
            current_mc = live.market_cap_usd
        except ScannerError:
            current_mc = None
        entry_mc = float(position["entry_market_cap"])
        pnl_pct = ((current_mc / entry_mc) - 1) * 100 if current_mc else None
        current_value = float(position["cost_usd"]) * (current_mc / entry_mc) if current_mc else None
        with st.container(border=True):
            st.markdown(f"### ${position['symbol']}")
            with st.container(horizontal=True):
                st.metric("Entry market cap", money(entry_mc), border=True)
                st.metric("Current market cap", money(current_mc), border=True)
                st.metric("Estimated position", money(current_value), delta=percent(pnl_pct), border=True)
                st.metric("Invalidation", money(float(position["invalidation_market_cap"])), border=True)
            progress = "IN RANGE"
            if current_mc:
                if current_mc <= float(position["invalidation_market_cap"]):
                    progress = "INVALIDATED"
                elif current_mc >= float(position["target_2_market_cap"]):
                    progress = "TARGET 2"
                elif current_mc >= float(position["target_1_market_cap"]):
                    progress = "TARGET 1"
            st.markdown(verdict_badge("ENTRY ELIGIBLE" if progress == "IN RANGE" else "WATCH") + f" **{progress}**")
            st.caption("Position value is an estimate based on market-cap change and excludes fees, slippage, and partial sales.")

    with st.expander("Add position"):
        with st.form("position_form"):
            p_mint = st.text_input("Mint", key="position_mint")
            p_symbol = st.text_input("Symbol")
            p_cost = st.number_input("Cost ($)", 0.0, 1_000_000.0, 15.0)
            p_entry = st.number_input("Entry market cap ($)", 0.0, 1_000_000_000.0, 0.0)
            p_stop = st.number_input("Invalidation market cap ($)", 0.0, 1_000_000_000.0, 0.0)
            p_t1 = st.number_input("Target 1 market cap ($)", 0.0, 1_000_000_000.0, 0.0)
            p_t2 = st.number_input("Target 2 market cap ($)", 0.0, 1_000_000_000.0, 0.0)
            add_position = st.form_submit_button("Save position", icon=":material/save:")
        if add_position and p_mint and p_entry:
            positions.append({
                "mint": p_mint.strip(), "symbol": p_symbol.strip() or "Unknown", "cost_usd": p_cost,
                "entry_market_cap": p_entry, "invalidation_market_cap": p_stop,
                "target_1_market_cap": p_t1, "target_2_market_cap": p_t2,
            })
            save_json(POSITIONS_FILE, positions)
            st.rerun()

st.divider()
st.caption("Decision support only. Data can be delayed or incomplete. Verify the mint, execution price, liquidity, and wallet prompts before every trade.")
