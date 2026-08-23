"""Live data adapters and deterministic screening rules for Pump Scanner."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import re
from statistics import median
import time
from typing import Any

import requests

DEX_TOKEN_PAIRS = "https://api.dexscreener.com/token-pairs/v1/solana/{mint}"
GECKO_DISCOVERY = (
    "https://api.geckoterminal.com/api/v2/networks/solana/{feed}_pools"
    "?page={page}&include=base_token"
)
PUMP_COIN = (
    "https://frontend-api-v3.pump.fun/coins-v3/{mint}"
    "?includeLiveStreamInfo=true"
)
PUMP_LIVE_SEARCH = (
    "https://frontend-api-v3.pump.fun/coins/search-unrestricted"
    "?limit=100&offset=0&includeNsfw=false&order=desc"
    "&currentlyLive=true&sort=livestream_num_participants"
)
PUMP_CURRENTLY_LIVE = (
    "https://frontend-api-v3.pump.fun/coins/currently-live"
    "?offset=0&limit=1000&includeNsfw=false"
)
PUMP_LIVESTREAM = "https://livestream-api.pump.fun/livestream?mintId={mint}"
PUMP_LIVE_PAGE_RSC = "https://pump.fun/live?_rsc=pumpscanner"
PUMP_COIN_PAGE_RSC = "https://pump.fun/coin/{mint}?_rsc=pumpscanner"
PUMP_RECENT_SEARCH = (
    "https://frontend-api-v3.pump.fun/coins/search-unrestricted"
    "?offset=0&limit={limit}&sort={sort_by}&order=desc&includeNsfw=false"
)
PUMP_RECENT_LEGACY = (
    "https://frontend-api-v3.pump.fun/coins"
    "?offset=0&limit={limit}&sort={sort_by}&order=DESC&includeNsfw=false"
)
PUMP_DISCOVERY_BOARD = (
    "https://advanced-indexer.pump.fun/boards/{board}"
    "?tier=web&surface=WEB&platform=WEB&limit={limit}"
)
PUMP_SWAP_TRADES = "https://swap-api.pump.fun/v2/coins/{mint}/trades?limit={limit}"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

_LIVE_ROSTER_CACHE: dict[str, dict[str, Any]] = {}
_LIVE_ROSTER_CACHED_AT = 0.0
_LIVE_ROSTER_TTL_SECONDS = 15.0
_LIVE_PAGE_CACHE: dict[str, dict[str, Any]] = {}
_LIVE_PAGE_CACHED_AT = 0.0
_LIVE_PAGE_TTL_SECONDS = 25.0
_LIVE_PAGE_LAST_ERROR: str | None = None
_LIVESTREAM_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}
_LIVESTREAM_TTL_SECONDS = 30.0
_LIVESTREAM_FAILURE_TTL_SECONDS = 8.0
_MAX_VIEWER_LOOKUPS_PER_SWEEP = 30


@dataclass
class Rules:
    minimum_age_minutes: float = 15.0
    minimum_liquidity_usd: float = 15_000.0
    maximum_top10_percent: float = 30.0
    maximum_creator_percent: float = 10.0
    maximum_bundler_percent: float = 5.0
    maximum_five_minute_change: float = 35.0
    minimum_holder_count: int = 100
    reject_boost: bool = True
    reject_mayhem: bool = True


@dataclass
class ScanResult:
    mint: str
    symbol: str = "Unknown"
    name: str = "Unknown"
    verdict: str = "PASS"
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    market_cap_usd: float | None = None
    ath_market_cap_usd: float | None = None
    liquidity_usd: float | None = None
    age_minutes: float | None = None
    live_viewers: int | None = None
    is_currently_live: bool = False
    reply_count: int | None = None
    price_change_5m: float | None = None
    price_change_1h: float | None = None
    price_change_24h: float | None = None
    volume_5m: float | None = None
    volume_1h: float | None = None
    volume_24h: float | None = None
    buys_5m: int | None = None
    sells_5m: int | None = None
    buys_1h: int | None = None
    sells_1h: int | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None
    top10_percent: float | None = None
    creator_percent: float | None = None
    holder_count: int | None = None
    sniper_percent: float | None = None
    bundler_percent: float | None = None
    complete: bool = False
    boost_mode: bool = False
    mayhem_mode: bool = False
    website: str | None = None
    social: str | None = None
    pair_url: str | None = None
    scanned_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScannerError(RuntimeError):
    pass


def _get_json(url: str, *, timeout: int = 15) -> Any:
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "PumpScanner/1.0"})
    response.raise_for_status()
    return response.json()


def _get_pump_rsc(url: str, path: str, *, timeout: int = 12) -> str:
    response = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/x-component",
            "RSC": "1",
            "Next-Url": path,
        },
    )
    response.raise_for_status()
    return response.text


def _rpc(method: str, params: list[Any]) -> Any:
    response = requests.post(
        SOLANA_RPC,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=20,
        headers={"User-Agent": "PumpScanner/1.0"},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise ScannerError(payload["error"].get("message", "Solana RPC error"))
    return payload.get("result")


def _best_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not pairs:
        return None
    pump_pairs = [p for p in pairs if p.get("dexId") in {"pumpswap", "pumpfun"}]
    candidates = pump_pairs or pairs
    return max(candidates, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def _age_minutes(created_ms: int | None) -> float | None:
    if not created_ms:
        return None
    return max(0.0, (datetime.now(timezone.utc).timestamp() * 1000 - created_ms) / 60_000)


def _timestamp_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _extract_percent(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_present(*values: Any) -> Any:
    """Return the first non-None value while preserving legitimate numeric zeroes."""
    return next((value for value in values if value is not None), None)


def analyze_bot_trades(mint: str, limit: int = 100) -> dict[str, Any]:
    """Summarize the latest minute of PumpSwap trades for automation-like risk signals."""
    safe_limit = max(50, min(int(limit), 100))
    try:
        payload = _get_json(PUMP_SWAP_TRADES.format(mint=mint, limit=safe_limit), timeout=8)
    except (requests.RequestException, ValueError) as exc:
        raise ScannerError(f"Pump trade sample unavailable: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("trades"), list):
        raise ScannerError("Pump trade sample returned an unexpected response")

    parsed: list[dict[str, Any]] = []
    for trade in payload["trades"]:
        if not isinstance(trade, dict):
            continue
        try:
            timestamp = datetime.fromisoformat(str(trade.get("timestamp")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        amount_usd = _extract_percent(trade.get("amountUsd"))
        if amount_usd is None or not math.isfinite(amount_usd) or amount_usd <= 0:
            continue
        wallet_value = trade.get("userAddress")
        wallet = wallet_value.strip() if isinstance(wallet_value, str) else None
        parsed.append({
            "timestamp": timestamp,
            "side": str(trade.get("type") or "").lower(),
            "wallet": wallet or None,
            "amount_usd": amount_usd,
        })

    if not parsed:
        return {"bot_data_status": "NO TRADES"}

    now = datetime.now(timezone.utc)
    latest = max(trade["timestamp"] for trade in parsed)
    latest_age_seconds = max(0.0, (now - latest).total_seconds())
    cutoff_epoch = now.timestamp() - 60.0
    window = [
        trade
        for trade in parsed
        if cutoff_epoch <= trade["timestamp"].timestamp() <= now.timestamp() + 5.0
    ]
    if not window:
        return {
            "bot_data_status": "STALE",
            "raw_latest_trade_at": latest.isoformat(),
            "raw_latest_trade_age_seconds": round(latest_age_seconds, 1),
        }
    buys = [trade for trade in window if trade["side"] == "buy"]
    sells = [trade for trade in window if trade["side"] == "sell"]
    buy_sizes = [trade["amount_usd"] for trade in buys]
    buy_volume = sum(buy_sizes)
    wallet_volume: dict[str, float] = {}
    for trade in buys:
        wallet = trade["wallet"]
        if wallet:
            wallet_volume[wallet] = wallet_volume.get(wallet, 0.0) + trade["amount_usd"]
    ranked_wallet_volume = sorted(wallet_volume.values(), reverse=True)

    truncated = bool(
        len(payload["trades"]) >= safe_limit
        and min(trade["timestamp"].timestamp() for trade in parsed) >= cutoff_epoch
    )

    median_buy = median(buy_sizes) if buy_sizes else None
    return {
        "bot_data_status": "SAMPLED",
        "raw_trade_count_1m": len(window),
        "raw_buys_1m": len(buys),
        "raw_sells_1m": len(sells),
        "raw_unique_buyers_1m": len(wallet_volume),
        "raw_buy_share_pct": round(len(buys) / len(window) * 100.0, 1) if window else None,
        "median_buy_usd": round(median_buy, 4) if median_buy is not None else None,
        "micro_buy_share_pct": (
            round(sum(size < 0.10 for size in buy_sizes) / len(buy_sizes) * 100.0, 1)
            if buy_sizes else None
        ),
        "top_wallet_buy_share_pct": (
            round(ranked_wallet_volume[0] / buy_volume * 100.0, 1)
            if buy_volume > 0 and ranked_wallet_volume else None
        ),
        "top3_wallet_buy_share_pct": (
            round(sum(ranked_wallet_volume[:3]) / buy_volume * 100.0, 1)
            if buy_volume > 0 else None
        ),
        "largest_to_median_buy": (
            round(max(buy_sizes) / median_buy, 1)
            if buy_sizes and median_buy and median_buy > 0 else None
        ),
        "raw_sample_truncated": truncated,
        "raw_latest_trade_at": latest.isoformat(),
        "raw_latest_trade_age_seconds": round(latest_age_seconds, 1),
    }


def _live_viewers(coin: dict[str, Any]) -> int | None:
    """Read the count shown in Pump's live video player for an active stream."""
    if not _is_live(coin):
        return None
    livestream = coin.get("livestream") if isinstance(coin.get("livestream"), dict) else {}
    for value in (
        coin.get("live_viewers"),
        coin.get("viewer_count"),
        coin.get("viewers"),
        coin.get("current_viewers"),
        coin.get("livestream_viewer_count"),
        coin.get("num_participants"),
        coin.get("numParticipants"),
        coin.get("viewerCount"),
        coin.get("livestream_num_participants"),
        livestream.get("viewer_count"),
        livestream.get("viewers"),
        livestream.get("current_viewers"),
        livestream.get("num_participants"),
        livestream.get("numParticipants"),
    ):
        parsed = _extract_int(value)
        if parsed is not None:
            return max(0, parsed)
    return None


