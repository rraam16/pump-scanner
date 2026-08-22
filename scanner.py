"""Live data adapters and deterministic screening rules for Pump Scanner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

DEX_TOKEN_PAIRS = "https://api.dexscreener.com/token-pairs/v1/solana/{mint}"
PUMP_COIN = "https://frontend-api-v3.pump.fun/coins/{mint}"
PUMP_CURRENTLY_LIVE = (
    "https://frontend-api-v3.pump.fun/coins/currently-live"
    "?offset=0&limit=1000&includeNsfw=false"
)
PUMP_RECENT = (
    "https://frontend-api-v3.pump.fun/coins"
    "?offset=0&limit={limit}&sort={sort_by}&order=DESC&includeNsfw=false"
)
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


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
    volume_24h: float | None = None
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


def _live_viewers(coin: dict[str, Any]) -> int | None:
    """Read the count shown in Pump's live video player for an active stream."""
    if not bool(coin.get("is_currently_live") or coin.get("is_live")):
        return None
    livestream = coin.get("livestream") if isinstance(coin.get("livestream"), dict) else {}
    for value in (
        coin.get("live_viewers"),
        coin.get("viewer_count"),
        coin.get("viewers"),
        coin.get("current_viewers"),
        coin.get("livestream_viewer_count"),
        coin.get("num_participants"),
        livestream.get("viewer_count"),
        livestream.get("viewers"),
        livestream.get("current_viewers"),
    ):
        parsed = _extract_int(value)
        if parsed is not None:
            return max(0, parsed)
    return None


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


def discover_recent_mints(limit: int = 20, sort_by: str = "created_timestamp") -> list[dict[str, Any]]:
    """Return active non-NSFW Pump.fun launches from the public feed."""
    limit = max(5, min(int(limit), 50))
    sort_by = sort_by if sort_by in {"created_timestamp", "last_trade_timestamp"} else "created_timestamp"
    try:
        coins = _get_json(PUMP_RECENT.format(limit=limit, sort_by=sort_by))
    except requests.RequestException as exc:
        raise ScannerError(f"Recent-mint discovery failed: {exc}") from exc
    if not isinstance(coins, list):
        raise ScannerError("Pump returned an unexpected recent-mint response.")
    recent = [coin for coin in coins if isinstance(coin, dict) and coin.get("mint")]

    # The normal coin feed may omit the live-player participant count. Pump's
    # dedicated live feed includes it, so merge that small lookup once per sweep.
    try:
        live_coins = _get_json(PUMP_CURRENTLY_LIVE)
        live_by_mint = {
            coin["mint"]: coin
            for coin in live_coins
            if isinstance(coin, dict) and coin.get("mint")
        } if isinstance(live_coins, list) else {}
        for coin in recent:
            live_coin = live_by_mint.get(coin["mint"])
            if live_coin:
                coin["is_currently_live"] = True
                coin["num_participants"] = live_coin.get("num_participants")
    except requests.RequestException:
        pass

    return recent


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
        except requests.RequestException as exc:
            raise ScannerError(f"Pump coin lookup failed: {exc}") from exc
    else:
        coin = coin_data

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

    result = ScanResult(
        mint=mint,
        symbol=coin.get("symbol") or "Unknown",
        name=coin.get("name") or "Unknown",
        market_cap_usd=_extract_percent((pair or {}).get("marketCap") or coin.get("market_cap_usd") or coin.get("usd_market_cap")),
        ath_market_cap_usd=_extract_percent(coin.get("ath_market_cap")),
        liquidity_usd=_extract_percent(liquidity.get("usd")),
        age_minutes=_age_minutes(created_ms or pair_created_ms),
        live_viewers=_live_viewers(coin),
        is_currently_live=bool(coin.get("is_currently_live") or coin.get("is_live")),
        reply_count=_extract_int(coin.get("reply_count")),
        price_change_5m=_extract_percent(changes.get("m5")),
        price_change_1h=_extract_percent(changes.get("h1")),
        price_change_24h=_extract_percent(changes.get("h24")),
        volume_24h=_extract_percent(volume.get("h24")),
        buys_24h=(txns.get("h24") or {}).get("buys"),
        sells_24h=(txns.get("h24") or {}).get("sells"),
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
        complete=bool(coin.get("complete")),
        boost_mode=_mode_is_active(coin.get("boost_mode")),
        mayhem_mode=_mode_is_active(coin.get("mayhem_mode") or coin.get("is_mayhem_mode")),
        website=coin.get("website"),
        social=coin.get("twitter"),
        pair_url=(pair or {}).get("url"),
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
