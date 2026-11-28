import asyncio
import sqlite3

import pytest

import csgomarket
import main
from access import Access
from config import Config
from main import handle_marketplace_listing, handle_waxpeer_listing, scan_csgomarket
from notifier import ListingPage, MarketplaceSignal, Notifier
from pricing import PriceBook
from stats import Stats
from test_access import ADMIN, GUEST, setup
from waxpeer import WaxpeerListing


def reference_book():
    book = PriceBook()
    book.load([
        {"market_hash_name": name, "version": phase,
         "last_7_days": {"median": price, "volume": 10},
         "last_30_days": {"median": price, "volume": 30}}
        for name, phase, price in [("AK", None, 25), ("AWP", None, 50),
                                   ("★ Knife | Doppler", "Phase 2", 35)]
    ])
    return book


class CaptureNotifier:
    def __init__(self):
        self.sent = []

    def push(self, user_id, signal):
        self.sent.append((user_id, signal))


def test_market_settings_are_independent_and_commands_apply():
    async def run():
        _, access, _, session, say, tap = setup(Config(admin_id=ADMIN))
        assert access.redeem(access.create_invite(), GUEST, "guest")
        assert access.market_settings(ADMIN, "waxpeer").enabled
        await tap(ADMIN, "markets:toggle:waxpeer")
        await tap(ADMIN, "markets:toggle:csgomarket")
        assert not access.market_settings(ADMIN, "waxpeer").enabled
        assert not access.market_settings(ADMIN, "csgomarket").enabled
        assert access.market_settings(GUEST, "waxpeer").enabled
        await say(ADMIN, "/marketprice waxpeer 10 30")
        await say(ADMIN, "/marketfloat waxpeer 0.1 0.2")
        s = access.market_settings(ADMIN, "waxpeer")
        assert (s.min_price, s.max_price, s.min_float, s.max_float) == (10, 30, .1, .2)
        assert "USD" in (await tap(ADMIN, "markets:open"))[-1][1]
        await say(ADMIN, "/marketfloat waxpeer off")
        assert access.market_settings(ADMIN, "waxpeer").min_float is None
        await say(ADMIN, "/marketprice waxpeer 30 10")
        assert access.market_settings(ADMIN, "waxpeer").min_price == 10
        await tap(333, "markets:toggle:waxpeer")
        assert session.answers[-1] == "Доступ закрыт."
    asyncio.run(run())


def test_waxpeer_delivery_is_per_user_and_deduplicated():
    async def run():
        journal, access, bot, session, _, _ = setup(Config(admin_id=ADMIN))
        access.redeem(access.create_invite(), GUEST, "guest")
        access.set_market_enabled(GUEST, "waxpeer", False)
        access.set_market_enabled(ADMIN, "waxpeer", True)
        notifier, stats = CaptureNotifier(), Stats()
        item = WaxpeerListing("123", "AK", 12, .15)
        handle_waxpeer_listing(item, journal, notifier, access, stats, reference_book())
        access.set_market_enabled(GUEST, "waxpeer", True)
        handle_waxpeer_listing(item, journal, notifier, access, stats, reference_book())
        handle_waxpeer_listing(item, journal, notifier, access, stats, reference_book())
        assert [uid for uid, _ in notifier.sent] == [ADMIN, GUEST]
        assert all(s.url == "https://waxpeer.com/ak/item?id=123" and s.currency == "USD"
                   for _, s in notifier.sent)
        await Notifier(bot, access).send(ADMIN, notifier.sent[0][1])
        assert session.markups[-1].inline_keyboard[0][0].url == notifier.sent[0][1].url
    asyncio.run(run())


def test_market_selections_survive_restart():
    journal, access, _, _, _, _ = setup(Config(admin_id=ADMIN))
    access.set_market_enabled(ADMIN, "waxpeer", False)
    restarted = Access(journal.db, Config(admin_id=ADMIN))
    assert not restarted.market_settings(ADMIN, "waxpeer").enabled
    assert restarted.market_settings(ADMIN, "csgomarket").enabled