def _is_live(coin: dict[str, Any]) -> bool:
    return bool(
        coin.get("is_currently_live")
        or coin.get("is_live")
        or coin.get("isLive")
    )


def _flight_objects(text: str, key: str, *, lookback: int = 8_000):
    """Yield JSON objects containing a key from a Next.js Flight response."""
    decoder = json.JSONDecoder()
    seen: set[tuple[int, int]] = set()
    for hit in re.finditer(rf'"{re.escape(key)}"\s*:', text):
        lower_bound = max(0, hit.start() - lookback)
        starts = [
            match.start() + lower_bound
            for match in re.finditer(r"\{", text[lower_bound:hit.start()])
        ]
        for start in reversed(starts):
            try:
                value, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                continue
            if end < hit.end() or not isinstance(value, dict) or key not in value:
                continue
            if (start, end) not in seen:
                seen.add((start, end))
                yield value
            break


def _live_page_roster() -> dict[str, dict[str, Any]] | None:
    """Read Pump's same-origin live page when the frontend API rejects cloud IPs."""
    global _LIVE_PAGE_CACHE, _LIVE_PAGE_CACHED_AT, _LIVE_PAGE_LAST_ERROR

    now = time.monotonic()
    if _LIVE_PAGE_CACHED_AT and now - _LIVE_PAGE_CACHED_AT < _LIVE_PAGE_TTL_SECONDS:
        return {mint: dict(coin) for mint, coin in _LIVE_PAGE_CACHE.items()}

    try:
        payload = _get_pump_rsc(PUMP_LIVE_PAGE_RSC, "/live")
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        _LIVE_PAGE_LAST_ERROR = f"HTTP {status}" if status else type(exc).__name__
        return (
            {mint: dict(coin) for mint, coin in _LIVE_PAGE_CACHE.items()}
            if _LIVE_PAGE_CACHE else None
        )

    roster: dict[str, dict[str, Any]] = {}
    for item in _flight_objects(payload, "viewerCount"):
        mint = item.get("mint")
        viewers = item.get("viewerCount")
        if (
            item.get("isLive") is True
            and isinstance(mint, str)
            and isinstance(viewers, int)
            and viewers >= 0
        ):
            roster[mint] = {
                **item,
                "mint": mint,
                "is_currently_live": True,
                "num_participants": viewers,
            }

    # A malformed/blocked response must not erase the last known good roster.
    if not roster:
        _LIVE_PAGE_LAST_ERROR = "unexpected response"
        return (
            {mint: dict(coin) for mint, coin in _LIVE_PAGE_CACHE.items()}
            if _LIVE_PAGE_CACHE else None
        )

    _LIVE_PAGE_CACHE = roster
    _LIVE_PAGE_CACHED_AT = now
    _LIVE_PAGE_LAST_ERROR = None
    return {mint: dict(coin) for mint, coin in roster.items()}


