import asyncio

import pytest
from nacl.bindings import crypto_sign_keypair, crypto_sign_open

from config import Config
from dmarket import DMarketClient, DMarketListing, parse_listing
from journal import Journal
from main import handle_dmarket_listing
from notifier import MarketplaceSignal, Notifier
from stats import Stats

from test_access import ADMIN, GUEST, setup


def test_dmarket_signing_matches_documented_message():
    public_key, secret_key = crypto_sign_keypair()
    client = DMarketClient(public_key.hex(), secret_key.hex())
    path = "/exchange/v1/market/items"
    query = "?gameId=a8db&title=AK%20%26%20Test"
    timestamp = "1700000000"
    headers = client.headers("GET", path, query, timestamp)

    signature = bytes.fromhex(headers["X-Request-Sign"].split()[-1])
    message = f"GET{path}{query}{timestamp}".encode()
    assert crypto_sign_open(signature + message, public_key) == message
    assert headers["X-Api-Key"] == public_key.hex()
    assert headers["X-Sign-Date"] == timestamp
    assert headers["X-Request-Sign"].startswith("dmar ed25519 ")


def test_dmarket_request_signs_the_exact_encoded_query():
    public_key, secret_key = crypto_sign_keypair()
    client = DMarketClient(public_key.hex(), secret_key.hex())
    captured = {}

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {"objects": []}

    class Session:
        def get(self, url, headers, allow_redirects):
            captured.update(url=url, headers=headers, allow_redirects=allow_redirects)
            return Response()

    async def request():
        return await client._get_page(
            Session(), {"gameId": "a8db", "title": "AK test"})

    assert asyncio.run(request()) == {"objects": []}
    encoded_query = captured["url"].partition("?")[2]
    timestamp = captured["headers"]["X-Sign-Date"]
    message = (
        f"GET/exchange/v1/market/items?{encoded_query}{timestamp}".encode())
    signed = bytes.fromhex(captured["headers"]["X-Request-Sign"].split()[-1])
    assert "title=AK%20test" in encoded_query
    assert not captured["allow_redirects"]
    assert crypto_sign_open(signed + message, public_key) == message


def test_dmarket_config_hides_credentials_from_repr():
    config = Config(dmarket_api_key="public-secret", dmarket_secret_key="private-secret")
    assert "public-secret" not in repr(config)
    assert "private-secret" not in repr(config)


def test_dmarket_listing_converts_cents_and_parses_float():
    listing = parse_listing({
        "itemId": "listing-1",
        "title": "AK-47 | Test",
        "amount": 1292,
        "extra": {"floatValue": "0.1452"},
    })
    assert listing == DMarketListing("listing-1", "AK-47 | Test", 12.92, 0.1452)


@pytest.mark.parametrize("item", [
    {"itemId": "x", "title": "item", "amount": "1292"},
    {"itemId": "", "title": "item", "amount": 1292},
    {"itemId": "x", "title": "", "amount": 1292},
    {"itemId": "x", "title": "item", "amount": -1},
])
def test_dmarket_invalid_listing_data_is_rejected(item):
    with pytest.raises(ValueError):
        parse_listing(item)


def test_dmarket_pagination_uses_returned_cursor(monkeypatch):
    public_key, secret_key = crypto_sign_keypair()
    client = DMarketClient(public_key.hex(), secret_key.hex())
    pages = [
        {"objects": [
            {"itemId": f"id-{i}", "title": "item", "amount": 100}
            for i in range(50)
        ], "cursor": "next-page"},
        {"objects": [
            {"itemId": "last", "title": "last item", "amount": 200}
        ]},
    ]
    requests = []

    async def get_page(session, params):
        requests.append(params.copy())
        return pages.pop(0)

    async def no_wait(_):
        pass

    monkeypatch.setattr(client, "_get_page", get_page)
    monkeypatch.setattr("dmarket.asyncio.sleep", no_wait)

    async def collect():
        return [item async for item in client.listings(object(), 100, 500)]

    listings = asyncio.run(collect())
    assert len(listings) == 51
    assert requests[0]["gameId"] == "a8db"
    assert requests[0]["currency"] == "USD"
    assert requests[0]["priceFrom"] == "100"
    assert requests[1]["cursor"] == "next-page"
    assert "offset" not in requests[1]


