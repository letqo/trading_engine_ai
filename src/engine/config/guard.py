"""The paper-only startup guard.

SPEC.md hard constraint #1: no live-money API keys, endpoints, or order
routes may exist anywhere in this codebase, and if a live URL or live
credential is ever detected in config, the program must refuse to start.

This module is the single enforcement point. It is called from every
entrypoint that could talk to a broker (CLI papertrade/kill commands, the
live worker main loop) before anything else happens.
"""

from __future__ import annotations

from engine.config.settings import Settings

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"

# Env vars that, if set to ANY value, indicate someone is trying to steer
# the broker client away from the hardcoded paper endpoint. Their mere
# presence is the violation -- the app never reads them to build a URL.
_FORBIDDEN_OVERRIDE_VARS = ("alpaca_base_url", "alpaca_live")

_LIVE_URL_MARKERS = (
    "api.alpaca.markets",  # live host, without the "paper-" prefix
)


class PaperOnlyViolation(RuntimeError):
    """Raised when config indicates a live-trading endpoint or credential."""


def enforce_paper_only(settings: Settings) -> None:
    """Raise PaperOnlyViolation if config resolves to anything but paper trading.

    Callers at process entrypoints should catch this, log it, and exit
    nonzero -- never trade, never retry, never fall back to a default.
    """
    violations: list[str] = []

    for field_name in _FORBIDDEN_OVERRIDE_VARS:
        value = getattr(settings, field_name, None)
        if value:
            violations.append(
                f"{field_name} is set (value hidden) -- the broker base URL is "
                f"hardcoded to {ALPACA_PAPER_BASE_URL} and must never be overridden"
            )

    for field_name in ("alpaca_api_key", "alpaca_api_secret", "database_url", "alert_webhook_url"):
        value = getattr(settings, field_name, None)
        if not value:
            continue
        lowered = str(value).lower()
        for marker in _LIVE_URL_MARKERS:
            if marker in lowered and "paper-" + marker not in lowered:
                violations.append(f"{field_name} contains a live-endpoint marker: {marker!r}")

    if violations:
        raise PaperOnlyViolation(
            "PAPER-ONLY GUARD TRIPPED -- refusing to start:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )
