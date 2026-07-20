"""Optional webhook alerting (Discord/Telegram/Slack-compatible: anything
that accepts {"content": "..."} as JSON, which all three do for a basic
text message). SPEC.md Deployment/Observability: alert on trade executed,
risk halt triggered, kill switch engaged, service restart.

Best-effort and silent-on-failure by design: a broken webhook must never be
allowed to interrupt trading logic or the kill switch itself. If
ALERT_WEBHOOK_URL isn't set, alerts are just logged instead (still visible
in Railway's captured stdout).
"""

from __future__ import annotations

import requests

from engine.config.settings import Settings
from engine.logging_setup import get_logger

logger = get_logger(__name__)

_TIMEOUT_SECONDS = 5


def send_alert(settings: Settings, message: str) -> None:
    logger.info("alert", extra={"extra_fields": {"message": message}})
    if not settings.alert_webhook_url:
        return
    try:
        requests.post(settings.alert_webhook_url, json={"content": message}, timeout=_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("failed to deliver alert webhook (non-fatal)")


def alert_trade_executed(settings: Settings, symbol: str, side: str, quantity: float, price: float) -> None:
    send_alert(settings, f"trade executed: {side} {quantity} {symbol} @ {price:.2f}")


def alert_risk_halt(settings: Settings, reason: str) -> None:
    send_alert(settings, f"RISK HALT triggered: {reason}")


def alert_kill_switch(settings: Settings) -> None:
    send_alert(settings, "KILL SWITCH engaged: all orders canceled, positions flattened")


def alert_service_restart(settings: Settings) -> None:
    send_alert(settings, "service (re)started")
