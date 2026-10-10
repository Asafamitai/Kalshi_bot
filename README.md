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
  necessarily cheap in absolute terms. When the safer ask is even below the
  dominated rung's *bid*, the alert says so: buying one and selling the other
  would lock the difference in.

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

python -m kalshi_bot scan                    # one pass over every open market, printed
python -m kalshi_bot scan --min-saving 0.01 --max-spread 0.05
python -m kalshi_bot run                     # every 5 minutes, alerts to Telegram
```

`scan` needs no keys: Kalshi market data is public. Every finding from every
scan is logged to the `findings` table in `DB_PATH` (SQLite), so you can see
afterwards how often contradictions appear, how big they are and how long
they last.

### Buying

`run` sends each buyable violation with a button such as **Buy 11 NO @ 0.86**
(contracts = `floor(BUY_BUDGET_USD / ask)`). A tap:

1. re-fetches both rungs;
2. refuses if either market stopped trading, the dominated rung no longer has
   a real price, the safer rung's book is empty, or the saving fell below
   `MIN_SAVING`;
3. refuses if the ask rose above the price on the button, and sends a fresh
   alert at the new price instead;
4. otherwise places an immediate-or-cancel limit buy at the current ask, so
   nothing rests on the book.

`LIVE_TRADING` is **off by default**: taps then report what they would have
bought. Only users in `TELEGRAM_ALLOWED_USER_IDS` (default: `TELEGRAM_CHAT_ID`)
can press buttons. Point `KALSHI_API_BASE` at the demo exchange to try real
orders without real money.

Fees are not included in the saving shown.

## Configuration

See [`.env.example`](.env.example). Keys can also come from real environment
variables, which take precedence over `.env`.

| Variable | Default | |
|---|---|---|
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY` or `KALSHI_PRIVATE_KEY_PATH` | – | Only for buying. RSA or Ed25519 keys. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | – | Without them, `run` logs alerts to the console. |
| `SCAN_INTERVAL_SECONDS` | 300 | |
| `MIN_SAVING` | 0.02 | Dollars, against the dominated mid. |
| `MAX_SPREAD` | 0.10 | Widest dominated-rung book whose mid still counts. |
| `BUY_BUDGET_USD` | 10 | Per tap. |
| `LIVE_TRADING` | 0 | Set to 1 to place real orders. |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```
