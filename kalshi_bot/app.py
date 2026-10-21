"""The running bot: a scan every few minutes, alerts to Telegram, buttons and commands."""

from __future__ import annotations

import logging
import threading
import time
from html import escape

from . import alerts, episodes, live, paper, subaccounts, trader, zoom
from .config import Config
from .fees import fee_per_contract
from .format import alert_text, buy_buttons, console_line
from .kalshi import KalshiClient
from .ladder import SIDES, Violation, build_ladders, find_violations
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
    "/report · כמה זמן סתירות שורדות ואיזו מדרגה זזה כשנסגרו\n"
    "/paper · תיק המסחר על נייר: מדרגה בודדת מול זוג נעול\n"
    "/zoom TICKER [yes|no] · זום אין על שוק: הסולם, מדרגת הייחוס וההיסטוריה של הסדרה"
)

# KALSHI_LADDER_AUTO_BOT: automatic trading with real money, and its twin on paper.
AUTO_HELP = (
    "<b>בוט המסחר האוטומטי</b>\n"
    "אחרי כל סבב סריקה: מה נקנה, נמכר והוכרע, בכסף אמיתי ועל נייר.\n"
    "/auto on · מסחר אוטומטי בכסף אמיתי לפי האסטרטגיה המחמירה\n"
    "/auto off · כיבוי המסחר האוטומטי (ברירת המחדל)\n"
    "/live · מצב המסחר האוטומטי: פוזיציות, רווח והפסד ותקציב\n"
    "/status · האם המסחר האוטומטי פעיל, איפה, וכמה נשאר בתקציב\n"
    "/subaccount · תת-החשבון של המסחר האוטומטי (KALSHI_LADDER_AUTO_BOT) והיתרות\n"
    "/subaccount create · פתיחת תת-החשבון ב-Kalshi\n"
    "/subaccount fund 100 · העברת $100 מהחשבון הראשי לתת-החשבון\n"
    "/subaccount withdraw 100 · החזרת $100 מתת-החשבון לחשבון הראשי\n"
    "/paper · תיקי המסחר על נייר\n"
    "/zoom · זום אין על כל פוזיציה פתוחה: הסולם, מדרגת הייחוס וההיסטוריה של הסדרה\n"
    "/zoom TICKER [yes|no] · זום אין על שוק מסוים"
)

MOVED = "המסחר האוטומטי עבר לבוט KALSHI_LADDER_AUTO_BOT: /auto, /live, /status ו-/subaccount שם."