def _live_roster() -> dict[str, dict[str, Any]]:
    """Return Pump's active livestream roster, briefly cached across scans."""
    global _LIVE_ROSTER_CACHE, _LIVE_ROSTER_CACHED_AT

    now = time.monotonic()
    if _LIVE_ROSTER_CACHED_AT and now - _LIVE_ROSTER_CACHED_AT < _LIVE_ROSTER_TTL_SECONDS:
        return _LIVE_ROSTER_CACHE

    roster: dict[str, dict[str, Any]] = {}
    successful_lookup = False

    live_page = _live_page_roster()
    if live_page is not None:
        _LIVE_ROSTER_CACHE = live_page
        _LIVE_ROSTER_CACHED_AT = now
        return live_page

    # The broad roster currently contains more live rooms. Pump's sorted live
    # search is fetched second so its displayed participant count wins when a
    # mint appears in both responses.
    for url in (PUMP_CURRENTLY_LIVE, PUMP_LIVE_SEARCH):
        try:
            payload = _get_json(url)
        except (requests.RequestException, ValueError):
            continue
        if not isinstance(payload, list):
            continue
        successful_lookup = True
        for coin in payload:
            if not isinstance(coin, dict) or not coin.get("mint") or not _is_live(coin):
                continue
            mint = str(coin["mint"])
            roster[mint] = coin

    if successful_lookup:
        _LIVE_ROSTER_CACHE = roster
        _LIVE_ROSTER_CACHED_AT = now
        return roster

    return _LIVE_ROSTER_CACHE


