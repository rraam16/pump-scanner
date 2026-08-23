# Pump Scanner

The **Discover** tab automatically pulls the newest non-NSFW Pump.fun mints and runs a fast market-data screen. Choose a ranked candidate and click **Deep scan candidate** to add the slower on-chain concentration checks—no mint-address copying required.

Use **Automatic sweep** to refresh the discovery results every 1–15 minutes. The Discover view must remain open; choose **Off** to disable scheduled API activity.

The default **Early runners** profile scans 50 actively traded mints every minute and looks for an entry-stage pattern: ungraduated tokens between $8K and $30K market cap, 15–120 minutes old, with +5% to +800% five-minute momentum, at least $1K of five-minute volume, five-minute turnover equal to at least 12% of market cap, 25+ recent trades, and at least 1.4 buys per sell. Its similarity rank favors the $10K–$20K range and combines market-cap fit, age, momentum, recent turnover, recent order flow, and activity. Moves above +65% remain visible but are labeled **EXTENDED**. Viewer count remains visible but does not influence the rank.

Open **Early-runner filters** to adjust the market-cap, age, momentum, turnover, trade-count, and buy/sell requirements. Turn off **Ungraduated only** to include graduated tokens with at least $8K liquidity and a 15% liquidity-to-market-cap ratio.

The **BOT trades** profile is modeled on the launch behavior of `ufteZkSALGhT9NwE34UHNUyxinJGgAFLMmmtfZepump`. It looks for graduated tokens with at least 200 trades in five minutes, at least 80% buys, and a fresh raw-trade sample showing a micro-buy swarm and wallet-dollar concentration. Its default raw thresholds are 40 trades in the latest minute, a median buy no larger than $0.25, at least 50% of buys below $0.10, and at least 50% of buy dollars from the top wallet. Raw sampling is limited to the ten busiest aggregate matches per sweep so the one-minute schedule remains responsive.

**BOT trades is a manipulation-risk warning, not a buy profile.** Aggregate counts can suggest automation but cannot prove who controls a wallet; candidates without a fresh raw sample are labeled **AGGREGATE ONLY**. **Aggressive**, **Top movers**, **Live now**, and **Risk-first** remain available and keep their existing behavior. **Live now** ranks indexed active livestreams by their current Pump.fun viewer count.

The quick sweep is a discovery filter, not an entry signal. Holder, sniper, bundler, and concentration data may still require a deep scan or independent verification.

A decision-support dashboard for Solana Pump.fun tokens.

## Deploy

The app is ready for Streamlit Community Cloud. Use `streamlit_app.py` as the entry point. No API keys are required.

## Run it

Open PowerShell in this folder and run:

```powershell
.\launch.ps1
```

The app opens at `http://localhost:8502`.

## What it does

- Screens a Solana mint using Pump metadata, DEX Screener market data, and Solana RPC.
- Shows token age and the current Pump.fun viewer count for active livestreams.
- Applies configurable age, liquidity, concentration, creator, bundler, and momentum limits.
- Maintains a watchlist and ranks candidates.
- Tracks positions entered by the user.
- Never connects to a wallet or places trades.

## Important limitation

Sniper and bundler classification is not reliably exposed through the public APIs used here. Enter those values from Pump's Audit panel when available. The automatic top-10 figure is a conservative raw RPC estimate and may include pool-owned token accounts; override it with Pump's audit figure for better decisions.
