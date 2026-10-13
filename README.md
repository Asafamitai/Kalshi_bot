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
python -m kalshi_bot live            # automatic trading with real money
python -m kalshi_bot subaccount      # its Kalshi subaccount: show, create, fund 100, withdraw 100
python -m kalshi_bot telegram-chats  # who wrote to the bot, to find KALSHI_LADDER_BOT_CHAT_ID
python -m kalshi_bot telegram-chats --paper  # the same for KALSHI_LADDER_AUTO_BOT
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
- at least `MIN_HOURS_LEFT` (24) hours before the market stops trading or its
  outcome is due, whichever Kalshi gives first (`close_time`,
  `expected_expiration_time`). A live game moves faster than a scan every few
  minutes: a contradiction there is usually a rung that hasn't caught up, and
  the cheap side is cheap for a reason. The first night of paper trading lost
  every settled trade in fourth-quarter markets;
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

### Automatic trading with real money

The strict paper book's strategy, traded for real. It lives in its own
Telegram bot, `KALSHI_LADDER_AUTO_BOT` (the same bot as paper trading, below;
the alert bot keeps only alerts, their buy buttons, `/trading`, `/status` and
`/report`). **Off by default**: `/auto on` in KALSHI_LADDER_AUTO_BOT switches
it on (it needs a Kalshi key), `/auto off` switches it off. It is separate
from `/trading`, which only governs the alert bot's buy buttons. Without that
bot configured it can't be switched on.

While on, every pass buys what the strict book would buy (its filters, its
second read of both rungs, at most one position per event), with real orders:

- one immediate-or-cancel limit order at the ask, sized to `LIVE_MAX_TRADE`
  dollars including the fee;
- only with money `LIVE_BUDGET` still has. Buys spend the budget and what
  comes back refills it, but it is never topped up, so `LIVE_BUDGET` is the
  most automatic trading can lose. The Kalshi cash balance must be readable
  and cover the order too;
- sold with an immediate-or-cancel order at the bid once the bid holds
  `PAPER_TAKE_PROFIT` of the maximum profit and can take the whole position,
  otherwise held to settlement. Before a sale the position on Kalshi is read,
  and no more than is held is sold, since selling what isn't held would open
  the other side. A position no longer held there (closed by hand) is closed
  with nothing back;
- an order whose outcome is unknown (no answer, or a 5xx) switches automatic
  trading off and says so: nothing more is sent until someone has checked the
  account and sent `/auto on` again. An order Kalshi refuses is reported and
  the pass goes on.

#### Its own subaccount: KALSHI_LADDER_AUTO_BOT

Everything automatic trading does on Kalshi (orders, the cash check, position
reads) can happen in a subaccount of its own, with its own cash and
positions. That keeps its money and positions apart from anything bought by
hand (the buy buttons always use the primary account), and its cash there is
a second ceiling beside `LIVE_BUDGET`. Kalshi offers subaccounts only through
its API (and only from its Advanced API tier), so the bot manages it:

- `/subaccount create` opens one. Kalshi numbers subaccounts (1-63) and keeps
  no names, so the bot remembers the number as `KALSHI_LADDER_AUTO_BOT` in its
  database, and automatic trading uses it from then on. It opens one at most.
- `/subaccount fund 100` moves $100 from the primary account into it, and
  `/subaccount withdraw 100` moves it back. Each transfer carries an id
  Kalshi applies once, so a retry can't move the money twice; when the
  outcome is unknown the reply says to check before trying again.
- `/subaccount` shows which account it trades in, and both balances.

The same from the command line: `python -m kalshi_bot subaccount create`,
`... fund 100`, `... withdraw 100`, or just `subaccount`.
`KALSHI_LADDER_AUTO_BOT_SUBACCOUNT` overrides the number (0 is the primary
account). Without either, it trades in the primary account. `/auto on`
replies with the cash the bot sees where it trades, which also shows Kalshi
accepts that subaccount.

With it off, no order of any kind is sent, but what is held is still marked
at its bid and recorded when its market settles. After every pass that
bought, closed or hit a problem, KALSHI_LADDER_AUTO_BOT gets one message in
Hebrew; `/live` there (or `python -m kalshi_bot live`) shows the positions,
P&L and what is left of the budget, and `/status` there says whether it is
on, in which account, and what is left.

To start: connect KALSHI_LADDER_AUTO_BOT (below), set `KALSHI_API_KEY_ID` and
the private key, choose `LIVE_MAX_TRADE` and `LIVE_BUDGET`, and restart. Then,
in KALSHI_LADDER_AUTO_BOT, for a subaccount of its own, `/subaccount create`
and `/subaccount fund 100`; and `/auto on`.

### Paper trading

On by default (`PAPER_TRADING=off` to stop it), with no keys and no money.
Every pass, the books named in `PAPER_BOOKS` (default: `strict` and `pair`), each
with its own `PAPER_BANKROLL`, trade what the scan found. A book left out opens
nothing new but still sells and settles what it holds, and the report keeps
its history.

- **single** buys the safer contract of every contradiction with a bid
  behind its ask, at least `MIN_NET_SAVING` after the fee, and its own
  filters, looser than the alerts' so trades accumulate faster: price
  `PAPER_MIN_PRICE` to `PAPER_MAX_PRICE` (0.10 to 0.95), at least
  `PAPER_MIN_DEPTH` (10) contracts at the ask, `PAPER_MIN_VOLUME_24H` (0)
  traded in a day, and the alerts' `MIN_HOURS_LEFT`. Below 0.10, $15 buys hundreds of contracts and one trade
  would swamp the rest; volume isn't required because a sale needs a real bid
  anyway. Each trade records whether it also passed the alert filters, and the
  report gives those trades' P&L apart. It spends up to `PAPER_MAX_TRADE`
  dollars including the fee, sells once its bid holds `PAPER_TAKE_PROFIT`
  (80%) of the maximum profit and can take the whole position, and otherwise
  holds to settlement. It wins or loses with the outcome.