class App:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        scan_client: KalshiClient,
        trade_client: KalshiClient,
        telegram: Telegram | None,
        clock=time.time,
        paper_telegram: Telegram | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.scan_client = scan_client
        self.trade_client = trade_client
        self.telegram = telegram
        self.clock = clock
        # KALSHI_LADDER_AUTO_BOT, if configured: automatic trading (real money) and paper trading.
        # Without it automatic trading can't be switched on; /paper also works on the alert bot.
        self.paper_telegram = paper_telegram

    def scan_once(self) -> list[Violation]:
        """One pass: read everything, track episodes, alert on the best new options."""
        started = self.clock()
        markets = list(self.scan_client.iter_open_markets())
        by_ticker = {m["ticker"]: m for m in markets}
        ladders = build_ladders(markets)
        found = find_violations(markets, self.cfg.min_saving, self.cfg.max_spread)
        now = self.clock()
        qualifying = {v.key for v in found if alerts.alertable(v, self.cfg, now)}
        opened, closed = episodes.update(self.store, found, qualifying, now, by_ticker)

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

        papered = ""
        if self.cfg.paper_trading:
            try:
                p = paper.step(self.store, self.scan_client, self.cfg, found, by_ticker, now)
                papered = f"; paper: {len(p['opened'])} opened, {p['sold']} sold, {p['settled']} settled"
            except Exception:
                log.exception("paper trading failed")
            else:
                self.send_paper(p)

        lived = ""
        try:
            # Runs with automatic trading off too: what is held is still marked and settled.
            done = live.step(self.store, self.trade_client, self.cfg, found, by_ticker, now)
            lived = f"; live: {len(done['opened'])} opened, {len(done['closed'])} closed, " \
                    f"{len(done['problems'])} problems"
        except Exception as e:
            log.exception("automatic trading failed")
            done = {"opened": [], "closed": [], "problems": [f"המסחר האוטומטי נכשל בסבב הזה: {e}"]}
        self.send_live(done)
        self.send_zooms(done["opened"])

        self.store.log_scan(at=now, seconds=now - started, markets=len(markets), ladders=len(ladders),
                            violations=len(found), alertable=len(qualifying), sent=sent)
        log.info("scanned %d markets in %.0fs: %d violations (%d new, %d closed), %d alertable, %d sent%s",
                 len(markets), now - started, len(found), opened, closed, len(qualifying), sent, papered + lived)
        return picked

    def send_alert(self, v: Violation) -> None:
        if self.telegram is None:
            log.info("ALERT %s", console_line(v))
            return
        buttons = None
        if v.buyable:
            buttons = buy_buttons(self.store.add_alert(v), v, self.cfg.buy_sizes) or None
        self.telegram.send(self.cfg.telegram_chat_id, alert_text(v), buttons)

    def send_paper(self, done: dict) -> None:
        """What paper trading did this pass, to its own bot; a failed send never stops the scan."""
        if self.paper_telegram is None:
            return
        try:
            for text in paper.pass_messages(self.store, self.cfg, done):
                self.paper_telegram.send(self.cfg.paper_telegram_chat_id, text)
        except Exception:
            log.exception("could not send the paper trading update")

    def send_live(self, done: dict) -> None:
        """What automatic trading did this pass, to KALSHI_LADDER_AUTO_BOT; a failed send never stops the scan."""
        try:
            text = live.pass_message(self.store, self.cfg, done)
            if text is None:
                return
            if self.paper_telegram is None:
                log.info("LIVE %s", text)
            else:
                self.paper_telegram.send(self.cfg.paper_telegram_chat_id, text)
        except Exception:
            log.exception("could not send the automatic trading update")

    def send_zooms(self, opened: list[int]) -> None:
        """A zoom on every position automatic trading just opened, to KALSHI_LADDER_AUTO_BOT; never stops the scan."""
        if not opened:
            return
        by_id = {p["id"]: p for p in self.store.live_positions()}
        for i in opened:
            text = self._zoom_text(lambda p=by_id[i]: zoom.for_position(self.scan_client, self.cfg, p, now=self.clock()))
            try:
                if self.paper_telegram is None:
                    log.info("ZOOM %s", zoom.plain(text))
                else:
                    self.paper_telegram.send(self.cfg.paper_telegram_chat_id, text)
            except Exception:
                log.exception("could not send the zoom on live position %s", i)

    def zoom_command(self, args: list[str]) -> list[str]:
        """/zoom: every open automatic trading position; /zoom TICKER [yes|no]: that market."""
        held = [p for p in self.store.live_positions() if p["status"] == "open"]
        if not args:
            if not held:
                return ["אין פוזיציות פתוחות במסחר האוטומטי. /zoom TICKER [yes|no] לזום על שוק מסוים."]
            return [self._zoom_text(lambda p=p: zoom.for_position(self.scan_client, self.cfg, p, now=self.clock()))
                    for p in held]
        ticker = args[0].upper()
        mine = [p for p in held if p["ticker"] == ticker]
        side = args[1].lower() if len(args) > 1 and args[1].lower() in SIDES else mine[0]["side"] if mine else "yes"
        mine = [p for p in mine if p["side"] == side]
        if mine:
            return [self._zoom_text(lambda: zoom.for_position(self.scan_client, self.cfg, mine[0], now=self.clock()))]
        return [self._zoom_text(lambda: zoom.build(self.scan_client, self.cfg, ticker, side, now=self.clock()))]

    def _zoom_text(self, make) -> str:
        try:
            return zoom.render(make(), self.cfg)
        except LookupError as e:
            return f"🔍 {escape(str(e.args[0] if e.args else e))}."
        except Exception as e:
            log.exception("zoom failed")
            return f"🔍 לא הצלחתי לעשות זום: {escape(str(e))}"

    def _allowed(self, user: dict | None) -> bool:
        return str((user or {}).get("id")) in self.cfg.telegram_allowed_user_ids

    def handle_paper_message(self, msg: dict) -> None:
        if str((msg.get("from") or {}).get("id")) not in self.cfg.paper_telegram_allowed_user_ids:
            return  # strangers get silence
        words = (msg.get("text") or "").strip().split()
        if not words:
            return
        command = words[0].split("@")[0].lower()
        if command == "/paper":
            reply = f"<pre>{escape(paper.report(self.store, self.cfg))}</pre>"
        elif command == "/auto" and len(words) > 1 and words[1].lower() in ("on", "off"):
            reply = self.auto_command(words[1].lower())
        elif command == "/live":
            links = live.open_positions_html(self.store)
            reply = f"<pre>{escape(live.report(self.store, self.cfg))}</pre>" + (f"\n{links}" if links else "")
        elif command == "/status":
            reply = self.auto_status_text()
        elif command == "/subaccount":
            reply = self.subaccount_command(words[1:])
        elif command == "/zoom":
            for text in self.zoom_command(words[1:]):
                self.paper_telegram.send(self.cfg.paper_telegram_chat_id, text)
            return
        else:
            reply = AUTO_HELP
        self.paper_telegram.send(self.cfg.paper_telegram_chat_id, reply)

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
        elif command in ("/auto", "/live", "/subaccount"):
            reply = MOVED
        elif command == "/status":
            reply = self.status_text()
        elif command == "/report":
            reply = f"<pre>{escape(episodes.report(self.store, now=self.clock()))}</pre>"
        elif command == "/paper":
            reply = f"<pre>{escape(paper.report(self.store, self.cfg))}</pre>"
        elif command == "/zoom":
            for text in self.zoom_command(words[1:]):
                self.telegram.send(self.cfg.telegram_chat_id, text)
            return
        else:
            reply = HELP
        self.telegram.send(self.cfg.telegram_chat_id, reply)

    def auto_command(self, state: str) -> str:
        if state == "off":
            self.store.set_setting("auto", "off")
            return "⏸ המסחר האוטומטי כובה: לא יישלחו הזמנות."
        if not self.trade_client.can_trade:
            return "אין מפתח Kalshi בסביבה, אז המסחר האוטומטי נשאר כבוי."
        self.store.set_setting("auto", "on")
        return (f"✅ המסחר האוטומטי הופעל: האסטרטגיה המחמירה קונה בכסף אמיתי {self._where()}, עד "
                f"${self.cfg.live_max_trade:,.2f} לעסקה ועד ${self.cfg.live_budget:,.2f} בסך הכול.\n"
                + self._live_cash())

    def subaccount_command(self, args: list[str]) -> str:
        action = args[0].lower() if args else ""
        if action == "create":
            text = subaccounts.create(self.store, self.trade_client, self.cfg)
        elif action in ("fund", "withdraw") and len(args) == 2:
            text = subaccounts.transfer(self.store, self.trade_client, self.cfg, args[1], back=action == "withdraw")
        elif not action:
            text = subaccounts.show(self.store, self.trade_client, self.cfg)
        else:
            return AUTO_HELP
        return escape(text)

    def _where(self) -> str:
        return subaccounts.name(subaccounts.number(self.store, self.cfg))

    def _live_cash(self) -> str:
        """The cash automatic trading can see, which also shows Kalshi accepts its subaccount."""
        try:
            cash = self.trade_client.balance(subaccount=subaccounts.number(self.store, self.cfg))
        except Exception as e:
            return f"⚠️ לא הצלחתי לקרוא את היתרה {self._where()}: {escape(str(e))}. עד שזה יסתדר לא ייקנה כלום."
        if cash is None:
            return f"⚠️ לא הצלחתי לקרוא את היתרה {self._where()}. עד שזה יסתדר לא ייקנה כלום."
        return f"יתרה {self._where()}: ${cash:,.2f}"

    def status_text(self) -> str:
        last = self.store.last_scan()
        now = self.clock()
        lines = [f"מסחר בלחיצה: <b>{'פעיל' if self.store.trading_on else 'כבוי'}</b>"]
        if last:
            lines.append(
                f"סריקה אחרונה: לפני {(now - last['at']) / 60:.0f} דק׳, {last['markets']:,} שווקים, "
                f"{last['violations']} סתירות, {last['alertable']} עוברות את הסינון"
            )
        else:
            lines.append("עוד לא הייתה סריקה.")
        lines.append(f"התראות ב-24 השעות האחרונות: {self.store.sent_since(now - 86400)}")
        return "\n".join(lines)

    def auto_status_text(self) -> str:
        """KALSHI_LADDER_AUTO_BOT's /status: whether automatic trading is on, where, and what is left."""
        positions = self.store.live_positions()
        lines = [f"מסחר אוטומטי (מחמירה, כסף אמיתי): <b>{'פעיל' if self.store.auto_on else 'כבוי'}</b> {self._where()}",
                 f"נשאר בתקציב ${live.budget_left(positions, self.cfg.live_budget):,.2f} מתוך "
                 f"${self.cfg.live_budget:,.2f} · עד ${self.cfg.live_max_trade:,.2f} לעסקה · "
                 f"{sum(p['status'] == 'open' for p in positions)} פוזיציות פתוחות"]
        last = self.store.last_scan()
        if last:
            lines.append(f"סריקה אחרונה: לפני {(self.clock() - last['at']) / 60:.0f} דק׳")
        else:
            lines.append("עוד לא הייתה סריקה.")
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
        elif outcome.kind == "bought":
            v = outcome.violation
            self.telegram.send(self.cfg.telegram_chat_id, self._zoom_text(lambda: zoom.build(
                self.scan_client, self.cfg, v.safer.ticker, v.side, reference=v.dominated.ticker,
                paid=v.ask + fee_per_contract(v.ask), now=self.clock())))

    def _scan_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                self.scan_once()
            except Exception:
                log.exception("scan failed")
            time.sleep(max(0.0, self.cfg.scan_interval - (time.monotonic() - started)))

    def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
        elif "message" in update:
            self.handle_message(update["message"])

    def _handle_paper_update(self, update: dict) -> None:
        if "message" in update:
            self.handle_paper_message(update["message"])

    @staticmethod
    def _poll(telegram: Telegram, handle) -> None:
        offset = None
        while True:
            try:
                for update in telegram.updates(offset):
                    offset = update["update_id"] + 1
                    handle(update)
            except Exception:
                log.exception("telegram polling failed")
                time.sleep(5)

    def run(self) -> None:
        if self.paper_telegram is not None:
            threading.Thread(target=self._poll, args=(self.paper_telegram, self._handle_paper_update),
                             name="paper-telegram", daemon=True).start()
        if self.telegram is None:
            log.info("Telegram not configured: alerts go to this log only")
            self._scan_forever()
            return
        threading.Thread(target=self._scan_forever, name="scanner", daemon=True).start()
        self._poll(self.telegram, self._handle_update)
