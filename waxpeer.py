"""Waxpeer's public CS2 listing feed."""
import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Callable

import aiohttp
import socketio

log = logging.getLogger("waxpeer")
SITE = "https://waxpeer.com"
RECONNECT_DELAY = 15
BUY_ORDERS_URL = "https://api.waxpeer.com/v1/buy-orders/snapshot"
SELLING_FEE = 0.06  # https://waxpeer.com/faq/fees-sell
BUY_ORDERS_REFRESH = 30


class BuyOrderBook:
    """Fresh public buy offers by market hash name. Prices are in USD."""

    MAX_AGE = 300

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._timestamp = 0

    def load(self, payload: object) -> None:
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ValueError("Waxpeer buy order snapshot did not succeed")
        timestamp, offers = payload.get("timestamp"), payload.get("offers")
        now = time.time()
        if type(timestamp) is not int or not now - self.MAX_AGE < timestamp <= now + 30:
            raise ValueError("Waxpeer buy order snapshot timestamp is invalid or stale")
        if not isinstance(offers, list):
            raise ValueError("Waxpeer buy order snapshot has no offers array")
        if timestamp < self._timestamp:
            return
        prices = {}
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            name, amount = offer.get("name"), offer.get("max")
            if not isinstance(name, str) or not name:
                continue
            if isinstance(amount, str) and re.fullmatch(r"[0-9]{1,15}", amount):
                amount = int(amount)
            if type(amount) is int and 0 < amount < 10**15:
                prices[name] = max(prices.get(name, 0), amount / 1000)
        self._prices, self._timestamp = prices, timestamp

    def bid(self, listing: "WaxpeerListing") -> float | None:
        # The snapshot doesn't separate Doppler phases.
        if (listing.phase or "doppler" in listing.name.lower()
                or time.time() - self._timestamp >= self.MAX_AGE):
            return None
        return self._prices.get(listing.name)


async def refresh_buy_orders(book: BuyOrderBook) -> None:
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(BUY_ORDERS_URL, params={"game": "csgo"}) as response:
                    response.raise_for_status()
                    book.load(await response.json())
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                log.warning("Waxpeer buy order refresh failed: %s", exc)
            await asyncio.sleep(BUY_ORDERS_REFRESH)


@dataclass(frozen=True)
class WaxpeerListing:
    item_id: str
    name: str
    price: float  # USD
    wear: float | None = None
    phase: str | None = None

    @property
    def url(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        return f"{SITE}/{slug or 'cs2'}/item?id={self.item_id}"


def parse_listing(data: object) -> WaxpeerListing:
    if not isinstance(data, dict) or data.get("game") != "csgo":
        raise ValueError("Invalid Waxpeer game event")
    item_id, name, price = data.get("item_id"), data.get("name"), data.get("price")
    if not isinstance(item_id, str) or not item_id.isascii() or not item_id.isdigit():
        raise ValueError("Invalid Waxpeer item ID")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Invalid Waxpeer item name")
    if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
        raise ValueError("Invalid Waxpeer price")
    try:
        usd = price / 1000
    except OverflowError as exc:
        raise ValueError("Invalid Waxpeer price") from exc
    if not math.isfinite(usd):
        raise ValueError("Invalid Waxpeer price")
    wear = data.get("float")
    if isinstance(wear, bool) or not isinstance(wear, (float, int)):
        wear = None
    elif not 0 <= wear <= 1:
        wear = None
    phase = data.get("phase")
    return WaxpeerListing(item_id, name.strip(), usd, wear,
                          phase if isinstance(phase, str) and phase else None)


async def listen(on_listing: Callable[[WaxpeerListing], None]) -> None:
    """Subscribe to new listings; reconnect after a dropped feed."""
    while True:
        async with aiohttp.ClientSession() as session:
            client = socketio.AsyncClient(reconnection=False, http_session=session)

            @client.event
            async def connect():
                await client.emit("sub", {"name": "csgo"})
                log.info("Waxpeer feed connected")

            @client.on("new")
            async def on_new(data):
                try:
                    listing = parse_listing(data)
                except ValueError:
                    log.warning("Invalid Waxpeer listing event")
                    return
                try:
                    on_listing(listing)
                except Exception:
                    log.exception("Could not process Waxpeer listing")

            wait_task = None
            try:
                await client.connect(SITE, transports=["websocket"])
                # Do not propagate cancellation into Socket.IO's internal read loop.
                wait_task = asyncio.create_task(client.wait())
                await asyncio.shield(wait_task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Waxpeer feed unavailable: %s", exc)
            finally:
                await client.disconnect()
                if wait_task:
                    await asyncio.gather(wait_task, return_exceptions=True)
        await asyncio.sleep(RECONNECT_DELAY)
