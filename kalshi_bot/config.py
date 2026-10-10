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
    cooldown_hours: float = 12
    max_alerts_per_pass: int = 10
    buy_sizes: tuple[int, ...] = (10, 50)
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
        allowed = {u.strip() for u in (env.get("KALSHI_LADDER_BOT_ALLOWED_USER_IDS") or "").split(",") if u.strip()}
        if not allowed and chat_id:
            # In a private chat the chat id is the user's id.
            allowed = {chat_id}
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
            cooldown_hours=float(env.get("COOLDOWN_HOURS") or d.cooldown_hours),
            max_alerts_per_pass=int(env.get("MAX_ALERTS_PER_PASS") or d.max_alerts_per_pass),
            buy_sizes=tuple(int(x) for x in (env.get("BUY_SIZES") or "10,50").split(",") if x.strip()),
            db_path=env.get("DB_PATH") or d.db_path,
        )

    def kalshi_client(self) -> KalshiClient:
        key = load_private_key(self.kalshi_private_key) if self.kalshi_private_key else None
        return KalshiClient(self.kalshi_base_url, key_id=self.kalshi_key_id, private_key=key)