- **strict** is the single book with the alert filters instead of its own
  (price 0.30 to 0.90, depth and 24h volume of at least 50) and a book no
  wider than `PAPER_STRICT_MAX_SPREAD` (0.05) on the contract it buys. A wide
  book costs its spread the moment it is bought, and its own mid says the
  cheap ask is no bargain. It runs beside single so the two can be compared;
  it sells and settles like single.
- **pair** trades only when the safer ask is below the dominated rung's bid,
  and the pair is profitable after fees. It buys the safer contract and the
  other side of the dominated one, which together pay at least 1 in every
  outcome, so the profit is known at entry. Held to settlement. No price band
  (the hedge leg is cheap by design) and no `MIN_HOURS_LEFT`: it pays whatever
  happens.

Each holds at most one trade per event at a time (the two sides of one
inversion are the same bet), only buy with the cash their bankroll has left,
and only after a second read of both rungs. Fills are assumed at that read's
ask, up to the depth shown there, plus the taker fee. Sales are at the bid,
minus the fee. Settlement pays 1 or 0. Real orders would fill no better.
Open legs are marked at their bid every pass, and legs whose market stopped
trading are settled once Kalshi reports a result.

Every leg also records the mid of what it bought at entry, and again 15
minutes, an hour and four hours later (by the first pass inside twice that
time, or never, so a late pass can't pass off a later price as an earlier
one). That answers the question settlement takes weeks to: if the cheap
contract really was cheap, its price rises toward the dear one's after it is
bought; if it keeps falling, the scan bought a stale price.

`paper` (or `/paper` in Telegram) shows each book: trades, cash, realized
P&L, open positions at the bid (or the locked minimum for pairs), how the
single book did on the trades that passed the alert filters and on those where
a pair was also available, the average move of the mid after buying for
single and strict, and the latest trades.

#### KALSHI_LADDER_AUTO_BOT, its own Telegram bot

Paper trading shares a bot with automatic trading, apart from the alerts: the
strict paper book is the automatic strategy's twin. After every pass that
bought, sold or settled anything on paper, that bot sends
one message, in Hebrew: each trade bought (contracts, price, cost, saving,
and whether it also passed the alert filters, or the locked minimum for a
pair), each trade closed with its P&L, and each book's cash. A long pass is
split under Telegram's message limit. `/paper` there gives the report, beside
the automatic trading commands (`/auto`, `/live`, `/status`, `/subaccount`);
any other message gets a short help. Only `KALSHI_LADDER_AUTO_BOT_ALLOWED_USER_IDS`
(default: its chat id) are answered.

To connect one: create a bot with @BotFather, set
`KALSHI_LADDER_AUTO_BOT_TOKEN`, write to the new bot once, run
`python -m kalshi_bot telegram-chats --paper` to find your chat id, set
`KALSHI_LADDER_AUTO_BOT_CHAT_ID`, and restart. Without it, paper trading runs
silently, `/paper` works on the alert bot, and automatic trading can't be
switched on.

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
| `KALSHI_LADDER_AUTO_BOT_TOKEN`, `KALSHI_LADDER_AUTO_BOT_CHAT_ID` | – | The bot for automatic trading and paper trading; automatic trading needs it. |
| `KALSHI_LADDER_AUTO_BOT_ALLOWED_USER_IDS` | its chat id | Who that bot answers. |
| `SCAN_INTERVAL_SECONDS` | 300 | |
| `MIN_SAVING` | 0.01 | Tracked: saving against the dominated mid. |
| `MAX_SPREAD` | 0.10 | Widest dominated-rung book whose mid still counts. |
| `MIN_NET_SAVING` | 0.01 | Alerted: saving after the fee. |
| `MIN_PRICE`, `MAX_PRICE` | 0.30, 0.90 | Buy price band. |
| `MIN_DEPTH`, `MIN_VOLUME_24H` | 50, 50 | Contracts. |
| `MIN_HOURS_LEFT` | 24 | Hours before the market stops trading or resolves; also the single and strict books'. 0 turns it off. |
| `COOLDOWN_HOURS` | 12 | Per option. |
| `MAX_ALERTS_PER_PASS` | 10 | |
| `BUY_SIZES` | 10,50 | Buttons, plus "all". |
| `PAPER_TRADING` | on | `off` to stop paper trading. |
| `PAPER_BANKROLL` | 500 | Dollars, per book. |
| `PAPER_MAX_TRADE` | 15 | Dollars per trade, fees included. |
| `PAPER_TAKE_PROFIT` | 0.80 | Share of the maximum profit at which the single book sells. |
| `PAPER_MIN_PRICE`, `PAPER_MAX_PRICE` | 0.10, 0.95 | Single book's buy price band. |
| `PAPER_MIN_DEPTH`, `PAPER_MIN_VOLUME_24H` | 10, 0 | Single book, contracts. |
| `PAPER_STRICT_MAX_SPREAD` | 0.05 | Strict book: widest book it buys from. |
| `PAPER_BOOKS` | strict,pair | Books that open new trades: any of `single`, `strict`, `pair`. |
| `LIVE_MAX_TRADE` | 10 | Automatic trading: dollars per trade, fees included. |
| `LIVE_BUDGET` | 100 | Automatic trading: the most it may lose in all. |
| `KALSHI_LADDER_AUTO_BOT_SUBACCOUNT` | – | Automatic trading: the Kalshi subaccount it trades in (0 is the primary account, 1-63); unset, the one `/subaccount create` opened, else the primary account. |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```
