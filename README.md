# Kalshi-BTC-Arbitrage

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
