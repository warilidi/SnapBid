"""Журнал сигналов: проверяем, что стало с ценой через 7+ дней."""
import sqlite3
import time
from dataclasses import asdict

from config import Config
from pricing import PriceBook, Signal, make_key, net_profit

HOLD = 7 * 24 * 3600


class Journal:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                uid TEXT, price REAL, name TEXT, version TEXT, fair REAL,
                profit REAL, roi REAL, volume_7d INTEGER, currency TEXT,
                wear REAL, lock TEXT, url TEXT, created REAL,
                later_median REAL, later_roi REAL, checked REAL,
                PRIMARY KEY (uid, price))""")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS signal_deliveries (
                user_id INTEGER, uid TEXT, price REAL,
                PRIMARY KEY (user_id, uid, price))""")
        self.db.commit()

    def add(self, s: Signal, now: float | None = None) -> bool:
        """False, если этот лот по этой цене уже был."""
        row = asdict(s) | {"created": now or time.time()}
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        with self.db:
            cur = self.db.execute(
                f"INSERT OR IGNORE INTO signals ({cols}) VALUES ({marks})", row)
        return cur.rowcount == 1

    def add_delivery(self, user_id: int, s: Signal) -> bool:
        """Claim this signal for one user without deduplicating other users."""
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO signal_deliveries VALUES (?, ?, ?)",
                (user_id, s.uid, s.price))
        return cur.rowcount == 1

    def check_outcomes(self, book: PriceBook, cfg: Config, now: float | None = None) -> int:
        now = now or time.time()
        rows = self.db.execute(
            "SELECT uid, price, name, version FROM signals "
            "WHERE checked IS NULL AND currency = ? AND created <= ?",
            (cfg.currency, now - HOLD)).fetchall()
        done = 0
        with self.db:
            for uid, price, name, version in rows:
                stats = book.get(make_key(name, version))
                if stats is None or stats.volume_7d == 0:
                    continue
                roi = net_profit(price, stats.median_7d, cfg) / price
                self.db.execute(
                    "UPDATE signals SET later_median=?, later_roi=?, checked=? "
                    "WHERE uid=? AND price=?",
                    (stats.median_7d, roi, now, uid, price))
                done += 1
        return done