def test_csgomarket_snapshot_baseline_failures_and_changes(monkeypatch):
    async def run():
        journal, access, _, _, _, _ = setup(Config(admin_id=ADMIN))
        access.set_market_enabled(ADMIN, "csgomarket", True)
        access.set_market_price(ADMIN, "csgomarket", 10, 100)
        notifier, stats = CaptureNotifier(), Stats()
        samples = [
            ([csgomarket.Listing("1", "AK", 20, "EUR", .2)], False),
            ([csgomarket.Listing("1", "AK", 15, "EUR", .2)], True),
            ([csgomarket.Listing("1", "AK", 15, "EUR", .2),
              csgomarket.Listing("2", "AWP", 30, "EUR", None)], False),
            ([csgomarket.Listing("1", "AK", 15, "EUR", .2),
              csgomarket.Listing("2", "AWP", 30, "EUR", None)], False),
        ]

        async def feed(session, currency):
            rows, failed = samples.pop(0)
            for row in rows:
                yield row
            if failed:
                raise RuntimeError("incomplete snapshot")

        monkeypatch.setattr(csgomarket, "listings", feed)
        await scan_csgomarket(None, "EUR", journal, notifier, access, stats, reference_book())
        assert notifier.sent == []
        with pytest.raises(RuntimeError):
            await scan_csgomarket(None, "EUR", journal, notifier, access, stats, reference_book())
        assert notifier.sent == []
        assert journal.db.execute("SELECT price FROM csgomarket_snapshot").fetchone() == (20,)
        await scan_csgomarket(None, "EUR", journal, notifier, access, stats, reference_book())
        assert [(s.listing_id, s.price, s.url) for _, s in notifier.sent] == [
            ("1", 15, "https://market.csgo.com/ru/AK?id=1")]
        await scan_csgomarket(None, "EUR", journal, notifier, access, stats, reference_book())
        assert len(notifier.sent) == 1
    asyncio.run(run())


def test_main_starts_both_market_monitors(monkeypatch):
    async def run():
        started = set()
        journals = []
        real_journal = main.Journal

        def journal_factory(path):
            journal = real_journal(path)
            journals.append(journal)
            return journal

        def worker(name):
            async def task(*args):
                started.add(name)
                await asyncio.Event().wait()
            return task

        class Session:
            async def close(self):
                pass

        class Bot:
            def __init__(self, token):
                self.session = Session()

        class Notifier:
            def __init__(self, *args):
                pass

            async def close(self):
                pass

        class Dispatcher:
            def include_router(self, router):
                pass

            async def start_polling(self, *args, **kwargs):
                await asyncio.sleep(0)
                raise RuntimeError("stop test")

        for name in ("refresh_prices", "listen", "log_stats", "monitor_waxpeer", "monitor_csgomarket"):
            monkeypatch.setattr(main, name, worker(name))
        monkeypatch.setattr(main.Config, "from_env", lambda: Config(
            token="1:test", admin_id=ADMIN, db_path=":memory:", stars_price=0))
        monkeypatch.setattr(main, "Bot", Bot)
        monkeypatch.setattr(main, "Journal", journal_factory)
        monkeypatch.setattr(main, "Notifier", Notifier)
        monkeypatch.setattr(main, "Dispatcher", Dispatcher)
        monkeypatch.setattr(main, "build_router", lambda *args: None)
        with pytest.raises(ExceptionGroup):
            await main.main()
        assert {"monitor_waxpeer", "monitor_csgomarket"} <= started
        with pytest.raises(sqlite3.ProgrammingError):
            journals[0].db.execute("SELECT 1")
    asyncio.run(run())

