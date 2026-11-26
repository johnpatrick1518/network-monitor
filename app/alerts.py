"""Bounded Telegram/SMTP delivery with persistent cooldowns."""

import logging
import smtplib
import ssl
import threading
from datetime import datetime
from email.message import EmailMessage

import httpx
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import utcnow
from app.models import AlertCooldown, Event

logger = logging.getLogger(__name__)


def cooldown_allows(
    last_sent_at: datetime | None, now: datetime, cooldown_seconds: int
) -> bool:
    """Return whether this alert's successful-delivery cooldown has elapsed."""
    return last_sent_at is None or (now - last_sent_at).total_seconds() >= cooldown_seconds


class AlertService:
    """Serialize outbound notifications and retain rate limits across restarts."""

    def __init__(self, settings: Settings, sessions: sessionmaker[Session]) -> None:
        """Bind configuration and a session factory without opening a connection."""
        self.settings = settings
        self.sessions = sessions
        self._lock = threading.Lock()

    def _telegram(self, message: str) -> str:
        """Send a plain-text Telegram message without logging tokens or request URLs."""
        if self.settings.demo_mode:
            return "simulated"
        try:
            with httpx.Client(timeout=10, follow_redirects=False) as client:
                response = client.post(
                    f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage",
                    json={"chat_id": self.settings.telegram_chat_id, "text": message[:4000]},
                )
                if response.status_code == 200 and response.json().get("ok"):
                    return "sent"
                logger.warning("Telegram delivery rejected (HTTP %s).", response.status_code)
        except Exception as exc:
            logger.warning("Telegram unavailable (%s). Check token/chat ID/connectivity.", type(exc).__name__)
        return "failed"

    def _email(self, message: str) -> str:
        """Send email using authenticated TLS by default, with a network timeout."""
        if self.settings.demo_mode:
            return "simulated"
        settings = self.settings
        mail = EmailMessage()
        mail["Subject"] = "Network Monitor alert"
        mail["From"] = settings.smtp_from
        mail["To"] = settings.smtp_to
        mail.set_content(message)
        try:
            context = ssl.create_default_context()
            if settings.smtp_use_ssl:
                connection = smtplib.SMTP_SSL(
                    settings.smtp_host, settings.smtp_port, timeout=10, context=context
                )
            else:
                connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
            with connection as server:
                if settings.smtp_starttls:
                    server.starttls(context=context)
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(mail)
            return "sent"
        except Exception as exc:
            logger.warning("SMTP delivery failed (%s). Check SMTP configuration.", type(exc).__name__)
            return "failed"

    def notify(
        self, key: str, message: str, event_id: int | None = None
    ) -> dict[str, str]:
        """Attempt configured channels, enforcing cooldown and failure backoff."""
        with self._lock:
            now = utcnow()
            with self.sessions() as session:
                cooldown = session.get(AlertCooldown, key)
                if cooldown and not cooldown_allows(
                    cooldown.last_sent_at, now, self.settings.alert_cooldown_seconds
                ):
                    self._update_event(session, event_id, "cooldown")
                    return {"status": "cooldown"}
                if cooldown and cooldown.last_attempt_at and (
                    now - cooldown.last_attempt_at
                ).total_seconds() < 30:
                    self._update_event(session, event_id, "backoff")
                    return {"status": "backoff"}
                if cooldown is None:
                    cooldown = AlertCooldown(key=key)
                    session.add(cooldown)
                cooldown.last_attempt_at = now
                session.commit()

            channels: dict[str, str] = {}
            if self.settings.demo_mode:
                channels["demo"] = "simulated"
            elif self.settings.telegram_bot_token and self.settings.telegram_chat_id:
                channels["telegram"] = self._telegram(message)
            if not self.settings.demo_mode and self.settings.smtp_host and self.settings.smtp_from and self.settings.smtp_to:
                channels["smtp"] = self._email(message)
            delivered = any(result == "sent" for result in channels.values())
            status = "simulated" if self.settings.demo_mode else (
                "sent" if delivered else ("failed" if channels else "disabled")
            )
            with self.sessions() as session:
                cooldown = session.get(AlertCooldown, key)
                if cooldown and (delivered or self.settings.demo_mode):
                    cooldown.last_sent_at = utcnow()
                self._update_event(session, event_id, status)
            return {"status": status, **channels}

    @staticmethod
    def _update_event(session: Session, event_id: int | None, status: str) -> None:
        """Persist the delivery outcome if this notification belongs to an event."""
        if event_id is not None:
            event = session.get(Event, event_id)
            if event:
                event.notification_status = status
        session.commit()
