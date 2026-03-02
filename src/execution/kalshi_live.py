"""Place real orders on Kalshi when approved_signal is received; close on position_closed."""
from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import Settings
from src.feeds.event_bus import EventBus
from src.models import Signal

logger = logging.getLogger(__name__)


def _load_private_key(pem_str: str):
    """Load RSA private key from PEM string (handles \\n in .env)."""
    if not pem_str or "BEGIN" not in pem_str:
        raise ValueError("Invalid Kalshi private key")
    s = pem_str.replace("\\n", "\n").strip()
    return serialization.load_pem_private_key(
        s.encode("utf-8"), password=None, backend=default_backend()
    )


def _sign_request(private_key, timestamp: str, method: str, path: str) -> str:
    """RSA-PSS SHA256 sign timestamp+method+path, return base64."""
    path_clean = path.split("?")[0]
    message = f"{timestamp}{method}{path_clean}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


async def fetch_kalshi_positions(settings: Settings) -> list[dict] | None:
    """Fetch live Kalshi open positions. Returns list of position dicts or None on failure."""
    if not settings.kalshi_api_key_id or not settings.kalshi_private_key:
        return None
    try:
        key = _load_private_key(settings.kalshi_private_key)
    except Exception:
        return None
    path = "/trade-api/v2/portfolio/positions"
    url = settings.kalshi_api_base.rstrip("/") + "/portfolio/positions"
    params = "?settlement_status=unsettled"
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    sig = _sign_request(key, ts, "GET", path)
    headers = {
        "KALSHI-ACCESS-KEY": settings.kalshi_api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url + params, headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
        positions = []
        for ep in data.get("event_positions", []):
            for mp in ep.get("market_positions", []):
                ticker = mp.get("ticker", "")
                yes_count = mp.get("position", 0)
                no_count = mp.get("total_traded", 0) - abs(yes_count) if mp.get("total_traded") else 0
                market_value = mp.get("market_exposure", 0) / 100
                resting = mp.get("resting_orders_count", 0)
                if yes_count > 0:
                    positions.append({
                        "ticker": ticker,
                        "side": "YES",
                        "count": yes_count,
                        "market_value": market_value,
                        "resting_orders": resting,
                    })
                elif yes_count < 0:
                    positions.append({
                        "ticker": ticker,
                        "side": "NO",
                        "count": abs(yes_count),
                        "market_value": market_value,
                        "resting_orders": resting,
                    })
        return positions
    except Exception:
        return None


async def fetch_kalshi_balance(settings: Settings) -> dict | None:
    """Fetch live Kalshi balance and portfolio value (dollars). Returns None on failure."""
    if not settings.kalshi_api_key_id or not settings.kalshi_private_key:
        return None
    try:
        key = _load_private_key(settings.kalshi_private_key)
    except Exception:
        return None
    path = "/trade-api/v2/portfolio/balance"
    url = settings.kalshi_api_base.rstrip("/") + "/portfolio/balance"
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    sig = _sign_request(key, ts, "GET", path)
    headers = {
        "KALSHI-ACCESS-KEY": settings.kalshi_api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return None
        data = r.json()
        return {
            "balance": data.get("balance", 0) / 100,
            "portfolio_value": data.get("portfolio_value", 0) / 100,
        }
    except Exception:
        return None


class KalshiLiveEngine:
    """Subscribes to approved_signal (place buys) and position_closed (place sells)."""

    def __init__(self, event_bus: EventBus, settings: Settings):
        self.bus = event_bus
        self.s = settings
        self._sig_queue = event_bus.subscribe("approved_signal", maxsize=50)
        self._close_queue = event_bus.subscribe("position_closed", maxsize=50)
        self._reversal_queue = event_bus.subscribe("reversal_order", maxsize=50)
        self._private_key = None
        if settings.kalshi_private_key:
            try:
                self._private_key = _load_private_key(settings.kalshi_private_key)
            except Exception as e:
                logger.error("Failed to load Kalshi private key: %s", e)

    async def run(self):
        if not self._private_key or not self.s.kalshi_api_key_id:
            logger.warning("Kalshi live execution disabled: missing API key or private key")
            return
        logger.info(
            "Kalshi LIVE execution ON — max %.0f%% of balance per order",
            self.s.max_position_pct * 100,
        )
        await asyncio.gather(self._run_opens(), self._run_closes(), self._run_reversals())

    async def _run_opens(self):
        while True:
            sig: Signal = await self._sig_queue.get()
            await self._place_order(sig)

    async def _run_closes(self):
        while True:
            payload = await self._close_queue.get()
            await self._place_close(payload)

    async def _run_reversals(self):
        while True:
            payload = await self._reversal_queue.get()
            await self._place_reversal_order(payload)

    async def _place_order(self, sig: Signal):
        opp = sig.opportunity
        balance_data = await fetch_kalshi_balance(self.s)
        balance = balance_data.get("balance", 0.0) if balance_data else 0.0
        max_bet = balance * self.s.max_position_pct
        cost_usd = min(sig.position_size_usd, max_bet)
        if self.s.kalshi_live_max_order_usd > 0:
            cost_usd = min(cost_usd, self.s.kalshi_live_max_order_usd)
        if cost_usd < 0.50:
            logger.warning("LIVE skip: order would be $%.2f (min $0.50)", cost_usd)
            return
        entry = opp.entry_price
        if entry <= 0 or entry >= 1:
            logger.warning("LIVE skip: invalid entry price %.4f", entry)
            return
        fill = min(0.99, entry + 0.01)
        count = max(1, int(cost_usd / fill))
        # Kalshi requires 1-cent (0.01) tick
        raw = fill
        price_dollars = f"{round(raw, 2):.4f}"
        ticker = opp.market.id
        side = "yes" if opp.side.value == "YES" else "no"

        path = "/trade-api/v2/portfolio/orders"
        url = self.s.kalshi_api_base.rstrip("/") + "/portfolio/orders"
        body = {
            "ticker": ticker,
            "side": side,
            "action": "buy",
            "count": count,
            "client_order_id": str(uuid.uuid4()),
        }
        if side == "yes":
            body["yes_price_dollars"] = price_dollars
        else:
            body["no_price_dollars"] = price_dollars

        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        sig_b64 = _sign_request(self._private_key, ts, "POST", path)
        headers = {
            "KALSHI-ACCESS-KEY": self.s.kalshi_api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 201:
                data = resp.json()
                order = data.get("order", {})
                logger.info(
                    "LIVE ORDER PLACED on Kalshi: %s %s %s x %d @ %s (order_id=%s)",
                    side.upper(),
                    ticker,
                    price_dollars,
                    count,
                    price_dollars,
                    order.get("order_id", "?"),
                )
            else:
                logger.error(
                    "Kalshi order failed: %s %s — %s",
                    resp.status_code,
                    resp.text[:200],
                    ticker,
                )
        except Exception as e:
            logger.exception("Kalshi live order error: %s", e)

    async def _place_close(self, payload: dict):
        """Place a sell order on Kalshi when paper engine closes (early exit)."""
        market_id = payload.get("market_id")
        side = payload.get("side")
        count = payload.get("count", 0)
        sell_price = payload.get("sell_price", 0.5)
        if not market_id or count < 1:
            return
        side_str = getattr(side, "value", str(side)).lower()
        if side_str not in ("yes", "no"):
            return
        # Kalshi requires price in 1-cent (0.01) tick; invalid_price if not
        raw = max(0.01, min(0.99, float(sell_price) - 0.02))
        price = round(raw, 2)
        price_dollars = f"{price:.4f}"
        path = "/trade-api/v2/portfolio/orders"
        url = self.s.kalshi_api_base.rstrip("/") + "/portfolio/orders"
        body = {
            "ticker": market_id,
            "side": side_str,
            "action": "sell",
            "count": count,
            "client_order_id": str(uuid.uuid4()),
        }
        if side_str == "yes":
            body["yes_price_dollars"] = price_dollars
        else:
            body["no_price_dollars"] = price_dollars
        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        sig_b64 = _sign_request(self._private_key, ts, "POST", path)
        headers = {
            "KALSHI-ACCESS-KEY": self.s.kalshi_api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 201:
                data = resp.json()
                order = data.get("order", {})
                logger.info(
                    "LIVE CLOSE (sell) on Kalshi: %s %s x %d @ %s (order_id=%s)",
                    side_str.upper(),
                    market_id,
                    count,
                    price_dollars,
                    order.get("order_id", "?"),
                )
            else:
                logger.error(
                    "Kalshi close order failed: %s %s — %s",
                    resp.status_code,
                    resp.text[:200],
                    market_id,
                )
        except Exception as e:
            logger.exception("Kalshi live close error: %s", e)

    async def _place_reversal_order(self, payload: dict):
        """Place a buy order on Kalshi when paper engine places a reversal (after cut-loss)."""
        market_id = payload.get("market_id")
        side = payload.get("side", "").lower()
        entry_price = float(payload.get("entry_price", 0))
        count = int(payload.get("count", 0))
        if not market_id or side not in ("yes", "no") or count < 1 or entry_price <= 0 or entry_price >= 1:
            logger.warning("LIVE reversal skip: invalid payload %s", payload)
            return
        # Kalshi requires 1-cent (0.01) tick
        raw = min(0.99, entry_price + 0.01)
        price = round(raw, 2)
        price_dollars = f"{price:.4f}"
        path = "/trade-api/v2/portfolio/orders"
        url = self.s.kalshi_api_base.rstrip("/") + "/portfolio/orders"
        body = {
            "ticker": market_id,
            "side": side,
            "action": "buy",
            "count": count,
            "client_order_id": str(uuid.uuid4()),
        }
        if side == "yes":
            body["yes_price_dollars"] = price_dollars
        else:
            body["no_price_dollars"] = price_dollars
        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        sig_b64 = _sign_request(self._private_key, ts, "POST", path)
        headers = {
            "KALSHI-ACCESS-KEY": self.s.kalshi_api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 201:
                data = resp.json()
                order = data.get("order", {})
                logger.info(
                    "LIVE REVERSAL on Kalshi (cut-loss only): %s %s x %d @ %s (order_id=%s)",
                    side.upper(),
                    market_id,
                    count,
                    price_dollars,
                    order.get("order_id", "?"),
                )
            else:
                logger.error(
                    "Kalshi reversal order failed: %s %s — %s",
                    resp.status_code,
                    resp.text[:200],
                    market_id,
                )
        except Exception as e:
            logger.exception("Kalshi live reversal error: %s", e)
