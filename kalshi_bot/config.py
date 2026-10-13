"""Settings from environment variables (optionally loaded from a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .kalshi import DEFAULT_BASE_URL, KalshiClient, load_private_key


def load_dotenv(path: str | Path = ".env") -> None:
    """Read KEY=VALUE lines into os.environ without overriding what is already set."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _users(env, name: str, chat_id: str | None) -> set[str]:
    """Who may use a bot: the listed user ids, or else the chat's (in a private chat they are the same)."""
    users = {u.strip() for u in (env.get(name) or "").split(",") if u.strip()}
    return users or ({chat_id} if chat_id else set())


def _subaccount(value: str | None) -> int | None:
    if not (value or "").strip():
        return None
    n = int(value)
    if not 0 <= n <= 63:
        raise ValueError(f"KALSHI_LADDER_AUTO_BOT_SUBACCOUNT must be 0 (the primary account) or 1-63, not {n}")
    return n


@dataclass(frozen=True)
class Config:
    kalshi_base_url: str = DEFAULT_BASE_URL
    kalshi_key_id: str | None = None
    kalshi_private_key: str | None = field(default=None, repr=False)
    telegram_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
    telegram_allowed_user_ids: frozenset[str] = frozenset()
    scan_interval: int = 300
    # Detection: every contradiction at least this big against a real book is tracked.
    min_saving: Decimal = Decimal("0.01")
    max_spread: Decimal = Decimal("0.10")
    # Alerts: a subset of what is tracked.
    min_net_saving: Decimal = Decimal("0.01")
    min_price: Decimal = Decimal("0.30")
    max_price: Decimal = Decimal("0.90")
    min_depth: Decimal = Decimal("50")
    min_volume_24h: Decimal = Decimal("50")
    # The dearer rung's mid is the price the saving is measured against; a
    # rung that barely trades has a mid that means nothing.
    min_ref_volume_24h: Decimal = Decimal("20")
    # Hours left before the market stops trading or resolves; live games move faster than a scan.
    min_hours_left: float = 24
    cooldown_hours: float = 12
    max_alerts_per_pass: int = 10
    buy_sizes: tuple[int, ...] = (10, 50)
    # Paper trading: each strategy's own bankroll, what one trade may cost, and
    # the share of the maximum profit at which the single-contract book sells.
    paper_trading: bool = True
    paper_bankroll: Decimal = Decimal("500")
    paper_max_trade: Decimal = Decimal("15")
    paper_take_profit: Decimal = Decimal("0.80")
    # The single-contract book's filters: looser than the alerts', to collect
    # trades faster. Below 0.10, $15 buys hundreds of contracts and one trade
    # swamps the rest; no volume floor, since a sale needs a real bid anyway.
    paper_min_price: Decimal = Decimal("0.10")
    paper_max_price: Decimal = Decimal("0.95")
    paper_min_depth: Decimal = Decimal("10")
    paper_min_volume_24h: Decimal = Decimal("0")
    # The strict single book: the alert filters, plus a tight book on the
    # contract bought. A wide one costs its spread at once, and its own mid
    # says the cheap ask is no bargain.
    paper_strict_max_spread: Decimal = Decimal("0.05")
    # The books that open new trades; the others only see out what they hold.
    paper_books: tuple[str, ...] = ("strict", "pair")
    # Automatic trading with real money on the strict strategy (switched on
    # with /auto on): what one trade may cost, fees included, and the most it
    # may lose in all. Losses come off the budget and are not refilled.
    live_max_trade: Decimal = Decimal("10")
    live_budget: Decimal = Decimal("100")
    # The Kalshi subaccount it trades in, if set: 0 is the primary account,
    # 1-63 a numbered subaccount. Unset, it is the one the bot created as
    # KALSHI_LADDER_AUTO_BOT, or else the primary account.
    live_subaccount: int | None = None
    # Optional: a Telegram bot of its own for paper trading, so its messages
    # stay apart from the alerts.
    paper_telegram_token: str | None = field(default=None, repr=False)
    paper_telegram_chat_id: str | None = None
    paper_telegram_allowed_user_ids: frozenset[str] = frozenset()
    db_path: str = "kalshi_bot.sqlite3"

    @classmethod
    def from_env(cls, env=os.environ) -> Config:
        key_id = env.get("KALSHI_API_KEY_ID")
        pem = env.get("KALSHI_PRIVATE_KEY")
        key_path = env.get("KALSHI_PRIVATE_KEY_PATH")
        if not pem and key_path:
            pem = Path(key_path).expanduser().read_text()
        if pem:
            # Env-var UIs often flatten newlines; PEM needs them back.
            pem = pem.replace("\\n", "\n")
        chat_id = env.get("KALSHI_LADDER_BOT_CHAT_ID") or None
        allowed = _users(env, "KALSHI_LADDER_BOT_ALLOWED_USER_IDS", chat_id)
        paper_chat_id = env.get("KALSHI_LADDER_AUTO_BOT_CHAT_ID") or None
        paper_allowed = _users(env, "KALSHI_LADDER_AUTO_BOT_ALLOWED_USER_IDS", paper_chat_id)
        d = cls()
        num = lambda name, default: Decimal(env.get(name) or default)  # noqa: E731
        return cls(
            kalshi_base_url=env.get("KALSHI_API_BASE") or DEFAULT_BASE_URL,
            kalshi_key_id=key_id or None,
            kalshi_private_key=pem or None,
            telegram_token=env.get("KALSHI_LADDER_BOT_TOKEN") or None,
            telegram_chat_id=chat_id,
            telegram_allowed_user_ids=frozenset(allowed),
            scan_interval=int(env.get("SCAN_INTERVAL_SECONDS") or d.scan_interval),
            min_saving=num("MIN_SAVING", d.min_saving),
            max_spread=num("MAX_SPREAD", d.max_spread),
            min_net_saving=num("MIN_NET_SAVING", d.min_net_saving),
            min_price=num("MIN_PRICE", d.min_price),
            max_price=num("MAX_PRICE", d.max_price),
            min_depth=num("MIN_DEPTH", d.min_depth),
            min_volume_24h=num("MIN_VOLUME_24H", d.min_volume_24h),
            min_ref_volume_24h=num("MIN_REF_VOLUME_24H", d.min_ref_volume_24h),
            min_hours_left=float(env.get("MIN_HOURS_LEFT") or d.min_hours_left),
            cooldown_hours=float(env.get("COOLDOWN_HOURS") or d.cooldown_hours),
            max_alerts_per_pass=int(env.get("MAX_ALERTS_PER_PASS") or d.max_alerts_per_pass),
            buy_sizes=tuple(int(x) for x in (env.get("BUY_SIZES") or "10,50").split(",") if x.strip()),
            paper_trading=(env.get("PAPER_TRADING") or "on").strip().lower() not in ("0", "off", "false", "no"),
            paper_bankroll=num("PAPER_BANKROLL", d.paper_bankroll),
            paper_max_trade=num("PAPER_MAX_TRADE", d.paper_max_trade),
            paper_take_profit=num("PAPER_TAKE_PROFIT", d.paper_take_profit),
            paper_min_price=num("PAPER_MIN_PRICE", d.paper_min_price),
            paper_max_price=num("PAPER_MAX_PRICE", d.paper_max_price),
            paper_min_depth=num("PAPER_MIN_DEPTH", d.paper_min_depth),
            paper_min_volume_24h=num("PAPER_MIN_VOLUME_24H", d.paper_min_volume_24h),
            paper_strict_max_spread=num("PAPER_STRICT_MAX_SPREAD", d.paper_strict_max_spread),
            paper_books=tuple(b.strip().lower() for b in (env.get("PAPER_BOOKS") or ",".join(d.paper_books)).split(",")
                              if b.strip()),
            live_max_trade=num("LIVE_MAX_TRADE", d.live_max_trade),
            live_budget=num("LIVE_BUDGET", d.live_budget),
            live_subaccount=_subaccount(env.get("KALSHI_LADDER_AUTO_BOT_SUBACCOUNT")),
            paper_telegram_token=env.get("KALSHI_LADDER_AUTO_BOT_TOKEN") or None,
            paper_telegram_chat_id=paper_chat_id,
            paper_telegram_allowed_user_ids=frozenset(paper_allowed),
            db_path=env.get("DB_PATH") or d.db_path,
        )

    @property
    def paper_telegram(self) -> bool:
        return bool(self.paper_telegram_token and self.paper_telegram_chat_id)

    def kalshi_client(self) -> KalshiClient:
        key = load_private_key(self.kalshi_private_key) if self.kalshi_private_key else None
        return KalshiClient(self.kalshi_base_url, key_id=self.kalshi_key_id, private_key=key)