def test_alert_limit_is_per_marketplace_and_user_and_missing_links_are_skipped(monkeypatch, tmp_path):
    from journal import Journal

    now = [1000.0]
    monkeypatch.setattr("journal.time.time", lambda: now[0])
    path = str(tmp_path / "signals.db")
    cfg = Config(admin_id=ADMIN)
    journal = Journal(path)
    access = Access(journal.db, cfg)
    notifier, stats = CaptureNotifier(), Stats()

    def deliver(signal):
        handle_marketplace_listing(signal, journal, notifier, access, stats, reference_book())

    deliver(MarketplaceSignal("Waxpeer", "1", "AK", 10, "USD"))
    assert not notifier.sent
    handle_waxpeer_listing(WaxpeerListing("1", "AK", 10), journal, notifier, access, stats, reference_book())
    journal.db.close()
    journal = Journal(path)
    access = Access(journal.db, cfg)
    item = csgomarket.Listing("2", "★ Knife | Doppler", 20, "EUR", .01, "Phase 2")
    signal = MarketplaceSignal("CSGOMarket", item.item_id, item.name, item.price,
                               item.currency, item.wear, item.url, phase=item.phase)
    assert item.url == (
        "https://market.csgo.com/ru/%E2%98%85%20Knife%20%7C%20Doppler?id=2&phase-product=Phase+2")
    deliver(signal)
    other = WaxpeerListing("3", "AK", 12)
    now[0] += 599
    handle_waxpeer_listing(other, journal, notifier, access, stats, reference_book())
    assert [s.listing_id for _, s in notifier.sent] == ["1", "2"]
    now[0] += 1
    handle_waxpeer_listing(other, journal, notifier, access, stats, reference_book())
    assert [s.listing_id for _, s in notifier.sent] == ["1", "2", "3"]
    journal.db.close()

@pytest.mark.parametrize(("signal", "body"), [
    (MarketplaceSignal("Waxpeer", "12", "AK", 10, "USD"),
     '<script type="application/ld+json">{"@type":"Product","sku":"12","name":"AK"}</script>'),
    (MarketplaceSignal("CSGOMarket", "12", "AK & More", 10, "USD"),
     '<span data-id="12" data-hash="AK &amp; More">AK &amp; More</span>'),
])
def test_listing_page_rejects_other_lot(signal, body):
    page = ListingPage(signal)
    page.feed(body.replace("12", "123"))
    assert not page.matched
    page = ListingPage(signal)
    page.feed(body)
    assert page.matched


def test_market_filters_by_roi_and_requires_fresh_reference_prices():
    journal, access, _, _, _, _ = setup(Config(admin_id=ADMIN))
    access.redeem(access.create_invite(), GUEST, "guest")
    access.set_roi(GUEST, .20)
    notifier, stats = CaptureNotifier(), Stats()
    book = reference_book()
    # Net resale value: 25 * 0.92 = 23; ROI at 20 is 15%.
    item = WaxpeerListing("1", "AK", 20)
    handle_waxpeer_listing(item, journal, notifier, access, stats, PriceBook())
    handle_waxpeer_listing(WaxpeerListing("2", "AK", 22), journal, notifier, access, stats, book)
    assert not notifier.sent
    handle_waxpeer_listing(item, journal, notifier, access, stats, book)
    assert [uid for uid, _ in notifier.sent] == [ADMIN]
    assert notifier.sent[0][1].roi == pytest.approx(.15)
    journal.db.close()


def test_telegram_worker_only_sends_confirmed_listing(monkeypatch):
    async def run():
        journal, access, bot, session, _, _ = setup(Config(admin_id=ADMIN))
        notifier = Notifier(bot, access, pause=0)

        async def verify(signal):
            return signal.listing_id == "22"

        monkeypatch.setattr("notifier.confirms_listing", verify)
        for listing_id in ("11", "22"):
            item = WaxpeerListing(listing_id, "AK", 10)
            notifier.push(ADMIN, MarketplaceSignal("Waxpeer", listing_id, item.name,
                                                   item.price, "USD", url=item.url))
        try:
            await asyncio.wait_for(notifier.queues[ADMIN].join(), 2)
            assert len(session.sent) == 1
            button = session.markups[-1].inline_keyboard[0][0]
            assert button.url == "https://waxpeer.com/ak/item?id=22"
        finally:
            await notifier.close()
            journal.db.close()
    asyncio.run(run())
