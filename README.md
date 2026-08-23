# Pump Scanner

The **Discover** tab automatically pulls the newest non-NSFW Pump.fun mints and runs a fast market-data screen. Choose a ranked candidate and click **Deep scan candidate** to add the slower on-chain concentration checks—no mint-address copying required.

Use **Automatic sweep** to refresh the discovery results every 1–15 minutes. The Discover view must remain open; choose **Off** to disable scheduled API activity.

The default **Aggressive** profile scans 50 actively traded mints, allows ungraduated launches, and uses a 0% five-minute / $250 volume floor. **Live now** ranks indexed active livestreams by their current Pump.fun viewer count. **Top movers** and **Risk-first** remain available for other screening styles.

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
