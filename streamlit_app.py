"""Pump Scanner — manual-first decision support for Solana Pump.fun tokens."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from scanner import Rules, ScanResult, ScannerError, discover_recent_mints, scan_token

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


@st.cache_data(ttl="30s", max_entries=100, show_spinner=False)
def cached_scan(mint: str, rules_payload: dict, manual_payload: dict) -> dict:
    result = scan_token(mint, Rules(**rules_payload), manual=manual_payload)
    return result.to_dict()


@st.cache_data(ttl="20s", max_entries=10, show_spinner=False)
def cached_recent_mints(limit: int, sort_by: str) -> list[dict]:
    return discover_recent_mints(limit, sort_by)


@st.cache_data(ttl="30s", max_entries=150, show_spinner=False)
def cached_quick_scan(coin_payload: dict, rules_payload: dict) -> dict:
    result = scan_token(
        coin_payload["mint"],
        Rules(**rules_payload),
        coin_data=coin_payload,
        include_onchain=False,
    )
    return result.to_dict()


@st.fragment(run_every="1m")
def auto_scan_scheduler(interval_minutes: int) -> None:
    """Use a lightweight one-minute timer to trigger the selected sweep cadence."""
    if interval_minutes <= 0:
        st.caption(":gray-badge[Automatic sweep off]")
        return

    now = time.time()
    last_run = float(st.session_state.last_auto_scan_at or 0.0)
    if last_run <= 0:
        st.session_state.last_auto_scan_at = now
        last_run = now
    remaining_seconds = interval_minutes * 60 - (now - last_run)
    if remaining_seconds <= 0:
        st.session_state.last_auto_scan_at = now
        st.cache_data.clear()
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
    if value is None:
        return "—"
    if value < 60:
        return f"{value:.0f}m"
    if value < 1_440:
        return f"{int(value // 60)}h {int(value % 60)}m"
    return f"{int(value // 1_440)}d {int((value % 1_440) // 60)}h"


def live_audience_label(is_live: object, viewers: object) -> str:
    """Keep live participants distinct from Pump's recorded-video view totals."""
    if is_live is None or pd.isna(is_live) or not bool(is_live):
        return "Not live"
    if viewers is None or pd.isna(viewers):
        return "Live · count unavailable"
    return f"{int(viewers):,} live"


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
    minimum_holders = st.number_input("Minimum holders", 0, 1_000_000, 100, 25)
    reject_modes = st.pills("Reject launch modes", ["BOOST", "Mayhem"], default=["BOOST", "Mayhem"], selection_mode="multi")
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
        st.markdown("## Fresh mint discovery")
        st.caption(
            "Pulls the newest non-NSFW launches automatically. The quick pass avoids slow holder RPC calls; "
            "run a deep scan before treating any candidate as entry-eligible."
        )
        scan_profile = st.segmented_control(
            "Ranking profile",
            ["Aggressive", "Top movers", "Risk-first"],
            default="Aggressive",
            key="scan_profile_v2",
            help="Aggressive casts the widest early-launch net. Top movers requires stronger confirmation. Risk-first prioritizes safety rules.",
        )
        with st.container(horizontal=True, vertical_alignment="bottom"):
            discovery_limit = st.select_slider(
                "Mints per sweep",
                options=[10, 20, 30, 40, 50],
                value=50,
                key="discovery_limit_v2",
            )
            graduated_only = st.toggle("Graduated only", value=False)
            auto_scan_label = st.selectbox(
                "Automatic sweep",
                ["Off"] + [f"Every {minute} min" for minute in range(1, 16)],
                index=0,
                help="Runs while the Discover view remains open.",
            )
            if scan_profile in {"Aggressive", "Top movers"}:
                aggressive = scan_profile == "Aggressive"
                minimum_momentum = st.number_input(
                    "Minimum 5m move (%)",
                    -50.0,
                    500.0,
                    0.0 if aggressive else 3.0,
                    1.0,
                    key=f"minimum_momentum_{scan_profile}",
                )
                minimum_volume = st.number_input(
                    "Minimum 24h volume ($)",
                    0.0,
                    10_000_000.0,
                    250.0 if aggressive else 1_000.0,
                    250.0,
                    key=f"minimum_volume_{scan_profile}",
                )
            else:
                minimum_momentum = 0.0
                minimum_volume = 0.0
            if st.button("Sweep now", icon=":material/radar:", type="primary"):
                st.session_state.last_auto_scan_at = time.time()
                st.cache_data.clear()
                st.rerun()

        auto_scan_minutes = 0 if auto_scan_label == "Off" else int(auto_scan_label.split()[1])
        auto_scan_scheduler(auto_scan_minutes)
        if scan_profile == "Aggressive":
            st.warning(
                "Aggressive discovery includes thin, ungraduated and weakly confirmed launches. "
                "Treat the mover rank as an attention signal—not an entry signal."
            )

        try:
            with st.spinner("Scanning the newest launches..."):
                feed_sort = (
                    "last_trade_timestamp"
                    if scan_profile in {"Aggressive", "Top movers"}
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
            if scan_profile in {"Aggressive", "Top movers"}:
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
            else:
                discovery_df["mover_score"] = None
                discovery_df = discovery_df.sort_values(
                    ["score", "market_cap_usd"], ascending=[False, False], na_position="last"
                )
            if discovery_df.empty:
                st.info("No fresh mints cleared the mover thresholds. Lower the 5m move or volume minimum and sweep again.")
                st.stop()
            display_columns = [
                "symbol", "verdict", "mover_score", "score", "market_cap_usd", "liquidity_usd",
                "price_change_5m", "price_change_1h", "volume_24h", "age", "live_audience",
                "complete", "mint",
            ]
            discovery_df["age"] = discovery_df["age_minutes"].map(age_label)
            discovery_df["live_audience"] = discovery_df.apply(
                lambda row: live_audience_label(row.get("is_currently_live"), row.get("live_viewers")),
                axis=1,
            )
            for column in display_columns:
                if column not in discovery_df:
                    discovery_df[column] = None
            discovery_df = discovery_df[display_columns]

            with st.container(horizontal=True):
                st.metric("Mints scanned", len(discovery_df), border=True)
                st.metric("Graduated", int(discovery_df["complete"].sum()), border=True)
                st.metric(
                    "Cleared hard rules",
                    int((~discovery_df["verdict"].isin(["PASS", "HARD PASS"])).sum()),
                    border=True,
                )

            st.dataframe(
                discovery_df,
                hide_index=True,
                key="discovery_table",
                column_config={
                    "symbol": st.column_config.TextColumn("Token", pinned=True),
                    "verdict": st.column_config.TextColumn("Quick decision"),
                    "mover_score": st.column_config.NumberColumn("Mover rank", format="%.1f"),
                    "score": st.column_config.ProgressColumn("Risk score", min_value=0, max_value=100),
                    "market_cap_usd": st.column_config.NumberColumn("Market cap", format="$%.0f"),
                    "liquidity_usd": st.column_config.NumberColumn("Liquidity", format="$%.0f"),
                    "price_change_5m": st.column_config.NumberColumn("5m", format="%+.1f%%"),
                    "price_change_1h": st.column_config.NumberColumn("1h", format="%+.1f%%"),
                    "volume_24h": st.column_config.NumberColumn("24h volume", format="$%.0f"),
                    "age": st.column_config.TextColumn("Token age"),
                    "live_audience": st.column_config.TextColumn(
                        "Live audience",
                        help="Current Pump.fun livestream participants. Recorded-video views are a different metric.",
                    ),
                    "complete": st.column_config.CheckboxColumn("Graduated"),
                    "mint": st.column_config.TextColumn("Mint"),
                },
            )

            candidate_labels = {
                f"${row['symbol']} · {row['score']}/100 risk · {money(row['market_cap_usd'])}": row["mint"]
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
            holder_count = st.number_input("Holders", min_value=0, value=0, help="Enter 0 to leave unknown")
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
                    "Live audience",
                    live_audience_label(result.is_currently_live, result.live_viewers),
                    delta="LIVE" if result.is_currently_live else None,
                    border=True,
                    help="Current Pump.fun livestream participants. 'Not live' means there is no active stream; recorded-video views are not counted here.",
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
            "symbol", "verdict", "score", "market_cap_usd", "liquidity_usd",
            "price_change_5m", "price_change_1h", "volume_24h", "age", "live_audience",
            "mint",
        ]
        watch_df["age"] = watch_df["age_minutes"].map(age_label)
        watch_df["live_audience"] = watch_df.apply(
            lambda row: live_audience_label(row.get("is_currently_live"), row.get("live_viewers")),
            axis=1,
        )
        for column in display_columns:
            if column not in watch_df:
                watch_df[column] = None
        watch_df = watch_df[display_columns].sort_values("score", ascending=False)
        st.dataframe(
            watch_df,
            hide_index=True,
            column_config={
                "symbol": st.column_config.TextColumn("Token", pinned=True),
                "verdict": st.column_config.TextColumn("Decision"),
                "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100),
                "market_cap_usd": st.column_config.NumberColumn("Market cap", format="$%.0f"),
                "liquidity_usd": st.column_config.NumberColumn("Liquidity", format="$%.0f"),
                "price_change_5m": st.column_config.NumberColumn("5m", format="%+.1f%%"),
                "price_change_1h": st.column_config.NumberColumn("1h", format="%+.1f%%"),
                "volume_24h": st.column_config.NumberColumn("24h volume", format="$%.0f"),
                "age": st.column_config.TextColumn("Token age"),
                "live_audience": st.column_config.TextColumn(
                    "Live audience",
                    help="Current Pump.fun livestream participants; recorded-video views are separate.",
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
