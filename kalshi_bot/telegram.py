"""Just enough of the Telegram Bot API: send, long-poll button taps, answer them."""

from __future__ import annotations

import time

import requests


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, session: requests.Session | None = None):
        self._base = f"https://api.telegram.org/bot{token}"
        # No shared Session by default: the scanner thread and the button
        # handler both send, and calls are rare enough to skip pooling.
        self.session = session or requests

    def _call(self, method: str, http_timeout: float = 20, **params) -> object:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(4):
            resp = self.session.post(f"{self._base}/{method}", json=params, timeout=http_timeout)
            data = resp.json()
            if data.get("ok"):
                return data["result"]
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if resp.status_code == 429 and retry_after and attempt < 3:
                time.sleep(retry_after)
                continue
            raise TelegramError(f"{method}: {data.get('description')}")
        raise AssertionError("unreachable")

    def send(self, chat_id: str, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> dict:
        """`buttons` is rows of (label, callback_data)."""
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {
                "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in buttons]
            }
        return self._call("sendMessage", **params)

    def updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return self._call(
            "getUpdates",
            http_timeout=timeout + 10,
            offset=offset,
            timeout=timeout,
            allowed_updates=["callback_query", "message"],
        )

    def answer(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])

    def clear_buttons(self, chat_id, message_id) -> None:
        self._call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
