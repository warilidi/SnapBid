import asyncio
import sqlite3

import msgpack
import pytest

from config import Config
from journal import HOLD, Journal
from notifier import Notifier, render
from pricing import PriceBook, evaluate

CFG = Config()


def history(m7=100.0, m30=105.0, v7=10, v30=40, version=None):
    return [{"market_hash_name": "AK <Test>", "version": version,
             "last_7_days": {"median": m7, "volume": v7},
             "last_30_days": {"median": m30, "volume": v30}}]


def sale(**kw):
    return {"saleStatus": "listed", "saleType": "public", "currency": "EUR",
            "salePrice": 8000, "marketHashName": "AK <Test>", "version": "default",
            "saleId": 42, "assetId": 55034907, "assetid": "24958657824",
            "url": "ak-test", "wear": 0.12, "lock": None} | kw


def book(**kw):
    b = PriceBook()
    b.load(history(**kw))
    return b


def test_profit_uses_lower_median_and_fee():
    s = evaluate(sale(), book(), CFG)
    assert s.fair == 100
    assert s.profit == pytest.approx(12)
    assert s.roi == pytest.approx(0.15)
    assert s.url == "https://skinport.com/item/ak-test/42"
    assert s.sale_id == "42"
    assert s.asset_id == "24958657824"


def test_high_value_fee():
    cfg = Config(max_price=5000)
    s = evaluate(sale(salePrice=150000), book(m7=2000, m30=2000), cfg)
    assert s.profit == pytest.approx(2000 * 0.94 - 1500)


@pytest.mark.parametrize("change", [
    {"salePrice": 8500}, {"salePrice": 400}, {"salePrice": None},
    {"saleStatus": "sold"}, {"saleType": "private"}, {"currency": "USD"},
    {"version": "Phase 2"}, {"marketHashName": "other"},
])
def test_rejected(change):
    assert evaluate(sale(**change), book(), CFG) is None


def test_low_volume_rejected():
    assert evaluate(sale(), book(v7=2), CFG) is None


def test_null_median_skipped_and_empty_keeps_old():
    b = PriceBook()
    assert b.load(history(m7=None)) == 0
    b = book()
    b.load([])
    assert b.get(("AK <Test>", None))


def test_journal_dedup_and_outcome():
    j, s = Journal(":memory:"), evaluate(sale(), book(), CFG)
    assert j.add(s, now=1000)
    assert j.db.execute(
        "SELECT sale_id, asset_id, url FROM signals"
    ).fetchone() == ("42", "24958657824", "https://skinport.com/item/ak-test/42")
    assert not j.add(s, now=1001)
    assert j.check_outcomes(book(m7=80), CFG, now=1000 + HOLD - 1) == 0
    assert j.check_outcomes(book(m7=80), CFG, now=1000 + HOLD) == 1
    roi = j.db.execute("SELECT later_roi FROM signals").fetchone()[0]
    assert roi == pytest.approx((80 * 0.92 - 80) / 80)


def test_journal_batches_delivery_claims_and_preserves_dedup():
    journal = Journal(":memory:")
    signal = evaluate(sale(), book(), CFG)

    assert journal.add_deliveries([(1, signal), (2, signal)]) == [1, 2]
    assert journal.add_deliveries([(1, signal), (3, signal)]) == [3]
    assert not journal.add_delivery(1, signal)
    assert journal.db.execute("SELECT COUNT(*) FROM signal_deliveries").fetchone()[0] == 3


def test_journal_migrates_existing_signals_table(tmp_path):
    path = tmp_path / "signals.db"
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE signals (
            uid TEXT, price REAL, name TEXT, version TEXT, fair REAL,
            profit REAL, roi REAL, volume_7d INTEGER, currency TEXT,
            wear REAL, lock TEXT, url TEXT, created REAL,
            later_median REAL, later_roi REAL, checked REAL,
            PRIMARY KEY (uid, price))
    """)
    db.close()

    journal = Journal(str(path))
    signal = evaluate(sale(), book(), CFG)
    assert journal.add(signal)
    assert journal.db.execute(
        "SELECT sale_id, asset_id FROM signals"
    ).fetchone() == ("42", "24958657824")


@pytest.mark.parametrize(("lock", "expected"), [
    (msgpack.ExtType(1, b"2026-11-27T00:00:00Z"), "2026-11-27T00:00:00Z"),
    (msgpack.ExtType(7, b"\xff\x00"), "msgpack-ext-7:ff00"),
])
def test_msgpack_lock_is_preserved_in_journal(lock, expected):
    signal = evaluate(sale(lock=lock), book(), CFG)
    assert signal.lock == expected

    journal = Journal(":memory:")
    assert journal.add(signal)
    assert journal.db.execute("SELECT lock FROM signals").fetchone()[0] == expected


def test_render_escapes_html():
    assert "AK &lt;Test&gt;" in render(evaluate(sale(), book(), CFG))


def test_render_only_shows_valid_trade_lock_date():
    valid = evaluate(sale(lock=msgpack.ExtType(1, b"2026-11-27T00:00:00Z")), book(), CFG)
    invalid = evaluate(sale(lock=msgpack.ExtType(7, b"\xff\x00")), book(), CFG)

    assert "Trade lock до: 2026-11-27" in render(valid)
    assert "Trade lock до:" not in render(invalid)


@pytest.mark.parametrize(
    ("changes", "expected_uid", "expected_sale_id", "expected_asset_id", "expected_url"),
    [
        ({}, "42", "42", "24958657824", "https://skinport.com/item/ak-test/42"),
        ({"saleId": None}, "24958657824", None, "24958657824",
         "https://skinport.com/a/24958657824"),
    ],
)
def test_inline_button_keeps_concrete_listing_id(
    changes, expected_uid, expected_sale_id, expected_asset_id, expected_url
):
    signal = evaluate(sale(**changes), book(), CFG)

    class CaptureBot:
        async def send_message(self, *args, **kwargs):
            self.reply_markup = kwargs["reply_markup"]

    bot = CaptureBot()
    asyncio.run(Notifier(bot, None).send(1, signal))
    button = bot.reply_markup.inline_keyboard[0][0]
    journal = Journal(":memory:")
    assert journal.add(signal)

    assert signal.uid == expected_uid
    assert signal.sale_id == expected_sale_id
    assert signal.asset_id == expected_asset_id
    assert signal.url == expected_url
    assert journal.db.execute(
        "SELECT uid, sale_id, asset_id, url FROM signals"
    ).fetchone() == (
        expected_uid, expected_sale_id, expected_asset_id, expected_url
    )
    assert button.text == "Открыть лот"
    assert button.url == expected_url


def test_missing_listing_ids_do_not_generate_generic_url():
    signal = evaluate(sale(saleId=None, assetid=None), book(), CFG)
    assert signal is None
