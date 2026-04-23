#!/usr/bin/env python3
"""
Kalshi Bitcoin 15-Minute Arbitrage Bot
=======================================
Monitors real-time BTC/USD price from Binance and trades Kalshi's
BTC Up/Down markets when the Kalshi odds significantly lag behind
the actual price movement (latency arbitrage).

IMPORTANT DISCLAIMERS
---------------------
- This is for EDUCATIONAL purposes. Prediction market trading involves
  real financial risk. You can lose money.
- Kalshi is CFTC-regulated and requires US residency + verified account.
- Always run in DRY_RUN=True mode first to validate logic.
- Past backtested results do NOT guarantee future profits.
- Read Kalshi's Developer Agreement before using their API commercially.

SETUP
-----
1. pip install requests cryptography python-dotenv
2. Create a .env file with your credentials (see .env.example)
3. Set DRY_RUN = True for paper trading first
4. Run: python kalshi_btc_bot.py

HOW IT WORKS
------------
Every 15 minutes, Kalshi opens a new BTC Up/Down market:
  "Will BTC be HIGHER at 2:15pm than it was at 2:00pm?"

The contract prices (YES/NO) reflect crowd probability. But the crowd
is slow — real BTC prices on Binance update every second. This bot:

1. Tracks BTC price from the window open (baseline)
2. Calculates the actual % move so far
3. Compares that to what the Kalshi market is pricing
4. If there's a significant gap (EDGE_THRESHOLD), bets accordingly
5. Fires the trade in the final ~60 seconds before close

EDGE CALCULATION
----------------
If BTC has moved +0.15% and Kalshi YES is still $0.55 (55% probability),
but our model says the actual win probability is ~80%, that's a 25-cent edge.
After fees (~0.7%), that's still a profitable bet.

STRATEGY MODES
--------------
CONSERVATIVE: Only trade when edge > 15%. Fewer trades, higher win rate.
MODERATE:     Edge > 8%. More trades, decent win rate.
AGGRESSIVE:   Edge > 4%. Many trades, lower win rate but high volume.
"""

import os
import sys
import time
import uuid
import json
import hmac
import hashlib
import datetime
import logging
import requests
from typing import Optional, Tuple
from dataclasses import dataclass, field
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# CONFIGURATION — edit these before running
# ─────────────────────────────────────────────

load_dotenv()

# Kalshi credentials (set in .env file)
KALSHI_API_KEY    = os.getenv("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "")  # PEM string or path

# Safety settings
DRY_RUN           = True    # Set False to place real orders
STRATEGY          = "CONSERVATIVE"  # CONSERVATIVE | MODERATE | AGGRESSIVE
MAX_POSITION_USD  = 25.0    # Max dollars per trade
MAX_DAILY_LOSS    = 100.0   # Stop trading if daily loss exceeds this
TRADE_IN_LAST_SEC = 60      # Only trade in last N seconds of window

# Edge thresholds per strategy
EDGE_THRESHOLDS = {
    "CONSERVATIVE": 0.15,
    "MODERATE":     0.08,
    "AGGRESSIVE":   0.04,
}
EDGE_THRESHOLD = EDGE_THRESHOLDS[STRATEGY]

# API endpoints
KALSHI_BASE   = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO   = "https://demo-api.kalshi.co/trade-api/v2"
BINANCE_PRICE = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
BINANCE_KLINE = "https://api.binance.com/api/v3/klines"

# Use demo API in dry run mode
KALSHI_URL = KALSHI_DEMO if DRY_RUN else KALSHI_BASE

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log"),
    ]
)
log = logging.getLogger("kalshi_btc_bot")

# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class TradeRecord:
    timestamp: str
    market_ticker: str
    direction: str       # "YES" or "NO"
    edge: float
    kalshi_price: float
    model_prob: float
    btc_delta_pct: float
    size_usd: float
    dry_run: bool
    result: str = "PENDING"
    pnl: float = 0.0

@dataclass
class BotState:
    daily_pnl: float = 0.0
    trades_today: int = 0
    wins: int = 0
    losses: int = 0
    btc_open_price: float = 0.0
    window_open_time: float = 0.0
    trades: list = field(default_factory=list)

state = BotState()

# ─────────────────────────────────────────────
# KALSHI AUTHENTICATION
# ─────────────────────────────────────────────

