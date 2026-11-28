"""Waxpeer's public CS2 listing feed."""
import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Callable

import aiohttp
import socketio

log = logging.getLogger("waxpeer")
SITE = "https://waxpeer.com"
RECONNECT_DELAY = 15


@dataclass(frozen=True)
class WaxpeerListing:
    item_id: str
    name: str
    price: float  # USD
    wear: float | None = None

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
    return WaxpeerListing(item_id, name.strip(), usd, wear)


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