def _livestream_status_from_coin_page(mint: str) -> dict[str, Any] | None:
    """Read Pump's server-rendered live status when its JSON host is blocked."""
    path = f"/coin/{mint}"
    try:
        page = _get_pump_rsc(
            PUMP_COIN_PAGE_RSC.format(mint=mint),
            path,
        )
    except requests.RequestException:
        return None

    for item in _flight_objects(page, "numParticipants"):
        viewers = item.get("numParticipants")
        if (
            item.get("mintId") == mint
            and item.get("isLive") is True
            and isinstance(viewers, int)
            and viewers >= 0
        ):
            return {**item, "numParticipants": viewers}
    return None


def _livestream_status(mint: str) -> dict[str, Any] | None:
    """Read Pump's per-mint livestream status with a rate-friendly cache."""
    now = time.monotonic()
    cached = _LIVESTREAM_CACHE.get(mint)
    if cached:
        cache_ttl = (
            _LIVESTREAM_TTL_SECONDS
            if cached[1] is not None
            else _LIVESTREAM_FAILURE_TTL_SECONDS
        )
        if now - cached[0] < cache_ttl:
            return dict(cached[1]) if cached[1] is not None else None

    api_failed = False
    try:
        response = requests.get(
            PUMP_LIVESTREAM.format(mint=mint),
            timeout=6,
            headers={"User-Agent": "PumpScanner/1.0"},
        )
        response.raise_for_status()
        payload = response.json() if response.content.strip() else None
    except requests.RequestException:
        api_failed = True
        payload = None
    except ValueError:
        api_failed = True
        payload = None
    status = dict(payload) if isinstance(payload, dict) else None
    if api_failed:
        status = _livestream_status_from_coin_page(mint)

    if len(_LIVESTREAM_CACHE) >= 500:
        expired = [
            key
            for key, (cached_at, _) in _LIVESTREAM_CACHE.items()
            if now - cached_at >= _LIVESTREAM_TTL_SECONDS
        ]
        for key in expired:
            _LIVESTREAM_CACHE.pop(key, None)
        if len(_LIVESTREAM_CACHE) >= 500:
            oldest = min(_LIVESTREAM_CACHE, key=lambda key: _LIVESTREAM_CACHE[key][0])
            _LIVESTREAM_CACHE.pop(oldest, None)
    _LIVESTREAM_CACHE[mint] = (now, status)
    return dict(status) if status is not None else None


