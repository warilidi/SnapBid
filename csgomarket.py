"""CS:GO Market's public full-export listing snapshots."""
import asyncio
import math
import re
from dataclasses import dataclass

import aiohttp

EXPORT_URL = "https://market.csgo.com/api/full-export/"
# The provider documents a maximum of five API requests per second.
REQUEST_INTERVAL = 0.3
PRICE_SCALE = {"RUB": 100, "USD": 1000, "EUR": 1000}
PART_NAME = re.compile(r"[0-9]+\.json\Z", re.ASCII)
REQUIRED_FIELDS = {"id", "market_hash_name", "price", "float"}


@dataclass(frozen=True)
class Listing:
    item_id: str
    name: str
    price: float
    currency: str
    wear: float | None


def parse_manifest(data: object, currency: str) -> tuple[list[str], list[str]]:
    if (not isinstance(data, dict) or data.get("success") is not True
            or data.get("currency") != currency):
        raise ValueError("Invalid CS:GO Market snapshot")
    fields, parts = data.get("format"), data.get("items")
    if (not isinstance(fields, list) or any(not isinstance(f, str) for f in fields)
            or len(set(fields)) != len(fields) or not REQUIRED_FIELDS.issubset(fields)):
        raise ValueError("Invalid CS:GO Market snapshot format")
    if (not isinstance(parts, list) or not parts
            or any(not isinstance(p, str) or not PART_NAME.fullmatch(p) for p in parts)
            or len(set(parts)) != len(parts)):
        raise ValueError("Invalid CS:GO Market snapshot parts")
    return fields, parts


def parse_listing(row: object, fields: list[str], currency: str) -> Listing:
    if currency not in PRICE_SCALE or not isinstance(row, list) or len(row) != len(fields):
        raise ValueError("Invalid CS:GO Market listing")
    data = dict(zip(fields, row))
    item_id, name, price = data.get("id"), data.get("market_hash_name"), data.get("price")
    if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id <= 0:
        raise ValueError("Invalid CS:GO Market item ID")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Invalid CS:GO Market item name")
    if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
        raise ValueError("Invalid CS:GO Market price")
    try:
        amount = price / PRICE_SCALE[currency]
    except OverflowError as exc:
        raise ValueError("Invalid CS:GO Market price") from exc
    if not math.isfinite(amount):
        raise ValueError("Invalid CS:GO Market price")
    raw_wear = data.get("float")
    try:
        wear = float(raw_wear) if raw_wear is not None and not isinstance(raw_wear, bool) else None
    except (ValueError, TypeError, OverflowError):
        wear = None
    if wear is not None and not 0 <= wear <= 1:
        wear = None
    return Listing(str(item_id), name.strip(), amount, currency, wear)


async def listings(session: aiohttp.ClientSession, currency: str):
    """Yield one complete snapshot; raise on any failed or malformed part."""
    if currency not in PRICE_SCALE:
        raise ValueError("Unsupported CS:GO Market currency")
    async with session.get(EXPORT_URL + currency + ".json") as response:
        response.raise_for_status()
        fields, parts = parse_manifest(await response.json(), currency)
    for part in parts:
        await asyncio.sleep(REQUEST_INTERVAL)
        async with session.get(EXPORT_URL + part) as response:
            response.raise_for_status()
            rows = await response.json()
        if not isinstance(rows, list):
            raise ValueError("Invalid CS:GO Market snapshot part")
        for row in rows:
            yield parse_listing(row, fields, currency)
