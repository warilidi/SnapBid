import asyncio

import pytest

from waxpeer import WaxpeerListing, listen, parse_listing


def test_parse_waxpeer_listing():
    assert parse_listing({
        "game": "csgo", "item_id": "53927617185", "name": "AK | Test",
        "price": 12920, "float": 0.1452,
    }) == WaxpeerListing("53927617185", "AK | Test", 12.92, 0.1452)
    assert parse_listing({
        "game": "csgo", "item_id": "1", "name": "AK", "price": 1,
        "float": float("nan"),
    }).wear is None


@pytest.mark.parametrize("changes", [
    {"game": "rust"}, {"item_id": ""}, {"item_id": "1/2"},
    {"name": " "}, {"price": True}, {"price": 0}, {"price": "1000"},
])
def test_invalid_listing_is_rejected(changes):
    data = {"game": "csgo", "item_id": "123", "name": "AK", "price": 1000}
    with pytest.raises(ValueError):
        parse_listing(data | changes)


def test_feed_subscribes_and_only_dispatches_valid_new_listings(monkeypatch):
    calls, received = [], []

    class Client:
        def __init__(self, **kwargs):
            self.handlers = {}
            calls.append(("init", kwargs))

        def event(self, fn):
            self.handlers[fn.__name__] = fn
            return fn

        def on(self, event):
            def register(fn):
                self.handlers[event] = fn
                return fn
            return register

        async def emit(self, event, data):
            calls.append(("emit", event, data))

        async def connect(self, site, transports):
            calls.append(("connect", site, transports))
            await self.handlers["connect"]()

        async def wait(self):
            await self.handlers["new"]({"game": "csgo", "item_id": "1",
                                        "name": "AK", "price": 12000})
            await self.handlers["new"]({"game": "csgo", "item_id": "1"})
            raise asyncio.CancelledError

        async def disconnect(self):
            calls.append(("disconnect",))

    monkeypatch.setattr("waxpeer.socketio.AsyncClient", Client)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(listen(received.append))
    assert ("connect", "https://waxpeer.com", ["websocket"]) in calls
    assert ("emit", "sub", {"name": "csgo"}) in calls
    assert calls[-1] == ("disconnect",)
    assert received == [WaxpeerListing("1", "AK", 12.0)]

def test_snapshot_uses_fresh_exact_name_bids_and_expires(monkeypatch):
    from waxpeer import BuyOrderBook, WaxpeerListing

    now = [1000]
    monkeypatch.setattr("waxpeer.time.time", lambda: now[0])
    book = BuyOrderBook()
    listing = WaxpeerListing("1", "AK | Test (Field-Tested)", 10)
    payload = {"success": True, "timestamp": 995, "offers": [
        {"name": listing.name, "max": "24000"},
        {"name": listing.name, "max": 23000},
        {"name": listing.name, "max": True},
        {"name": "★ Knife | Doppler", "max": 50000},
    ]}
    book.load(payload)
    assert book.bid(listing) == 24
    assert book.bid(WaxpeerListing("2", "AK | Test (Minimal Wear)", 10)) is None
    assert book.bid(WaxpeerListing("3", "★ Knife | Doppler", 10, phase="Phase 2")) is None
    with pytest.raises(ValueError):
        book.load({**payload, "timestamp": 600})
    assert book.bid(listing) == 24
    now[0] = 1295
    assert book.bid(listing) is None
