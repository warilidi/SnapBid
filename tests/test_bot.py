import msgpack
import pytest

from config import Config
from journal import HOLD, Journal
from notifier import render
from pricing import PriceBook, evaluate

CFG = Config()


def history(m7=100.0, m30=105.0, v7=10, v30=40, version=None):
    return [{"market_hash_name": "AK <Test>", "version": version,
             "last_7_days": {"median": m7, "volume": v7},
             "last_30_days": {"median": m30, "volume": v30}}]


def sale(**kw):
    return {"saleStatus": "listed", "saleType": "public", "currency": "EUR",
            "salePrice": 8000, "marketHashName": "AK <Test>", "version": "default",
            "saleId": 42, "url": "ak-test", "wear": 0.12, "lock": None} | kw


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
    assert not j.add(s, now=1001)
    assert j.check_outcomes(book(m7=80), CFG, now=1000 + HOLD - 1) == 0
    assert j.check_outcomes(book(m7=80), CFG, now=1000 + HOLD) == 1
    roi = j.db.execute("SELECT later_roi FROM signals").fetchone()[0]
    assert roi == pytest.approx((80 * 0.92 - 80) / 80)


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
