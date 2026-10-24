"""Minimal Telegram Bot API client over httpx. Commands are accepted ONLY from
the configured chat id. The bot token is never logged (it is part of the URL,
so httpx request logging is silenced and error texts are scrubbed)."""
import logging

import httpx

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class Telegram:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self.client = client or httpx.Client(timeout=40)
        self.offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _scrub(self, e: Exception) -> str:
        return str(e).replace(self.token, "<token>") if self.token else str(e)

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("telegram disabled; message: %s", text[:200])
            return False
        try:
            r = self.client.post(self._url("sendMessage"),
                                 json={"chat_id": self.chat_id, "text": text[:4000],
                                       "disable_web_page_preview": True})
            r.raise_for_status()
            return True
        except httpx.HTTPError as e:
            log.warning("telegram send failed: %s", self._scrub(e))
            return False

    def poll_once(self, handler, timeout: int = 25) -> int:
        """Fetch updates; run handler(command, args) for messages from our chat; reply."""
        if not self.enabled:
            return 0
        try:
            r = self.client.get(self._url("getUpdates"),
                                params={"offset": self.offset, "timeout": timeout})
            r.raise_for_status()
            updates = r.json().get("result", [])
        except (httpx.HTTPError, ValueError) as e:
            log.warning("telegram poll failed: %s", self._scrub(e))
            return 0
        handled = 0
        for u in updates:
            self.offset = max(self.offset, int(u["update_id"]) + 1)
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue  # ignore everyone else
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            cmd, *args = text.split()
            self.send(handler(cmd.split("@")[0].lower(), args))
            handled += 1
        return handled
