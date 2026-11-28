"""BitSkins authenticated inventory client and response parser."""
import math
from dataclasses import dataclass

import aiohttp

API = "https://api.bitskins.com"
MARKET_INVENTORY_PATH = "/market/get_inventory"


@dataclass(frozen=True)
class BitSkinsListing:
    listing_id: str
    name: str
    price: float
    wear: float | None
    url: str


def parse_listing(item: dict) -> BitSkinsListing:
    listing_id = str(item.get("id") or item.get("market_hash_name") or "").strip()
    name = str(
        item.get("market_hash_name")
        or item.get("name")
        or item.get("item_name")
        or "").strip()
    raw_price = item.get("price")
    if isinstance(raw_price, dict):
        raw_price = raw_price.get("amount") or raw_price.get("value")
    if isinstance(raw_price, bool):
        raise ValueError("BitSkins listing has an invalid price")
    try:
        price = float(raw_price)
    except (TypeError, ValueError):
        raise ValueError("BitSkins listing has an invalid price")
    if price >= 1000:
        price /= 100
    if not listing_id or not name or not math.isfinite(price) or price <= 0:
        raise ValueError("BitSkins listing has invalid listing data")
    raw_wear = item.get("float_value")
    if raw_wear is None:
        raw_wear = item.get("float")
    try:
        wear = float(raw_wear)
    except (TypeError, ValueError):
        wear = None
    if wear is not None and (not math.isfinite(wear) or not 0 <= wear <= 1):
        wear = None
    return BitSkinsListing(
        listing_id, name, price, wear,
        f"https://bitskins.com/item/{listing_id}?id={listing_id}")


class BitSkinsClient:
    def __init__(self, api_key: str, api_secret: str) -> None:
        if not api_key or not api_secret:
            raise ValueError("BitSkins API credentials are required")
        self.api_key = api_key
        self.api_secret = api_secret

    async def listings(
        self,
        session: aiohttp.ClientSession,
        low: float,
        high: float,
    ):
        params = {
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "app_id": "730",
            "min_price": str(low),
            "max_price": str(high),
        }
        async with session.get(
                f"{API}{MARKET_INVENTORY_PATH}",
                params=params,
                allow_redirects=False) as response:
            response.raise_for_status()
            payload = await response.json()
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(rows, dict):
            rows = rows.get("items") or rows.get("listings") or rows.get("inventory")
        if not isinstance(rows, list):
            raise ValueError("BitSkins API returned an invalid inventory response")
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError("BitSkins API returned an invalid listing")
            yield parse_listing(item)
