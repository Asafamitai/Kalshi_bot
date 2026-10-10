# Kalshi ladder scanner

Reads every open Kalshi market every 5 minutes and looks for contracts that
are both **cheaper and safer** than another rung on the same ladder. Each find
goes to Telegram with a one-tap buy button; the price is re-checked before
anything is bought.

## The idea: a contradiction inside one ladder

The scanner makes no forecasts. It only finds two prices in the same event
that contradict each other, which can be proven from the order book alone.

An event such as "how many downloads will the app get" is split into nested
threshold strikes ("above 100", "above 105", ...). NO on "above 107" pays in
every state where NO on "above 105" pays, plus the band between them, so it is
strictly safer and must never be cheaper:

```
no_ask  must be non-decreasing in strike
yes_ask must be non-increasing in strike
```

("below K" ladders are mirrored.) Any inversion is a violation. Real example:
NO on "above 195" offered at 0.86 while NO on "above 175" was at 0.98.

- **Saving is measured against the dominated rung's mid**, not its ask. An ask
  in a wide market is a price nobody pays.
- **Empty books are not prices**: a 1.00 ask, or an ask with no bid behind it.
  The dominated rung is the reference price, so it must have a two-sided book
  no wider than `MAX_SPREAD` (default 10¢); live data is full of 0.01/0.99
  placeholder quotes whose 0.50 "mid" means nothing. The safer rung only needs
  an ask to buy; if that ask has no bid behind it, the alert carries a warning
  and no buy button.
- **The rule doesn't say which quote is wrong**, only that one of them is.
  Buying the safer contract is right *relative* to the dearer one; it isn't
  necessarily cheap in absolute terms. Each alert also prices the pair
  (below), whose worst case is known.

### What counts as a ladder

Markets with `strike_type` `greater`/`greater_or_equal` and only a
`floor_strike`, or `less`/`less_or_equal` and only a `cap_strike`, grouped by
event, strike type, close time, and rules text with the numbers blanked out.
Anything carrying both bounds is skipped: Kalshi tags some "exactly N" buckets
as `less` with `floor_strike == cap_strike`, and those are not nested. The
grouping is deliberately strict so two rungs are only compared when they
measure the same thing. `between` buckets and custom strikes are ignored, as
are multivariate combo markets.

For each rung and side, the scanner reports the strongest contradiction
against any less-safe rung. One inversion usually shows up twice, once per
side (e.g. NO on the higher strike and YES on the lower one): those are two
ways into the same contradiction.

## Usage

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in what you need; .env is git-ignored

