from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

from src.feeds.event_bus import EventBus
from src.models import PriceTick

logger = logging.getLogger(__name__)

# Provider configs: (name, url, subscribe_msg_factory, price_extractor)
PROVIDERS = [
    {
        "name": "Coinbase",
        "url": "wss://ws-feed.exchange.coinbase.com",
        "subscribe": lambda: json.dumps(
            {
                "type": "subscribe",
                "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}],
            }
        ),
        "extract": lambda d: (
            float(d["price"]),
            datetime.fromisoformat(d["time"].replace("Z", "+00:00")),
        )
        if d.get("type") == "ticker" and "price" in d
        else None,
    },
    {
        "name": "Bybit",
        "url": "wss://stream.bybit.com/v5/public/spot",
        "subscribe": lambda: json.dumps(
            {"op": "subscribe", "args": ["tickers.BTCUSDT"]}
        ),
        "extract": lambda d: (
            float(d["data"]["lastPrice"]),
            datetime.fromtimestamp(int(d["ts"]) / 1000, tz=timezone.utc),
        )
        if d.get("topic") == "tickers.BTCUSDT" and "data" in d
        else None,
    },
    {
        "name": "OKX",
        "url": "wss://ws.okx.com:8443/ws/v5/public",
        "subscribe": lambda: json.dumps(
            {"op": "subscribe", "args": [{"channel": "tickers", "instId": "BTC-USDT"}]}
        ),
        "extract": lambda d: (
            float(d["data"][0]["last"]),
            datetime.fromtimestamp(int(d["data"][0]["ts"]) / 1000, tz=timezone.utc),
        )
        if d.get("arg", {}).get("channel") == "tickers" and d.get("data")
        else None,
    },
    {
        "name": "Binance",
        "url": "wss://stream.binance.com:9443/ws/btcusdt@trade",
        "subscribe": None,
        "extract": lambda d: (
            float(d["p"]),
            datetime.fromtimestamp(d["T"] / 1000, tz=timezone.utc),
        )
        if "p" in d
        else None,
    },
]


class PriceFeed:
    """Multi-provider BTC price feed with automatic failover.

    Tries Coinbase -> Bybit -> OKX -> Binance, using whichever connects first.
    """

    def __init__(self, event_bus: EventBus):
        self.bus = event_bus
        self.last_price: float | None = None
        self.last_update: datetime | None = None
        self.connected = False
        self.provider_name = ""
        self._msg_count = 0

    async def run(self):
        while True:
            for provider in PROVIDERS:
                name = provider["name"]
                url = provider["url"]
                logger.info("Trying %s WebSocket at %s...", name, url)
                try:
                    await self._connect(provider)
                except Exception as e:
                    logger.warning("%s failed: %s — trying next provider", name, e)
                    self.connected = False
                    continue
            logger.error("All providers exhausted, restarting from top in 5s")
            await asyncio.sleep(5)

    async def _connect(self, provider: dict):
        name = provider["name"]
        url = provider["url"]
        sub_msg = provider["subscribe"]
        extract = provider["extract"]

        async with websockets.connect(url, ping_interval=20) as ws:
            self.connected = True
            self.provider_name = name
            logger.info("Connected to %s", name)

            if sub_msg:
                await ws.send(sub_msg())

            async for raw in ws:
                data = json.loads(raw)
                result = extract(data)
                if result is None:
                    continue

                price, ts = result
                self.last_price = price
                self.last_update = ts
                self._msg_count += 1

                if self._msg_count % 3 == 0:
                    tick = PriceTick(price=price, timestamp=ts, source=name.lower())
                    await self.bus.publish("btc_price", tick)
