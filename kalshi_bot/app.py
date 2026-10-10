"""The running bot: a scan every few minutes, alerts to Telegram, buttons and commands."""

from __future__ import annotations

import logging
import threading
import time

from . import alerts, episodes, trader
from .config import Config
from .format import alert_text, buy_buttons, console_line
from .kalshi import KalshiClient
from .ladder import Violation, build_ladders, find_violations
from .store import Store
from .telegram import Telegram

log = logging.getLogger("kalshi_bot")

# Alert status once a tap is done; anything else goes back to "open".
_STATUS_AFTER = {"bought": "bought", "repriced": "superseded", "unknown": "unknown"}

HELP = (
    "<b>פקודות</b>\n"
    "/status · מצב הסריקה והמסחר\n"
    "/trading on · הפעלת קנייה אמיתית בלחיצה\n"
    "/trading off · כיבוי (ברירת המחדל): לחיצה רק מראה מה הייתה קונה\n"
    "/report · כמה זמן סתירות שורדות"
)


class App:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        scan_client: KalshiClient,
        trade_client: KalshiClient,
        telegram: Telegram | None,
        clock=time.time,
    ):
        self.cfg = cfg
        self.store = store
        self.scan_client = scan_client
        self.trade_client = trade_client
        self.telegram = telegram
        self.clock = clock

    def scan_once(self) -> list[Violation]:
        """One pass: read everything, track episodes, alert on the best new options."""
        started = self.clock()
        markets = list(self.scan_client.iter_open_markets())
        ladders = build_ladders(markets)
        found = find_violations(markets, self.cfg.min_saving, self.cfg.max_spread)
        now = self.clock()
        qualifying = {v.key for v in found if alerts.alertable(v, self.cfg)}
        opened, closed = episodes.update(self.store, found, qualifying, now)

        picked = alerts.pick(found, self.store, self.cfg, self.scan_client, now)
        sent = 0
        for v in picked:
            try:
                self.send_alert(v)
            except Exception:
                # Not marked sent, so it is retried next pass.
                log.exception("could not send alert for %s", v.safer.ticker)
                continue
            self.store.mark_sent(alerts.option_key(v), now)
            sent += 1

        self.store.log_scan(at=now, seconds=now - started, markets=len(markets), ladders=len(ladders),
                            violations=len(found), alertable=len(qualifying), sent=sent)
        log.info("scanned %d markets in %.0fs: %d violations (%d new, %d closed), %d alertable, %d sent",
                 len(markets), now - started, len(found), opened, closed, len(qualifying), sent)
        return picked

    def send_alert(self, v: Violation) -> None:
        if self.telegram is None:
            log.info("ALERT %s", console_line(v))
            return
        buttons = None
        if v.buyable:
            buttons = buy_buttons(self.store.add_alert(v), v, self.cfg.buy_sizes) or None
        self.telegram.send(self.cfg.telegram_chat_id, alert_text(v), buttons)

    def _allowed(self, user: dict | None) -> bool:
        return str((user or {}).get("id")) in self.cfg.telegram_allowed_user_ids

    def handle_message(self, msg: dict) -> None:
        if not self._allowed(msg.get("from")):
            return  # strangers get silence, not a hint that the bot exists
        words = (msg.get("text") or "").strip().split()
        if not words:
            return
        command = words[0].split("@")[0].lower()
        if command == "/trading" and len(words) > 1 and words[1].lower() in ("on", "off"):
            if words[1].lower() == "on" and not self.trade_client.can_trade:
                reply = "אין מפתח Kalshi בסביבה, אז המסחר נשאר כבוי."
            else:
                self.store.set_setting("trading", words[1].lower())
                reply = "✅ המסחר הופעל: לחיצה על כפתור קונה באמת." if words[1].lower() == "on" \
                    else "⏸ המסחר כובה: לחיצה רק תראה מה הייתה קונה."
        elif command == "/status":
            reply = self.status_text()
        elif command == "/report":
            reply = f"<pre>{episodes.report(self.store, now=self.clock())}</pre>"
        else:
            reply = HELP
        self.telegram.send(self.cfg.telegram_chat_id, reply)

    def status_text(self) -> str:
        last = self.store.last_scan()
        now = self.clock()
        lines = [f"מסחר: <b>{'פעיל' if self.store.trading_on else 'כבוי'}</b>"]
        if last:
            lines.append(
                f"סריקה אחרונה: לפני {(now - last['at']) / 60:.0f} דק׳, {last['markets']:,} שווקים, "
                f"{last['violations']} סתירות, {last['alertable']} עוברות את הסינון"
            )
        else:
            lines.append("עוד לא הייתה סריקה.")
        lines.append(f"התראות ב-24 השעות האחרונות: {self.store.sent_since(now - 86400)}")
        return "\n".join(lines)

    def handle_callback(self, cb: dict) -> None:
        if not self._allowed(cb.get("from")):
            self.telegram.answer(cb["id"], "Not authorised.")
            log.warning("ignored button tap from user %s", (cb.get("from") or {}).get("id"))
            return
        parts = (cb.get("data") or "").split(":")
        if len(parts) != 3 or parts[0] != "buy":
            self.telegram.answer(cb["id"])
            return
        alert = self.store.get_alert(int(parts[1]))
        if alert is None or not self.store.claim_alert(alert.id):
            self.telegram.answer(cb["id"], "כבר טופל.")
            return

        # Anything that fails before an order is sent leaves the alert tappable again.
        status = "open"
        try:
            self.telegram.answer(cb["id"], "בודק את המחיר מחדש…")
            outcome = trader.buy(self.trade_client, self.store, alert, parts[2], self.cfg)
            status = _STATUS_AFTER.get(outcome.kind, "open")
        except Exception as e:  # report every failure back to the chat
            log.exception("buy failed for alert %s", alert.id)
            self.telegram.send(self.cfg.telegram_chat_id, f"❌ הקנייה נכשלה: {e}")
            return
        finally:
            self.store.set_status(alert.id, status)

        message = cb.get("message") or {}
        if status != "open" and message:
            self.telegram.clear_buttons(message["chat"]["id"], message["message_id"])
        icon = {"bought": "✅", "dry_run": "🧪"}.get(outcome.kind, "⛔")
        self.telegram.send(self.cfg.telegram_chat_id, f"{icon} {outcome.message}")
        if outcome.kind == "repriced":
            self.send_alert(outcome.violation)

    def _scan_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self.scan_once()
            except Exception:
                log.exception("scan failed")
            time.sleep(max(0.0, self.cfg.scan_interval - (time.monotonic() - started)))

    def run(self) -> None:
        if self.telegram is None:
            log.info("Telegram not configured: alerts go to this log only")
            self._scan_forever()
            return
        threading.Thread(target=self._scan_forever, name="scanner", daemon=True).start()
        offset = None
        while True:
            try:
                for update in self.telegram.updates(offset):
                    offset = update["update_id"] + 1
                    if "callback_query" in update:
                        self.handle_callback(update["callback_query"])
                    elif "message" in update:
                        self.handle_message(update["message"])
            except Exception:
                log.exception("telegram polling failed")
                time.sleep(5)