def test_dmarket_filters_and_delivery_are_isolated_per_user():
    async def run():
        cfg = Config(
            admin_id=ADMIN,
            dmarket_api_key="configured",
            dmarket_secret_key="configured",
        )
        journal, access, _, _, _, _ = setup(cfg)
        assert access.redeem(access.create_invite(), GUEST, "guest")
        access.db.execute(
            "UPDATE user_market_settings SET enabled = 1 WHERE provider = 'dmarket'")
        access.set_market_price(GUEST, "dmarket", 15, 20)
        access.set_market_float(GUEST, "dmarket", 0.1, 0.2)

        class CaptureNotifier:
            def __init__(self):
                self.sent = []

            def push(self, user_id, signal):
                self.sent.append((user_id, signal))

        notifier = CaptureNotifier()
        stats = Stats()
        handle_dmarket_listing(
            DMarketListing("listing-1", "AK | Test", 17, 0.15),
            journal, notifier, access, stats)
        assert [user_id for user_id, _ in notifier.sent] == [ADMIN, GUEST]

        notifier.sent.clear()
        handle_dmarket_listing(
            DMarketListing("listing-2", "AK | Test", 17, None),
            journal, notifier, access, stats)
        assert [user_id for user_id, _ in notifier.sent] == [ADMIN]

        notifier.sent.clear()
        handle_dmarket_listing(
            DMarketListing("listing-3", "AK | Test", 25, 0.15),
            journal, notifier, access, stats)
        assert [user_id for user_id, _ in notifier.sent] == [ADMIN]
        assert journal.db.execute(
            "SELECT COUNT(*) FROM market_deliveries WHERE user_id = ?", (GUEST,)
        ).fetchone()[0] == 1

    asyncio.run(run())


def test_dmarket_cannot_be_enabled_for_restricted_regions():
    _, access, *_ = setup(Config(admin_id=ADMIN))
    with pytest.raises(ValueError, match="России и Беларуси"):
        access.set_market_enabled(ADMIN, "dmarket", True)
    assert not access.market_settings(ADMIN, "dmarket").enabled


def test_marketplace_notification_has_no_unverified_generic_url():
    class CaptureBot:
        async def send_message(self, *args, **kwargs):
            self.text = args[1]
            self.reply_markup = kwargs["reply_markup"]

    bot = CaptureBot()
    signal = MarketplaceSignal(
        "DMarket", "listing-1", "AK <Test>", 12.92, "USD", 0.1452)
    asyncio.run(Notifier(bot, None).send(ADMIN, signal))
    assert "ID лота: <code>listing-1</code>" in bot.text
    assert "AK &lt;Test&gt;" in bot.text
    assert "комиссия не включены" in bot.text
    assert bot.reply_markup is None


def test_dmarket_market_signal_dedup_and_baseline():
    journal = Journal(":memory:")
    assert not journal.market_baseline_completed("dmarket")
    assert journal.add_market_signal("dmarket", "lot", 12.92)
    assert not journal.add_market_signal("dmarket", "lot", 12.92)
    assert journal.add_market_signal("dmarket", "lot", 12.91)
    journal.complete_market_baseline("dmarket")
    assert journal.market_baseline_completed("dmarket")


def test_status_displays_process_uptime():
    now = [100.0]
    stats = Stats(clock=lambda: now[0])
    now[0] += 60
    assert stats.uptime() == "1 мин."
    now[0] += 59 * 60
    assert stats.uptime() == "1 ч. 0 мин."
    now[0] += 23 * 60 * 60 + 2 * 60
    assert stats.uptime() == "1 дн. 0 ч. 2 мин."
    assert "Время работы процесса: 1 дн. 0 ч. 2 мин." in stats.text()
