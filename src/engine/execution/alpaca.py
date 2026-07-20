"""Alpaca paper-trading client. SPEC.md hard constraint #1: the base URL is
hardcoded here to the paper endpoint and is NOT a constructor parameter --
there is no argument, env var, or config field that can point this class at
a live endpoint. enforce_paper_only() is also called at construction time
as defense in depth, even though every entrypoint that could reach this
class must already have called it first.
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from engine.config.guard import ALPACA_PAPER_BASE_URL, enforce_paper_only
from engine.config.settings import Settings
from engine.execution.broker import BrokerOrder
from engine.risk.models import OrderRequest, Position, Side


class AlpacaAuthError(RuntimeError):
    pass


class AlpacaPaperClient:
    """Implements engine.execution.broker.Broker against Alpaca's paper API."""

    def __init__(self, settings: Settings):
        enforce_paper_only(settings)  # defense in depth -- see module docstring
        if not settings.alpaca_api_key or not settings.alpaca_api_secret:
            raise AlpacaAuthError(
                "ALPACA_API_KEY / ALPACA_API_SECRET are not set -- refusing to construct "
                "a broker client rather than silently doing nothing."
            )
        self._base_url = ALPACA_PAPER_BASE_URL  # not settable -- see module docstring
        self._session = requests.Session()
        self._session.headers.update(
            {
                "APCA-API-KEY-ID": settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
            }
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        reraise=True,
    )
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = self._session.request(method, f"{self._base_url}{path}", timeout=15, **kwargs)
        if response.status_code == 401 or response.status_code == 403:
            raise AlpacaAuthError(f"Alpaca auth failed: {response.status_code} {response.text}")
        response.raise_for_status()
        return response

    def get_account_equity(self) -> float:
        data = self._request("GET", "/v2/account").json()
        return float(data["equity"])

    def get_positions(self) -> dict[str, Position]:
        data = self._request("GET", "/v2/positions").json()
        positions: dict[str, Position] = {}
        for raw in data:
            qty = float(raw["qty"])
            positions[raw["symbol"]] = Position(
                symbol=raw["symbol"],
                quantity=qty,
                avg_entry_price=float(raw["avg_entry_price"]),
            )
        return positions

    def get_open_orders(self) -> list[BrokerOrder]:
        data = self._request("GET", "/v2/orders", params={"status": "open"}).json()
        return [self._to_broker_order(raw) for raw in data]

    def submit_order(self, order: OrderRequest) -> BrokerOrder:
        payload = {
            "symbol": order.symbol,
            "qty": str(order.quantity),
            "side": order.side.value,
            "type": "market",
            "time_in_force": "day",
        }
        raw = self._request("POST", "/v2/orders", json=payload).json()
        return self._to_broker_order(raw)

    def cancel_all_orders(self) -> None:
        self._request("DELETE", "/v2/orders")

    def close_all_positions(self) -> None:
        self._request("DELETE", "/v2/positions", params={"cancel_orders": "true"})

    @staticmethod
    def _to_broker_order(raw: dict) -> BrokerOrder:
        filled_price = raw.get("filled_avg_price")
        submitted_at = raw.get("submitted_at")
        return BrokerOrder(
            broker_order_id=raw["id"],
            symbol=raw["symbol"],
            side=Side(raw["side"]),
            quantity=float(raw["qty"]),
            status=raw["status"],
            filled_avg_price=float(filled_price) if filled_price else None,
            submitted_at=datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
            if submitted_at
            else datetime.now(timezone.utc),
        )