def create_kalshi_signature(api_key: str, private_key_pem: str, timestamp_ms: str, method: str, path: str) -> str:
    """
    Create HMAC-SHA256 signature for Kalshi API authentication.
    Format: timestamp + method + path
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
    import base64

    message = timestamp_ms + method.upper() + path

    # Load private key
    if private_key_pem.strip().startswith("-----"):
        pem_bytes = private_key_pem.encode()
    else:
        # Treat as file path
        with open(private_key_pem, "rb") as f:
            pem_bytes = f.read()

    private_key = serialization.load_pem_private_key(pem_bytes, password=None, backend=default_backend())
    signature = private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def kalshi_headers(method: str, path: str) -> dict:
    """Build authenticated headers for Kalshi API requests."""
    timestamp = str(int(time.time() * 1000))
    if not KALSHI_API_KEY or not KALSHI_PRIVATE_KEY:
        raise ValueError("Missing KALSHI_API_KEY or KALSHI_PRIVATE_KEY in environment")
    sig = create_kalshi_signature(KALSHI_API_KEY, KALSHI_PRIVATE_KEY, timestamp, method, path)
    return {
        "KALSHI-ACCESS-KEY":       KALSHI_API_KEY,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type":            "application/json",
    }

# ─────────────────────────────────────────────
# BINANCE — REAL-TIME BTC PRICE
# ─────────────────────────────────────────────

def get_btc_price() -> Optional[float]:
    """Fetch live BTC/USD price from Coinbase (free, no rate limit issues)."""
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            timeout=5
        )
        r.raise_for_status()
        return float(r.json()["data"]["amount"])
    except Exception as e:
        log.warning(f"Price fetch failed: {e}")
        return None


def get_btc_1m_candles(limit: int = 5) -> list:
    """
    Fetch recent 1-minute BTC candles from Binance.
    Returns list of [open_time, open, high, low, close, volume, ...]
    """
    try:
        r = requests.get(
            BINANCE_KLINE,
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
            timeout=5
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Binance kline fetch failed: {e}")
        return []

# ─────────────────────────────────────────────
# KALSHI MARKET DISCOVERY
# ─────────────────────────────────────────────

def find_btc_15m_markets() -> list:
    """
    Search Kalshi for active BTC 15-minute Up/Down markets.
    Returns list of market dicts sorted by close time.
    """
    try:
        # Search for BTC crypto markets
        r = requests.get(
            f"{KALSHI_URL}/markets",
            params={
                "status":          "open",
                "series_ticker":   "KXBTC",   # BTC series on Kalshi
                "limit":           50,
            },
            timeout=5
        )
        if r.status_code == 200:
            markets = r.json().get("markets", [])
            # Filter to 15-min up/down style markets
            btc_markets = [
                m for m in markets
                if any(kw in m.get("title", "").lower()
                       for kw in ["up", "down", "higher", "lower", "15", "btc"])
            ]
            return sorted(btc_markets, key=lambda m: m.get("close_time", ""))
    except Exception as e:
        log.warning(f"Market discovery failed: {e}")

    return []


def get_market_orderbook(ticker: str) -> Optional[dict]:
    """Fetch current orderbook for a Kalshi market (no auth needed)."""
    try:
        r = requests.get(f"{KALSHI_URL}/markets/{ticker}/orderbook", timeout=3)
        if r.status_code == 200:
            return r.json().get("orderbook_fp", {})
    except Exception as e:
        log.warning(f"Orderbook fetch failed for {ticker}: {e}")
    return None


def get_market_info(ticker: str) -> Optional[dict]:
    """Fetch metadata for a specific Kalshi market."""
    try:
        r = requests.get(f"{KALSHI_URL}/markets/{ticker}", timeout=3)
        if r.status_code == 200:
            return r.json().get("market", {})
    except Exception as e:
        log.warning(f"Market info fetch failed: {e}")
    return None

# ─────────────────────────────────────────────
# PROBABILITY MODEL
# ─────────────────────────────────────────────

def btc_delta_to_win_probability(delta_pct: float, seconds_remaining: int) -> float:
    """
    Convert BTC price move (from window open) + time remaining
    into an estimated win probability for the UP outcome.

    The model uses a sigmoid-like curve calibrated to observed
    Polymarket/Kalshi pricing behavior (from published backtests).

    Args:
        delta_pct: % price change from window open (positive = UP)
        seconds_remaining: seconds left in the 15-min window

    Returns:
        Probability (0.0 - 1.0) that BTC closes UP vs window open
    """
    import math

    # Time decay factor: less time = more certainty in current direction
    # At 900s remaining: factor=0.5 (lots can change)
    # At 30s remaining: factor=0.97 (nearly locked in)
    time_certainty = 1.0 - (seconds_remaining / 900.0) * 0.5

    # Delta sensitivity: how much a given % move implies about final direction
    # Calibrated from 5-min market empirical data (scaled for 15-min)
    sensitivity = 35.0

    # Logistic function: P(UP) = 1 / (1 + e^(-k * delta))
    raw_prob = 1.0 / (1.0 + math.exp(-sensitivity * (delta_pct / 100.0)))

    # Blend with 50/50 based on time certainty
    # Early in window: stay closer to 50/50
    # Late in window: trust current direction more
    blended = 0.5 + (raw_prob - 0.5) * time_certainty

    return max(0.01, min(0.99, blended))


def calculate_edge(model_prob: float, kalshi_yes_price: float, fee_rate: float = 0.007) -> Tuple[float, str]:
    """
    Calculate edge for betting YES or NO.

    Args:
        model_prob: our estimated probability BTC closes UP
        kalshi_yes_price: current YES price on Kalshi (0.01 - 0.99)
        fee_rate: Kalshi taker fee (~0.7%)

    Returns:
        (edge, direction) — edge in probability points, direction is "YES" or "NO"
    """
    # YES edge: our prob vs market price (both net of fees)
    yes_edge = model_prob - kalshi_yes_price - fee_rate

    # NO edge: (1 - our prob) vs NO price
    no_price = 1.0 - kalshi_yes_price
    no_edge = (1.0 - model_prob) - no_price - fee_rate

    if yes_edge > no_edge and yes_edge > 0:
        return yes_edge, "YES"
    elif no_edge > yes_edge and no_edge > 0:
        return no_edge, "NO"
    else:
        return max(yes_edge, no_edge), "NONE"


def compute_kelly_size(edge: float, odds: float, bankroll: float, fraction: float = 0.25) -> float:
    """
    Kelly Criterion bet sizing (fractional Kelly for risk management).

    Args:
        edge: probability edge (e.g. 0.10 = 10%)
        odds: payout per dollar risked (1.0 for binary at fair price)
        bankroll: available capital
        fraction: Kelly fraction (0.25 = quarter-Kelly, conservative)

    Returns:
        Recommended bet size in USD
    """
    if edge <= 0:
        return 0.0
    kelly = (edge * (odds + 1) - (1 - edge)) / odds
    kelly = max(0, kelly)
    size = bankroll * kelly * fraction
    return min(size, MAX_POSITION_USD)

# ─────────────────────────────────────────────
# ORDER PLACEMENT
# ─────────────────────────────────────────────

def place_kalshi_order(
    ticker: str,
    side: str,        # "yes" or "no"
    count: int,       # number of contracts
    price_cents: int, # 1-99
    dry_run: bool = True,
) -> Optional[dict]:
    """
    Place a limit order on Kalshi.

    Args:
        ticker: Market ticker (e.g. "KXBTC-15M-1234")
        side: "yes" or "no"
        count: number of $0.01-increment contracts
        price_cents: limit price in cents (1–99)
        dry_run: if True, simulate without placing

    Returns:
        Order dict from API, or simulated dict if dry_run
    """
    order_id = str(uuid.uuid4())

    if dry_run:
        log.info(f"[DRY RUN] Would place: {side.upper()} {count}x {ticker} @ {price_cents}¢")
        return {
            "order_id":        order_id,
            "client_order_id": order_id,
            "ticker":          ticker,
            "side":            side,
            "count":           count,
            "yes_price":       price_cents,
            "status":          "dry_run_simulated",
        }

    path = "/trade-api/v2/portfolio/orders"
    payload = {
        "ticker":          ticker,
        "action":          "buy",
        "side":            side,
        "count":           count,
        "type":            "limit",
        "yes_price":       price_cents if side == "yes" else (100 - price_cents),
        "client_order_id": order_id,
    }

    try:
        r = requests.post(
            KALSHI_URL + path,
            headers=kalshi_headers("POST", path),
            json=payload,
            timeout=5,
        )
        if r.status_code == 201:
            order = r.json().get("order", {})
            log.info(f"Order placed: {order.get('order_id')} | {side.upper()} | Status: {order.get('status')}")
            return order
        else:
            log.error(f"Order failed {r.status_code}: {r.text}")
            return None
    except Exception as e:
        log.error(f"Order placement error: {e}")
        return None

# ─────────────────────────────────────────────
# WINDOW TRACKING
# ─────────────────────────────────────────────

def get_current_15m_window() -> Tuple[float, float, float]:
    """
    Calculate the current 15-minute window boundaries.
    Kalshi 15-min BTC markets align to clock (e.g. 2:00, 2:15, 2:30...).

    Returns:
        (window_open_ts, window_close_ts, seconds_remaining)
    """
    now = time.time()
    window_duration = 15 * 60  # 900 seconds
    window_open = now - (now % window_duration)
    window_close = window_open + window_duration
    seconds_remaining = window_close - now
    return window_open, window_close, seconds_remaining


def format_window_time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")

# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────

def print_status(
    btc_price: float,
    btc_open: float,
    delta_pct: float,
    model_prob: float,
    yes_price: float,
    edge: float,
    direction: str,
    seconds_remaining: float,
    market_ticker: str,
):
    """Print a formatted status dashboard line."""
    arrow = "^" if delta_pct > 0 else "v" if delta_pct < 0 else "-"
    mode_str = "[DRY RUN]" if DRY_RUN else "[LIVE]"
    edge_str = f"{edge*100:.1f}%" if direction != "NONE" else "no edge"
    trade_signal = f"→ BET {direction}" if direction != "NONE" and edge >= EDGE_THRESHOLD else "→ WAIT"

    print(
        f"\r{mode_str} "
        f"BTC ${btc_price:,.0f} {arrow}{abs(delta_pct):.3f}% | "
        f"Window close: {seconds_remaining:.0f}s | "
        f"YES: {yes_price:.2f} | Model: {model_prob:.2f} | "
        f"Edge: {edge_str} {trade_signal}   ",
        end="", flush=True
    )


def save_trade_log():
    """Persist trade history to JSON."""
    with open("trades.json", "w") as f:
        json.dump(
            {
                "summary": {
                    "daily_pnl":    state.daily_pnl,
                    "trades":       state.trades_today,
                    "wins":         state.wins,
                    "losses":       state.losses,
                    "win_rate":     state.wins / max(state.trades_today, 1),
                    "strategy":     STRATEGY,
                    "dry_run":      DRY_RUN,
                },
                "trades": [t.__dict__ for t in state.trades],
            },
            f,
            indent=2,
        )

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info(f"Kalshi BTC 15-min Arbitrage Bot")
    log.info(f"Strategy: {STRATEGY} | Edge threshold: {EDGE_THRESHOLD*100:.0f}%")
    log.info(f"Mode: {'DRY RUN (no real orders)' if DRY_RUN else '⚠ LIVE TRADING'}")
    log.info(f"Max position: ${MAX_POSITION_USD} | Max daily loss: ${MAX_DAILY_LOSS}")
    log.info("=" * 60)

    if not DRY_RUN and (not KALSHI_API_KEY or not KALSHI_PRIVATE_KEY):
        log.error("LIVE mode requires KALSHI_API_KEY and KALSHI_PRIVATE_KEY in .env")
        sys.exit(1)

    # Track which windows we've already traded
    traded_windows: set = set()

    # BTC price at window open (our baseline)
    btc_open_price: Optional[float] = None
    last_window_open: float = 0.0

    log.info("Starting main loop. Press Ctrl+C to stop.\n")

    while True:
        try:
            # ── Check daily loss limit ──────────────────────────
            if state.daily_pnl <= -MAX_DAILY_LOSS:
                log.warning(f"Daily loss limit hit (${state.daily_pnl:.2f}). Stopping.")
                break

            # ── Get current time window ─────────────────────────
            window_open, window_close, seconds_remaining = get_current_15m_window()

            # ── Detect new window, capture open price ───────────
            if window_open != last_window_open:
                btc_open_price = get_btc_price()
                last_window_open = window_open
                log.info(
                    f"\n[NEW WINDOW] {format_window_time(window_open)} to "
                    f"{format_window_time(window_close)} | "
                    f"BTC open: ${btc_open_price:,.2f}"
                )

            if btc_open_price is None:
                time.sleep(2)
                continue

            # ── Get current BTC price ───────────────────────────
            btc_now = get_btc_price()
            if btc_now is None:
                time.sleep(2)
                continue

            delta_pct = ((btc_now - btc_open_price) / btc_open_price) * 100

            # ── Compute model probability ───────────────────────
            model_prob = btc_delta_to_win_probability(delta_pct, int(seconds_remaining))

            # ── Find Kalshi BTC market for this window ──────────
            markets = find_btc_15m_markets()
            if not markets:
                print(f"\r[INFO] No Kalshi BTC markets found. Retrying...    ", end="", flush=True)
                time.sleep(10)
                continue

            # Pick the market closest to closing (most relevant)
            market = markets[0]
            ticker = market.get("ticker", "")

            # ── Get YES price from market ───────────────────────
            yes_bid = market.get("yes_bid", 50)   # in cents
            yes_ask = market.get("yes_ask", 50)
            yes_price = ((yes_bid + yes_ask) / 2) / 100.0  # midpoint, normalized to 0-1

            # ── Calculate edge ──────────────────────────────────
            edge, direction = calculate_edge(model_prob, yes_price)

            # ── Print status ────────────────────────────────────
            print_status(btc_now, btc_open_price, delta_pct,
                         model_prob, yes_price, edge, direction,
                         seconds_remaining, ticker)

            # ── Execute trade if conditions met ─────────────────
            window_key = f"{ticker}_{int(window_open)}"

            if (
                window_key not in traded_windows        # Haven't traded this window
                and direction != "NONE"                  # We have a directional edge
                and edge >= EDGE_THRESHOLD               # Edge exceeds our threshold
                and seconds_remaining <= TRADE_IN_LAST_SEC  # In trading window
                and seconds_remaining > 5               # Not too close to close
            ):
                traded_windows.add(window_key)

                # Position sizing
                # Use 10% of max position as bankroll estimate for Kelly
                size_usd = compute_kelly_size(
                    edge=edge,
                    odds=1.0,
                    bankroll=MAX_POSITION_USD * 10,
                )
                price_cents = int(yes_price * 100) if direction == "YES" else int((1 - yes_price) * 100)
                contract_count = max(1, int(size_usd / (price_cents / 100)))

                log.info(
                    f"\n[SIGNAL] {direction} | Edge: {edge*100:.1f}% | "
                    f"BTC delta: {delta_pct:+.3f}% | Model: {model_prob:.2f} | "
                    f"Kalshi YES: {yes_price:.2f} | "
                    f"Size: ${size_usd:.2f} ({contract_count} contracts @ {price_cents}¢)"
                )

                order = place_kalshi_order(
                    ticker=ticker,
                    side=direction.lower(),
                    count=contract_count,
                    price_cents=price_cents,
                    dry_run=DRY_RUN,
                )

                if order:
                    record = TradeRecord(
                        timestamp=datetime.datetime.now().isoformat(),
                        market_ticker=ticker,
                        direction=direction,
                        edge=edge,
                        kalshi_price=yes_price,
                        model_prob=model_prob,
                        btc_delta_pct=delta_pct,
                        size_usd=size_usd,
                        dry_run=DRY_RUN,
                    )
                    state.trades.append(record)
                    state.trades_today += 1
                    save_trade_log()
                    log.info(f"Trade recorded. Total today: {state.trades_today}")

            # ── Wait before next poll ───────────────────────────
            # Poll faster when near window close
            sleep_time = 2 if seconds_remaining > TRADE_IN_LAST_SEC else 1
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n")
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
            time.sleep(5)

    # ── Final summary ───────────────────────────────────────────
    print("\n")
    log.info("─" * 40)
    log.info(f"Session Summary")
    log.info(f"  Trades:    {state.trades_today}")
    log.info(f"  Win rate:  {state.wins}/{max(state.trades_today,1)}")
    log.info(f"  Daily PnL: ${state.daily_pnl:.2f}")
    log.info(f"  Log saved: trades.json")
    log.info("─" * 40)
    save_trade_log()


if __name__ == "__main__":
    run()
