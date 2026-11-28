import asyncio
import sqlite3

import pytest

import csgomarket
import main
from access import Access
from config import Config
from main import handle_waxpeer_listing, scan_csgomarket
from stats import Stats
from test_access import ADMIN, GUEST, setup
from waxpeer import WaxpeerListing


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
        journal, access, _, _, _, _ = setup(Config(admin_id=ADMIN))
        access.redeem(access.create_invite(), GUEST, "guest")
        access.set_market_enabled(GUEST, "waxpeer", False)
        access.set_market_enabled(ADMIN, "waxpeer", True)
        notifier, stats = CaptureNotifier(), Stats()
        item = WaxpeerListing("123", "AK", 12, .15)
        handle_waxpeer_listing(item, journal, notifier, access, stats)
        access.set_market_enabled(GUEST, "waxpeer", True)
        handle_waxpeer_listing(item, journal, notifier, access, stats)
        handle_waxpeer_listing(item, journal, notifier, access, stats)
        assert [uid for uid, _ in notifier.sent] == [ADMIN, GUEST]
        assert all(s.url is None and s.currency == "USD" for _, s in notifier.sent)
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
        await scan_csgomarket(None, "EUR", journal, notifier, access, stats)
        assert notifier.sent == []
        with pytest.raises(RuntimeError):
            await scan_csgomarket(None, "EUR", journal, notifier, access, stats)
        assert notifier.sent == []
        assert journal.db.execute("SELECT price FROM csgomarket_snapshot").fetchone() == (20,)
        await scan_csgomarket(None, "EUR", journal, notifier, access, stats)
        assert [(s.listing_id, s.price) for _, s in notifier.sent] == [("1", 15), ("2", 30)]
        await scan_csgomarket(None, "EUR", journal, notifier, access, stats)
        assert len(notifier.sent) == 2
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