def _enrich_pump_viewer_counts(
    coins: list[dict[str, Any]],
    *,
    max_lookups: int = _MAX_VIEWER_LOOKUPS_PER_SWEEP,
) -> list[dict[str, Any]]:
    """Attach Pump's exact active viewer count without exceeding a sweep budget."""
    enriched = [dict(coin) for coin in coins]
    live_by_mint = _live_roster()
    for coin in enriched:
        mint = str(coin.get("mint") or "")
        live_coin = live_by_mint.get(mint)
        if not live_coin:
            continue
        coin["is_currently_live"] = True
        viewers = _live_viewers(live_coin)
        if viewers is not None:
            coin["num_participants"] = viewers

    # When Pump's same-origin live page loaded, it already supplied the exact
    # active-room counts in one request. Avoid dozens of per-token fallbacks.
    if live_by_mint:
        return enriched

    targets = [
        coin
        for coin in enriched
        if coin.get("mint") and _live_viewers(coin) is None
    ][:max_lookups]
    if not targets:
        return enriched

    worker_count = min(8, len(targets))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        statuses = executor.map(
            _livestream_status,
            [str(coin["mint"]) for coin in targets],
        )
        for coin, status in zip(targets, statuses):
            if status and _is_live(status):
                coin["is_currently_live"] = True
                coin["num_participants"] = status.get("numParticipants")
    return enriched


def _merge_live_status(coin: dict[str, Any], mint: str) -> dict[str, Any]:
    """Enrich a single coin with the same live count used by Pump's live page."""
    merged = dict(coin)
    live_coin = _live_roster().get(mint)
    if live_coin:
        merged["is_currently_live"] = True
        viewers = _live_viewers(live_coin)
        if viewers is not None:
            merged["num_participants"] = viewers
            return merged

    # Pump's per-mint service is useful when a just-started stream has not yet
    # appeared in the roster. It uses camelCase and may return an empty body.
    livestream = _livestream_status(mint)
    if isinstance(livestream, dict) and _is_live(livestream):
        merged["is_currently_live"] = True
        merged["num_participants"] = livestream.get("numParticipants")
    return merged


def _mode_is_active(value: Any) -> bool:
    """Normalize Pump mode values without treating COMPLETED as active."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().upper() in {"ACTIVE", "ENABLED", "RUNNING", "TRUE", "1"}


def _creator_percent(mint: str, creator: str | None) -> float | None:
    if not creator:
        return None
    try:
        supply = _rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
        raw_supply = int(supply["value"]["amount"])
        accounts = _rpc(
            "getTokenAccountsByOwner",
            [creator, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        held = sum(
            int(item["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            for item in accounts.get("value", [])
        )
        return held / raw_supply * 100 if raw_supply else None
    except (requests.RequestException, ScannerError, KeyError, TypeError, ValueError):
        return None


def _top10_percent(mint: str) -> float | None:
    """Return a conservative raw top-10 estimate; pool accounts may inflate it."""
    try:
        supply = _rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
        raw_supply = int(supply["value"]["amount"])
        largest = _rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        top = sum(int(item["amount"]) for item in largest.get("value", [])[:10])
        return top / raw_supply * 100 if raw_supply else None
    except (requests.RequestException, ScannerError, KeyError, TypeError, ValueError):
        return None


def _board_coins(payload: Any) -> list[dict[str, Any]] | None:
    """Translate Pump's compact discovery-board response to the coin shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return None

    server_ms = _extract_int(payload.get("serverTs"))
    coins: list[dict[str, Any]] = []
    for entry in payload["entries"]:
        if not isinstance(entry, dict) or not entry.get("m"):
            continue
        age_seconds = _extract_int(entry.get("age"))
        created_ms = (
            server_ms - max(0, age_seconds) * 1000
            if server_ms is not None and age_seconds is not None
            else None
        )
        coins.append({
            "mint": entry["m"],
            "name": entry.get("n"),
            "symbol": entry.get("t"),
            "image_uri": entry.get("i"),
            "description": entry.get("desc"),
            "usd_market_cap": entry.get("mc"),
            "ath_market_cap": entry.get("ath"),
            "created_timestamp": created_ms,
            "complete": bool(_extract_int(entry.get("gd"))),
            "is_currently_live": bool(entry.get("lv")),
            "num_participants": _extract_int(entry.get("np")),
            "mayhem_mode": bool(entry.get("mh")),
        })
    return coins


