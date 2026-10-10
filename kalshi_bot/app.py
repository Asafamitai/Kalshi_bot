"""The running bot: a scan every few minutes, alerts to Telegram, buy buttons."""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal

from . import trader
from .config import Config
from .format import alert_text, console_line, price
from .kalshi import KalshiClient
from .ladder import Violation, find_violations
from .store import Store
from .telegram import Telegram

log = logging.getLogger("kalshi_bot")

# Alert status once a tap is done; anything else goes back to "open".
_STATUS_AFTER = {"bought": "bought", "repriced": "superseded", "unknown": "unknown"}


class App:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        scan_client: KalshiClient,
        trade_client: KalshiClient,
        telegram: Telegram | None,
    ):
        self.cfg = cfg
        self.store = store
        self.scan_client = scan_client
        self.trade_client = trade_client
        self.telegram = telegram
        # Violation key -> saving it was last alerted at.
        self._alerted: dict[tuple, Decimal] = {}

    def scan_once(self) -> list[Violation]:
        """Scan every open market; alert on violations that are new or grew by min_saving."""
        markets = list(self.scan_client.iter_open_markets())
        found = find_violations(markets, self.cfg.min_saving, self.cfg.max_spread)
        self.store.log_findings(found)

        fresh, alerted = [], {}
        for v in found:
            last = self._alerted.get(v.key)
            if last is None or v.saving >= last + self.cfg.min_saving:
                fresh.append(v)
                alerted[v.key] = v.saving
            else:
                alerted[v.key] = last
        # Anything that disappeared is forgotten, so it alerts again if it comes back.
        self._alerted = alerted

        for v in fresh:
            try:
                self.send_alert(v)
            except Exception:
                log.exception("could not send alert for %s", v.safer.ticker)
                self._alerted.pop(v.key, None)  # try again next scan
        log.info("scanned %d markets: %d violations, %d new", len(markets), len(found), len(fresh))
        return fresh

    def send_alert(self, v: Violation) -> None:
        if self.telegram is None:
            log.info("ALERT %s", console_line(v))
            return
        buttons = None
        count = trader.contracts_for(self.cfg.budget, v.ask)
        if v.buyable and count >= 1:
            alert_id = self.store.add_alert(v)
            buttons = [(f"Buy {count} {v.side.upper()} @ {price(v.ask)}", f"buy:{alert_id}")]
        self.telegram.send(self.cfg.telegram_chat_id, alert_text(v), buttons)

    def handle_callback(self, cb: dict) -> None:
        if str(cb.get("from", {}).get("id")) not in self.cfg.telegram_allowed_user_ids:
            self.telegram.answer(cb["id"], "Not authorised.")
            log.warning("ignored button tap from user %s", cb.get("from", {}).get("id"))
            return
        data = cb.get("data") or ""
        if not data.startswith("buy:"):
            self.telegram.answer(cb["id"])
            return
        alert = self.store.get_alert(int(data[4:]))
        if alert is None or not self.store.claim_alert(alert.id):
            self.telegram.answer(cb["id"], "Already handled.")
            return

        # Anything that fails before an order is sent leaves the alert tappable again.
        status = "open"
        try:
            self.telegram.answer(cb["id"], "Checking the price again…")
            outcome = trader.buy(self.trade_client, alert, self.cfg)
            status = _STATUS_AFTER.get(outcome.kind, "open")
        except Exception as e:  # report every failure back to the chat
            log.exception("buy failed for alert %s", alert.id)
            self.telegram.send(self.cfg.telegram_chat_id, f"❌ Buy failed: {e}")
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
            except Exception:
                log.exception("telegram polling failed")
                time.sleep(5)
