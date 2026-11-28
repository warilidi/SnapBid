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
                wear REAL, lock TEXT, sale_id TEXT, asset_id TEXT,
                url TEXT, created REAL,
                later_median REAL, later_roi REAL, checked REAL,
                PRIMARY KEY (uid, price))""")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS signal_deliveries (
                user_id INTEGER, uid TEXT, price REAL,
                PRIMARY KEY (user_id, uid, price))""")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS market_signals (
                provider TEXT, listing_id TEXT, price REAL, created REAL,
                PRIMARY KEY (provider, listing_id, price))""")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS market_deliveries (
                user_id INTEGER, provider TEXT, listing_id TEXT, price REAL,
                PRIMARY KEY (user_id, provider, listing_id, price))""")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS market_provider_state (
                provider TEXT PRIMARY KEY, baseline_completed INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS market_alert_cooldown (
                user_id INTEGER NOT NULL, provider TEXT NOT NULL, last_claimed REAL NOT NULL,
                PRIMARY KEY (user_id, provider))""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(signals)")}
        for column in ("sale_id", "asset_id"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE signals ADD COLUMN {column} TEXT")
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
        return bool(self.add_deliveries([(user_id, s)]))

    def add_deliveries(self, deliveries: list[tuple[int, Signal]]) -> list[int]:
        """Claim all new per-user deliveries in one SQLite transaction."""
        claimed = []
        if not deliveries:
            return claimed
        with self.db:
            for user_id, signal in deliveries:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO signal_deliveries VALUES (?, ?, ?)",
                    (user_id, signal.uid, signal.price))
                if cur.rowcount == 1:
                    claimed.append(user_id)
        return claimed

    def add_market_signal(
        self, provider: str, listing_id: str, price: float, now: float | None = None
    ) -> bool:
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO market_signals VALUES (?, ?, ?, ?)",
                (provider, listing_id, price, now if now is not None else time.time()))
        return cur.rowcount == 1

    def market_baseline_completed(self, provider: str) -> bool:
        row = self.db.execute(
            "SELECT baseline_completed FROM market_provider_state WHERE provider = ?",
            (provider,)).fetchone()
        return bool(row and row[0])

    def complete_market_baseline(self, provider: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO market_provider_state (provider, baseline_completed) "
                "VALUES (?, 1) ON CONFLICT(provider) DO UPDATE "
                "SET baseline_completed = 1", (provider,))

    def add_market_deliveries(
        self, provider: str, listing_id: str, price: float, user_ids: list[int],
        *, cooldown: float = 0,
    ) -> list[int]:
        claimed = []
        if not user_ids:
            return claimed
        now = time.time()
        with self.db:
            for user_id in user_ids:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO market_deliveries "
                    "SELECT ?, ?, ?, ? WHERE ? = 0 OR NOT EXISTS ("
                    "SELECT 1 FROM market_alert_cooldown "
                    "WHERE user_id = ? AND provider = ? AND last_claimed > ?)",
                    (user_id, provider, listing_id, price, cooldown, user_id, provider, now - cooldown))
                if cur.rowcount != 1:
                    continue
                if cooldown:
                    self.db.execute(
                        "INSERT INTO market_alert_cooldown VALUES (?, ?, ?) "
                        "ON CONFLICT(user_id, provider) DO UPDATE SET last_claimed = excluded.last_claimed",
                        (user_id, provider, now))
                claimed.append(user_id)
        return claimed

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
