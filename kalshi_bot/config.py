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


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    kalshi_base_url: str = DEFAULT_BASE_URL
    kalshi_key_id: str | None = None
    kalshi_private_key: str | None = field(default=None, repr=False)
    telegram_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
    telegram_allowed_user_ids: frozenset[str] = frozenset()
    scan_interval: int = 300
    min_saving: Decimal = Decimal("0.02")
    max_spread: Decimal = Decimal("0.10")
    budget: Decimal = Decimal("10")
    live_trading: bool = False
    db_path: str = "kalshi_bot.sqlite3"

    @classmethod
    def from_env(cls, env=os.environ) -> Config:
        pem = env.get("KALSHI_PRIVATE_KEY")
        key_path = env.get("KALSHI_PRIVATE_KEY_PATH")
        if not pem and key_path:
            pem = Path(key_path).expanduser().read_text()
        if pem:
            # Env-var UIs often flatten newlines; PEM needs them back.
            pem = pem.replace("\\n", "\n")
        chat_id = env.get("TELEGRAM_CHAT_ID") or None
        allowed = {u.strip() for u in (env.get("TELEGRAM_ALLOWED_USER_IDS") or "").split(",") if u.strip()}
        if not allowed and chat_id:
            # In a private chat the chat id is the user's id.
            allowed = {chat_id}
        return cls(
            kalshi_base_url=env.get("KALSHI_API_BASE") or DEFAULT_BASE_URL,
            kalshi_key_id=env.get("KALSHI_API_KEY_ID") or None,
            kalshi_private_key=pem or None,
            telegram_token=env.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=chat_id,
            telegram_allowed_user_ids=frozenset(allowed),
            scan_interval=int(env.get("SCAN_INTERVAL_SECONDS") or 300),
            min_saving=Decimal(env.get("MIN_SAVING") or "0.02"),
            max_spread=Decimal(env.get("MAX_SPREAD") or "0.10"),
            budget=Decimal(env.get("BUY_BUDGET_USD") or "10"),
            live_trading=_flag(env.get("LIVE_TRADING")),
            db_path=env.get("DB_PATH") or "kalshi_bot.sqlite3",
        )

    def kalshi_client(self) -> KalshiClient:
        key = load_private_key(self.kalshi_private_key) if self.kalshi_private_key else None
        return KalshiClient(self.kalshi_base_url, key_id=self.kalshi_key_id, private_key=key)
