import asyncio

import pytest

from csgomarket import Listing, listings, parse_manifest


def test_manifest_rejects_missing_columns_and_unsafe_part_names():
    valid = {"success": True, "currency": "USD",
             "format": ["id", "market_hash_name", "price", "float"],
             "items": ["123.json"]}
    with pytest.raises(ValueError):
        parse_manifest(valid | {"items": ["../123.json"]}, "USD")
    with pytest.raises(ValueError):
        parse_manifest(valid | {"format": ["id", "price"]}, "USD")
    with pytest.raises(ValueError):
        parse_manifest(valid, "RUB")


def test_snapshot_uses_field_names_and_currency_scaling(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def json(self):
            return self.payload

    class Session:
        def __init__(self):
            self.urls = []

        def get(self, url):
            self.urls.append(url)
            if url.endswith("RUB.json"):
                return Response({"success": True, "currency": "RUB",
                                 "format": ["market_hash_name", "float", "price", "id"],
                                 "items": ["123.json", "456.json"]})
            if url.endswith("123.json"):
                return Response([["AK | Test", "0.1452", 1292, 123456]])
            return Response([["AWP | Test", None, 500, 123457]])

    async def no_wait(delay):
        assert delay >= 0.2

    monkeypatch.setattr("csgomarket.asyncio.sleep", no_wait)
    session = Session()

    async def run():
        return [row async for row in listings(session, "RUB")]

    assert asyncio.run(run()) == [Listing("123456", "AK | Test", 12.92, "RUB", 0.1452),
                                  Listing("123457", "AWP | Test", 5, "RUB", None)]
    assert session.urls == ["https://market.csgo.com/api/full-export/RUB.json",
                            "https://market.csgo.com/api/full-export/123.json",
                            "https://market.csgo.com/api/full-export/456.json"]
