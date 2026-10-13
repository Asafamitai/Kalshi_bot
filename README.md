# Kalshi near-expiry favorites bot

**Strategy:** buy the side (YES or NO) that is priced **≥ 85¢** in a market that
**closes within 5 minutes**, then hold it to settlement.

The idea: with only minutes left, an 85–97¢ favorite usually wins. The catch is that
each win pays only 3–15¢ while each loss costs 85–97¢. At 90¢ (plus fees) you need
to win **more than about 90.6%** of the time just to break even. The backtest and
paper mode below exist to measure that before risking money.

## Setup

```bash
pip install -r requirements.txt
```

Market data is public, so the backtest and paper trading need no API keys.

## 1. Backtest on recently settled markets

```bash
python -m kalshi_bot backtest --hours 24                 # last 24h, all series
python -m kalshi_bot backtest --hours 48 --series KXBTCD # one series
python -m kalshi_bot backtest --min-price 0.90 --csv data/bt.csv
```

For each settled market it replays the 1-minute candles from the last 5 minutes,
takes the first minute where the rules fire, and scores that entry against the
actual result. The output shows win rate vs. breakeven and P&L per price bucket
(85–89¢, 90–94¢, 95–99¢) and per series.

Caveats: minute-close quotes are only approximate, book depth is unknown (full
fills are assumed), and markets that closed *early* (for example, a game that
ended) look like they were always going to close then.

## 2. Paper trade on live prices

```bash
python -m kalshi_bot run            # loop: scan every 15s, record simulated fills
python -m kalshi_bot run --once     # one scan
python -m kalshi_bot report         # win rate / P&L of the paper ledger
```

Paper fills are recorded at the displayed ask in `data/paper_trades.json` and
settled automatically from the real market result.

## 3. Live trading (real money)

```bash
cp .env.example .env   # fill in KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH
set -a; . ./.env; set +a
python -m kalshi_bot run --live --stake 5 --max-exposure 20 --max-daily-loss 10
```

Orders are limit **immediate-or-cancel** buys at the current ask, so the bot never
pays more than the price it saw and never leaves resting orders behind. Set
`KALSHI_ENV=demo` to use the demo exchange.

## Rules and parameters

| Flag | Default | Meaning |
|---|---|---|
| `--min-price` | 0.85 | Minimum ask for the side being bought |
| `--max-price` | 0.97 | Maximum ask; above this, fees eat the profit |
| `--max-seconds` | 300 | Only enter when close is within this many seconds |
| `--min-seconds` | 5 | Skip if close is too near for the order to land |
| `--max-spread` | 0.05 | Skip wide or empty books (e.g. 1¢ bid / 95¢ ask) |
| `--min-volume` | 0 | Minimum lifetime contracts traded in the market |
| `--stake` | 10 | Max dollars per trade, fees included |
| `--max-exposure` | 50 | Max dollars in all open positions (run only) |
| `--max-daily-loss` | 25 | Stop opening trades for the UTC day after this loss (run only) |

Other filters: one entry per market, binary markets only, and multivariate
parlays (`KXMVE…`) are excluded. Fees use Kalshi's taker formula,
`ceil(0.07 × contracts × P × (1 − P))`, rounded up to the cent.

## Layout

```
kalshi_bot/
  client.py    Kalshi API v2 client (RSA-PSS request signing, pagination, retries)
  strategy.py  pure entry rules, fee math, position sizing
  bot.py       scan → size → buy loop with risk limits
  ledger.py    trade ledger, settlement, reporting
  backtest.py  candlestick replay on settled markets
tests/         unit tests (python -m pytest)
```
