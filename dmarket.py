"""DMarket's documented market-items API client and response parser."""
import asyncio
import math
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import aiohttp
from nacl.bindings import crypto_sign

API = "https://api.dmarket.com"
MARKET_ITEMS_PATH = "/exchange/v1/market/items"
PAGE_SIZE = 50
PAGE_DELAY = 0.2


@dataclass(frozen=True)
class DMarketListing:
    item_id: str
    title: str
    price: float
    float_value: float | None


def parse_listing(item: dict) -> DMarketListing:
    item_id = item.get("itemId")
    title = item.get("title")
    amount = item.get("amount")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("DMarket listing has no itemId")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("DMarket listing has no title")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ValueError("DMarket listing has an invalid amount")
    price = float(amount) / 100
    if not math.isfinite(price) or price <= 0:
        raise ValueError("DMarket listing has an invalid amount")

    extra = item.get("extra")
    raw_float = extra.get("floatValue") if isinstance(extra, dict) else None
    try:
        float_value = float(raw_float) if raw_float is not None else None
    except (TypeError, ValueError):
        float_value = None
    if float_value is not None and (
            not math.isfinite(float_value) or not 0 <= float_value <= 1):
        float_value = None
    return DMarketListing(item_id.strip(), title.strip(), price, float_value)


class DMarketClient:
    def __init__(self, api_key: str, secret_key: str) -> None:
        if not api_key or not secret_key:
            raise ValueError("DMarket API credentials are required")
        if (len(api_key) != 64
                or any(char not in "0123456789abcdef" for char in api_key)):
            raise ValueError("DMarket API key must be 64 lowercase hexadecimal characters")
        try:
            secret = bytes.fromhex(secret_key)
        except ValueError as exc:
            raise ValueError("DMarket secret key must be hexadecimal") from exc
        if len(secret) != 64:
            raise ValueError("DMarket secret key has an invalid length")
        self._api_key = api_key
        self._secret = secret

    def headers(self, method: str, path: str, query: str, timestamp: str) -> dict[str, str]:
        message = f"{method.upper()}{path}{query}{timestamp}".encode("utf-8")
        signature = crypto_sign(message, self._secret)[:64].hex()
        return {
            "X-Api-Key": self._api_key,
            "X-Sign-Date": timestamp,
            "X-Request-Sign": f"dmar ed25519 {signature}",
        }

    async def _get_page(
        self,
        session: aiohttp.ClientSession,
        params: dict[str, str],
    ) -> dict:
        query = urlencode(params, quote_via=quote)
        timestamp = str(int(time.time()))
        headers = self.headers("GET", MARKET_ITEMS_PATH, f"?{query}", timestamp)
        async with session.get(
                f"{API}{MARKET_ITEMS_PATH}?{query}", headers=headers,
                allow_redirects=False) as response:
            if response.status >= 400:
                raise RuntimeError(f"DMarket API returned HTTP {response.status}")
            payload = await response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("objects"), list):
            raise ValueError("DMarket API returned an invalid market-items response")
        return payload

    async def listings(
        self,
        session: aiohttp.ClientSession,
        price_from_cents: int,
        price_to_cents: int,
    ):
        params = {
            "gameId": "a8db",
            "currency": "USD",
            "limit": str(PAGE_SIZE),
            "offset": "0",
            "orderBy": "title",
            "orderDir": "desc",
            "priceFrom": str(price_from_cents),
            "priceTo": str(price_to_cents),
        }
        seen_cursors: set[str] = set()
        using_cursor = False
        while True:
            payload = await self._get_page(session, params)
            items = payload["objects"]
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("DMarket API returned an invalid listing")
                yield parse_listing(item)

            cursor = payload.get("cursor")
            if isinstance(cursor, str) and cursor:
                if cursor in seen_cursors:
                    raise ValueError("DMarket API repeated a pagination cursor")
                seen_cursors.add(cursor)
                params.pop("offset", None)
                params["cursor"] = cursor
                using_cursor = True
            else:
                params.pop("cursor", None)
                if using_cursor or len(items) < PAGE_SIZE:
                    break
                params["offset"] = str(int(params["offset"]) + len(items))
            await asyncio.sleep(PAGE_DELAY)
