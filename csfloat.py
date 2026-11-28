"""Public CSFloat listings client and response parser."""
import asyncio
import math
from dataclasses import dataclass

import aiohttp

API = "https://csfloat.com/api/v1/listings"
PAGE_SIZE = 50


@dataclass(frozen=True)
class CSFloatListing:
    listing_id: str
    name: str
    price: float
    wear: float | None
    url: str


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_listing(item: dict) -> CSFloatListing:
    listing_id = _text(item.get("id") or item.get("listing_id"))
    nested = item.get("item")
    item_data = nested if isinstance(nested, dict) else {}
    name = _text(
        item_data.get("market_hash_name")
        or item_data.get("item_name")
        or item.get("name")
        or item.get("market_hash_name"))
    raw_price = item.get("price")
    if isinstance(raw_price, dict):
        raw_price = raw_price.get("amount") or raw_price.get("value")
    if isinstance(raw_price, bool):
        raise ValueError("CSFloat listing has an invalid price")
    try:
        numeric_price = float(raw_price)
        price = numeric_price / 100 if numeric_price >= 100 else numeric_price
    except (TypeError, ValueError):
        raise ValueError("CSFloat listing has an invalid price")
    if not listing_id or not name or not math.isfinite(price) or price <= 0:
        raise ValueError("CSFloat listing has invalid listing data")

    raw_wear = item_data.get("float_value")
    if raw_wear is None:
        raw_wear = item_data.get("float")
    if raw_wear is None:
        raw_wear = item.get("float_value")
    try:
        wear = float(raw_wear)
    except (TypeError, ValueError):
        wear = None
    if wear is not None and (not math.isfinite(wear) or not 0 <= wear <= 1):
        wear = None
    return CSFloatListing(
        listing_id, name, price, wear, f"https://csfloat.com/item/{listing_id}?id={listing_id}")


class CSFloatClient:
    async def listings(self, session: aiohttp.ClientSession, low: float, high: float):
        params = {
            "limit": str(PAGE_SIZE),
            "page": "0",
            "sort_by": "most_recent",
            "min_price": str(max(0, math.floor(low * 100))),
            "max_price": str(math.ceil(high * 100)),
        }
        async with session.get(API, params=params, allow_redirects=False) as response:
            response.raise_for_status()
            payload = await response.json()
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(rows, dict):
            rows = rows.get("listings") or rows.get("items")
        if not isinstance(rows, list):
            raise ValueError("CSFloat API returned an invalid listings response")
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError("CSFloat API returned an invalid listing")
            yield parse_listing(item)
        await asyncio.sleep(0)