def _gecko_pool_coins(payload: Any) -> list[dict[str, Any]] | None:
    """Translate GeckoTerminal pools into self-contained Pump scan rows."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None

    tokens: dict[str, dict[str, Any]] = {}
    for item in payload.get("included") or []:
        if isinstance(item, dict) and item.get("id"):
            tokens[item["id"]] = item.get("attributes") or {}

    coins: list[dict[str, Any]] = []
    for pool in payload["data"]:
        if not isinstance(pool, dict):
            continue
        relationships = pool.get("relationships") or {}
        base_ref = ((relationships.get("base_token") or {}).get("data") or {})
        dex_ref = ((relationships.get("dex") or {}).get("data") or {})
        token_id = base_ref.get("id")
        token = tokens.get(token_id, {})
        mint = token.get("address")
        if not mint and isinstance(token_id, str) and token_id.startswith("solana_"):
            mint = token_id.removeprefix("solana_")
        dex_id = str(dex_ref.get("id") or "").lower()
        if (
            not isinstance(mint, str)
            or not mint.lower().endswith("pump")
            or dex_id not in {"pump-fun", "pumpswap"}
        ):
            continue

        attributes = pool.get("attributes") or {}
        changes = attributes.get("price_change_percentage") or {}
        volume = attributes.get("volume_usd") or {}
        transactions = attributes.get("transactions") or {}
        txns_5m = transactions.get("m5") or {}
        txns_1h = transactions.get("h1") or {}
        txns_24h = transactions.get("h24") or {}
        pair_address = attributes.get("address")
        coins.append({
            "mint": mint,
            "name": token.get("name"),
            "symbol": token.get("symbol"),
            "image_uri": token.get("image_url"),
            "market_cap_usd": attributes.get("market_cap_usd") or attributes.get("fdv_usd"),
            "liquidity_usd": attributes.get("reserve_in_usd"),
            "created_timestamp": _timestamp_ms(attributes.get("pool_created_at")),
            "price_change_5m": changes.get("m5"),
            "price_change_1h": changes.get("h1"),
            "price_change_24h": changes.get("h24"),
            "volume_5m": volume.get("m5"),
            "volume_1h": volume.get("h1"),
            "volume_24h": volume.get("h24"),
            "buys_5m": txns_5m.get("buys"),
            "sells_5m": txns_5m.get("sells"),
            "buys_1h": txns_1h.get("buys"),
            "sells_1h": txns_1h.get("sells"),
            "buys_24h": txns_24h.get("buys"),
            "sells_24h": txns_24h.get("sells"),
            "complete": dex_id == "pumpswap",
            "pair_url": (
                f"https://www.geckoterminal.com/solana/pools/{pair_address}"
                if pair_address else None
            ),
            "_market_snapshot_complete": True,
        })
    return coins


def _gecko_discovery(limit: int, sort_by: str) -> list[dict[str, Any]]:
    """Use an independent market index when Pump blocks cloud-hosted requests."""
    feed = "trending" if sort_by == "last_trade_timestamp" else "new"
    coins: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_count = min(3, (limit + 19) // 20)
    for page in range(1, page_count + 1):
        try:
            payload = _get_json(GECKO_DISCOVERY.format(feed=feed, page=page))
        except (requests.RequestException, ValueError):
            break
        rows = _gecko_pool_coins(payload)
        if rows is None:
            break
        for coin in rows:
            if coin["mint"] in seen:
                continue
            seen.add(coin["mint"])
            coins.append(coin)
            if len(coins) >= limit:
                return coins
    return coins


def discover_recent_mints(limit: int = 20, sort_by: str = "created_timestamp") -> list[dict[str, Any]]:
    """Return Pump launches while tolerating a blocked discovery endpoint."""
    limit = max(5, min(int(limit), 50))
    sort_by = sort_by if sort_by in {"created_timestamp", "last_trade_timestamp"} else "created_timestamp"
    recent = _gecko_discovery(limit, sort_by)
    if recent:
        return _enrich_pump_viewer_counts(recent)

    board = "movers" if sort_by == "last_trade_timestamp" else "new"
    sources = (
        PUMP_RECENT_SEARCH.format(limit=limit, sort_by=sort_by),
        PUMP_DISCOVERY_BOARD.format(board=board, limit=limit),
        PUMP_RECENT_LEGACY.format(limit=limit, sort_by=sort_by),
    )

    recent_fallback: list[dict[str, Any]] | None = None
    for url in sources:
        try:
            payload = _get_json(url)
        except (requests.RequestException, ValueError):
            continue
        if isinstance(payload, list):
            recent_fallback = [
                dict(coin)
                for coin in payload
                if isinstance(coin, dict) and coin.get("mint")
            ]
            break
        board_rows = _board_coins(payload)
        if board_rows is not None:
            recent_fallback = board_rows
            break

    if recent_fallback is None:
        raise ScannerError(
            "Recent-mint discovery is temporarily unavailable. Try another sweep shortly."
        )
    recent = recent_fallback

    # The normal coin feed may omit the live-player participant count. Pump's
    # live page uses the dedicated roster, so merge it once per discovery sweep.
    live_by_mint = _live_roster()
    for coin in recent:
        live_coin = live_by_mint.get(coin["mint"])
        if live_coin:
            coin["is_currently_live"] = True
            viewers = _live_viewers(live_coin)
            if viewers is not None:
                coin["num_participants"] = viewers

    if not live_by_mint:
        recent = _enrich_pump_viewer_counts(recent)

    return recent


def discover_live_mints(limit: int = 20) -> list[dict[str, Any]]:
    """Return active Pump livestreams ranked by current participants."""
    limit = max(5, min(int(limit), 50))
    live = list(_live_roster().values())
    if not live:
        candidates = _enrich_pump_viewer_counts(
            _gecko_discovery(max(30, limit), "last_trade_timestamp")
        )
        live = [coin for coin in candidates if _is_live(coin)]
    if not live:
        if _LIVE_PAGE_LAST_ERROR:
            raise ScannerError(
                "Pump.fun viewer feed is temporarily unavailable "
                f"({_LIVE_PAGE_LAST_ERROR})."
            )
        raise ScannerError("No indexed Pump.fun livestreams are active right now.")
    live.sort(
        key=lambda coin: _extract_int(
            coin.get("num_participants") or coin.get("numParticipants")
        ) or 0,
        reverse=True,
    )
    return [dict(coin) for coin in live[:limit]]


def scan_token(
    mint: str,
    rules: Rules,
    *,
    manual: dict[str, float | int | None] | None = None,
    coin_data: dict[str, Any] | None = None,
    include_onchain: bool = True,
) -> ScanResult:
    mint = mint.strip()
    if not mint:
        raise ScannerError("Enter a mint address.")
    if mint.startswith("0x"):
        raise ScannerError("This scanner currently supports Solana mints only.")

    if coin_data is None:
        try:
            coin = _get_json(PUMP_COIN.format(mint=mint))
        except (requests.RequestException, ValueError):
            coin = {"mint": mint}
        coin = _merge_live_status(coin, mint)
    else:
        coin = coin_data

    if coin.get("_market_snapshot_complete"):
        pairs = []
    else:
        try:
            pairs = _get_json(DEX_TOKEN_PAIRS.format(mint=mint))
        except requests.RequestException:
            pairs = []
    pair = _best_pair(pairs if isinstance(pairs, list) else [])
    manual = manual or {}

    created_ms = coin.get("created_timestamp")
    pair_created_ms = pair.get("pairCreatedAt") if pair else None
    txns = (pair or {}).get("txns") or {}
    changes = (pair or {}).get("priceChange") or {}
    volume = (pair or {}).get("volume") or {}
    liquidity = (pair or {}).get("liquidity") or {}
    base_token = (pair or {}).get("baseToken") or {}

    result = ScanResult(
        mint=mint,
        symbol=coin.get("symbol") or base_token.get("symbol") or "Unknown",
        name=coin.get("name") or base_token.get("name") or "Unknown",
        market_cap_usd=_extract_percent((pair or {}).get("marketCap") or coin.get("market_cap_usd") or coin.get("usd_market_cap")),
        ath_market_cap_usd=_extract_percent(coin.get("ath_market_cap")),
        liquidity_usd=_extract_percent(liquidity.get("usd") or coin.get("liquidity_usd")),
        age_minutes=_age_minutes(created_ms or pair_created_ms),
        live_viewers=_live_viewers(coin),
        is_currently_live=_is_live(coin),
        reply_count=_extract_int(coin.get("reply_count")),
        price_change_5m=_extract_percent(changes.get("m5") or coin.get("price_change_5m")),
        price_change_1h=_extract_percent(changes.get("h1") or coin.get("price_change_1h")),
        price_change_24h=_extract_percent(changes.get("h24") or coin.get("price_change_24h")),
        volume_5m=_extract_percent(_first_present(volume.get("m5"), coin.get("volume_5m"))),
        volume_1h=_extract_percent(_first_present(volume.get("h1"), coin.get("volume_1h"))),
        volume_24h=_extract_percent(volume.get("h24") or coin.get("volume_24h")),
        buys_5m=_extract_int(
            _first_present((txns.get("m5") or {}).get("buys"), coin.get("buys_5m"))
        ),
        sells_5m=_extract_int(
            _first_present((txns.get("m5") or {}).get("sells"), coin.get("sells_5m"))
        ),
        buys_1h=_extract_int(
            _first_present((txns.get("h1") or {}).get("buys"), coin.get("buys_1h"))
        ),
        sells_1h=_extract_int(
            _first_present((txns.get("h1") or {}).get("sells"), coin.get("sells_1h"))
        ),
        buys_24h=(txns.get("h24") or {}).get("buys") or coin.get("buys_24h"),
        sells_24h=(txns.get("h24") or {}).get("sells") or coin.get("sells_24h"),
        top10_percent=(
            manual.get("top10_percent")
            if manual.get("top10_percent") is not None
            else (_top10_percent(mint) if include_onchain else None)
        ),
        creator_percent=(
            manual.get("creator_percent")
            if manual.get("creator_percent") is not None
            else (_creator_percent(mint, coin.get("creator")) if include_onchain else None)
        ),
        holder_count=manual.get("holder_count"),
        sniper_percent=manual.get("sniper_percent"),
        bundler_percent=manual.get("bundler_percent"),
        complete=bool(coin.get("complete") or (pair or {}).get("dexId") == "pumpswap"),
        boost_mode=_mode_is_active(coin.get("boost_mode")),
        mayhem_mode=_mode_is_active(coin.get("mayhem_mode") or coin.get("is_mayhem_mode")),
        website=coin.get("website"),
        social=coin.get("twitter"),
        pair_url=(pair or {}).get("url") or coin.get("pair_url"),
    )
    return evaluate(result, rules)


def evaluate(result: ScanResult, rules: Rules) -> ScanResult:
    failures: list[str] = []
    cautions: list[str] = []
    positives: list[str] = []

    if rules.reject_boost and result.boost_mode:
        failures.append("BOOST mode is active")
    if rules.reject_mayhem and result.mayhem_mode:
        failures.append("Mayhem mode is active")
    if result.age_minutes is not None and result.age_minutes < rules.minimum_age_minutes:
        failures.append(f"Only {result.age_minutes:.0f} minutes old")
    if result.complete and result.liquidity_usd is not None and result.liquidity_usd < rules.minimum_liquidity_usd:
        failures.append(f"Liquidity is below ${rules.minimum_liquidity_usd:,.0f}")
    if result.top10_percent is not None and result.top10_percent > rules.maximum_top10_percent:
        failures.append(f"Raw top-10 concentration is {result.top10_percent:.1f}%")
    if result.creator_percent is not None and result.creator_percent > rules.maximum_creator_percent:
        failures.append(f"Creator holds {result.creator_percent:.1f}%")
    if result.bundler_percent is not None and result.bundler_percent > rules.maximum_bundler_percent:
        failures.append(f"Bundlers are {result.bundler_percent:.1f}%")
    if result.holder_count is not None and result.holder_count < rules.minimum_holder_count:
        failures.append(f"Only {result.holder_count:,} holders")
    if result.price_change_5m is not None and result.price_change_5m > rules.maximum_five_minute_change:
        cautions.append(f"Five-minute move is +{result.price_change_5m:.1f}%")
    if not result.complete:
        cautions.append("Bonding curve has not graduated")
    if result.market_cap_usd and result.ath_market_cap_usd:
        drawdown = (1 - result.market_cap_usd / result.ath_market_cap_usd) * 100
        if drawdown > 65:
            cautions.append(f"Market cap is {drawdown:.0f}% below ATH")
    if result.liquidity_usd is not None and result.liquidity_usd >= rules.minimum_liquidity_usd:
        positives.append("Liquidity clears the minimum")
    if result.creator_percent is not None and result.creator_percent <= rules.maximum_creator_percent:
        positives.append("Creator exposure is within limit")
    if result.price_change_5m is not None and -8 <= result.price_change_5m <= 12:
        positives.append("Five-minute momentum is controlled")

    unknown_cluster = result.sniper_percent is None or result.bundler_percent is None
    if unknown_cluster:
        cautions.append("Sniper/bundler data needs manual verification")
    if result.holder_count is None:
        cautions.append("Holder count needs manual verification")

    if failures:
        verdict = "HARD PASS" if result.boost_mode or len(failures) >= 2 else "PASS"
    elif cautions:
        verdict = "WATCH"
    else:
        verdict = "ENTRY ELIGIBLE"

    score = max(0, min(100, 70 + len(positives) * 8 - len(cautions) * 7 - len(failures) * 22))
    result.verdict = verdict
    result.score = score
    result.reasons = failures + positives
    result.warnings = cautions
    return result