python -m kalshi_bot scan            # one pass over every open market, printed
python -m kalshi_bot run             # every 5 minutes, alerts to Telegram
python -m kalshi_bot report          # how long contradictions survived, and which rung moved
python -m kalshi_bot paper           # the paper trading books
python -m kalshi_bot telegram-chats  # who wrote to the bot, to find KALSHI_LADDER_BOT_CHAT_ID
./run.sh                             # `run`, restarted whenever it exits (bot.log)
```

`scan` needs no keys: Kalshi market data is public.

### What gets tracked, and what gets a message

Every contradiction of at least `MIN_SAVING` against a real book is tracked
as an **episode**, from the first scan that sees it to the first that
doesn't (`report`, or `/report` in Telegram).

Each episode also records **which rung moved to close it**: the safer ask
rising, or the dominated rung's mid falling (measured from when that pair of
rungs started to contradict). The rule only says one of the two prices is
wrong. If alertable contradictions mostly close by the safer ask rising, the
safer contract was the cheap one and buying it had an edge; if they mostly
close by the dominated mid falling, that rung was the dear one and buying the
safer contract gained nothing. `report` gives the shares and the mean moves.
Episodes whose rung stopped trading count as expired, and those that closed
because a book emptied or widened, with neither price moving, as "book".

Only the best of them become **alerts**:

- saving **after the taker fee** (`0.07 * p * (1 - p)` per contract) of at
  least `MIN_NET_SAVING`;
- buy price between `MIN_PRICE` and `MAX_PRICE` (below is lottery tickets,
  above is pennies against dollars);
- at least `MIN_DEPTH` contracts at the ask and `MIN_VOLUME_24H` traded in a day;
- the same option (contract and side) at most once per `COOLDOWN_HOURS`, kept
  in the database so a restart doesn't repeat it;
- at most `MAX_ALERTS_PER_PASS` per pass, best net saving first;
- a **second read** of both rungs just before sending; anything gone was a flicker.

Each alert, in Hebrew, shows the pair too: the safer contract plus the other
side of the dearer one pay 1 everywhere and 2 between the strikes, so its
worst case is known up front ("locked" when that is positive after fees).

### Buying

Buttons under an alert buy `BUY_SIZES` contracts (default 10 and 50) or all
the depth at the ask, each labelled with its cost including fees. A tap:

1. re-reads both rungs, and refuses if either market stopped trading, the
   dominated rung no longer has a real price, the safer rung's book is empty,
   or the contradiction closed;
2. refuses if the ask rose above the price on the button, and sends a fresh
   alert at the new price instead;
3. refuses if the cash balance can't cover the cost;
4. otherwise sends one immediate-or-cancel limit order at the current ask
   through Kalshi's V2 order endpoint (`POST /portfolio/events/orders`; the
   legacy `/portfolio/orders` answers 410). Its client order id comes from
   the alert and the size, so a retry can never buy twice.

Trading is **off by default**: a tap then says what it would have bought.
`/trading on` and `/trading off` in Telegram switch it (on needs a Kalshi
key). Only users in `KALSHI_LADDER_BOT_ALLOWED_USER_IDS` (default: `KALSHI_LADDER_BOT_CHAT_ID`)
can press buttons or send commands; anyone else gets silence.

### Paper trading

On by default (`PAPER_TRADING=off` to stop it), with no keys and no money.
Every pass, two books, each with its own `PAPER_BANKROLL`, trade what the scan
found:

- **single** buys the safer contract of every contradiction that passes the
  alert filters and has a bid behind its ask, up to `PAPER_MAX_TRADE` dollars including the fee. It sells
  once its bid holds `PAPER_TAKE_PROFIT` (80%) of the maximum profit and can
  take the whole position, and otherwise holds to settlement. It wins or
  loses with the outcome.
- **pair** trades only when the safer ask is below the dominated rung's bid,
  and the pair is profitable after fees. It buys the safer contract and the
  other side of the dominated one, which together pay at least 1 in every
  outcome, so the profit is known at entry. Held to settlement. No price band:
  the hedge leg is cheap by design.

Both hold at most one trade per event at a time (the two sides of one
inversion are the same bet), only buy with the cash their bankroll has left,
and only after a second read of both rungs. Fills are assumed at that read's
ask, up to the depth shown there, plus the taker fee. Sales are at the bid,
minus the fee. Settlement pays 1 or 0. Real orders would fill no better.
Open legs are marked at their bid every pass, and legs whose market stopped
trading are settled once Kalshi reports a result.

`paper` (or `/paper` in Telegram) shows each book: trades, cash, realized
P&L, open positions at the bid (or the locked minimum for pairs), how the
single book did on the trades where a pair was also available, and the latest
trades.

### Running in a Claude Code cloud session

`./run.sh` restarts the bot whenever it exits. A background command in a
cloud session is stopped after two hours and on a container restart, so the
session restarts `./run.sh` when that happens. The bot refuses to start a
second copy on the same database, and `run.sh` stops a stale one first.

## Configuration

See [`.env.example`](.env.example). Real environment variables take
precedence over `.env`. Only these names are read, so a token or key that
belongs to another project in the same environment is never picked up.

| Variable | Default | |
|---|---|---|
| `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY` (or `_PATH`) | – | Only for buying. RSA or Ed25519. |
| `KALSHI_LADDER_BOT_TOKEN`, `KALSHI_LADDER_BOT_CHAT_ID` | – | Without them, alerts go to the log. |
| `SCAN_INTERVAL_SECONDS` | 300 | |
| `MIN_SAVING` | 0.01 | Tracked: saving against the dominated mid. |
| `MAX_SPREAD` | 0.10 | Widest dominated-rung book whose mid still counts. |
| `MIN_NET_SAVING` | 0.01 | Alerted: saving after the fee. |
| `MIN_PRICE`, `MAX_PRICE` | 0.30, 0.90 | Buy price band. |
| `MIN_DEPTH`, `MIN_VOLUME_24H` | 50, 50 | Contracts. |
| `COOLDOWN_HOURS` | 12 | Per option. |
| `MAX_ALERTS_PER_PASS` | 10 | |
| `BUY_SIZES` | 10,50 | Buttons, plus "all". |
| `PAPER_TRADING` | on | `off` to stop paper trading. |
| `PAPER_BANKROLL` | 500 | Dollars, per book. |
| `PAPER_MAX_TRADE` | 15 | Dollars per trade, fees included. |
| `PAPER_TAKE_PROFIT` | 0.80 | Share of the maximum profit at which the single book sells. |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```
