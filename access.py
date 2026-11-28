"""Кто имеет доступ к боту и с какими фильтрами."""
import math
import secrets
import sqlite3
import time
from dataclasses import dataclass, replace

from config import Config

SUPPORTED_MARKETPLACES = ("skinport", "dmarket", "waxpeer", "csgomarket")
NEW_MARKETPLACES = ("waxpeer", "csgomarket")


def marketplace_currency(provider: str, cfg: Config) -> str:
    if provider == "csgomarket" and cfg.currency in ("USD", "EUR", "RUB"):
        return cfg.currency
    return "USD"


@dataclass(frozen=True)
class User:
    user_id: int
    name: str
    min_roi: float
    min_price: float
    max_price: float
    paused: bool
    expires_at: float | None = None  # None: бессрочно

    def apply(self, cfg: Config) -> Config:
        return replace(cfg, min_roi=self.min_roi, min_price=self.min_price,
                       max_price=self.max_price)


@dataclass(frozen=True)
class MarketSettings:
    provider: str
    enabled: bool
    min_price: float
    max_price: float
    min_roi: float | None = None
    min_float: float | None = None
    max_float: float | None = None


class Access:
    def __init__(self, db: sqlite3.Connection, cfg: Config) -> None:
        self.db, self.cfg = db, cfg
        db.execute("PRAGMA foreign_keys = ON")
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, name TEXT, min_roi REAL,
                min_price REAL, max_price REAL, paused INTEGER DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS invites (
                code TEXT PRIMARY KEY, created REAL, used_by INTEGER)""")
            db.execute("CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY)")
            columns = [r[1] for r in db.execute("PRAGMA table_info(users)")]
            if "expires_at" not in columns:  # база от прошлой версии
                db.execute("ALTER TABLE users ADD COLUMN expires_at REAL")
            db.execute("""CREATE TABLE IF NOT EXISTS user_market_settings (
                user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                min_roi REAL,
                min_price REAL NOT NULL,
                max_price REAL NOT NULL,
                min_float REAL,
                max_float REAL,
                PRIMARY KEY (user_id, provider))""")
            db.execute(
                "INSERT OR IGNORE INTO user_market_settings "
                "(user_id, provider, enabled, min_roi, min_price, max_price) "
                "SELECT user_id, 'skinport', 1, min_roi, min_price, max_price FROM users")
            db.execute(
                "INSERT OR IGNORE INTO user_market_settings "
                "(user_id, provider, enabled, min_price, max_price) "
                "SELECT user_id, 'dmarket', 0, ?, ? FROM users",
                (cfg.min_price, cfg.max_price))
            db.execute(
                "UPDATE user_market_settings SET enabled = 0 WHERE provider = 'dmarket'")
            for provider in NEW_MARKETPLACES:
                db.execute(
                    "INSERT OR IGNORE INTO user_market_settings "
                    "(user_id, provider, enabled, min_price, max_price) "
                    "SELECT user_id, ?, 1, ?, ? FROM users",
                    (provider, cfg.min_price, cfg.max_price))
            self._add_user(cfg.admin_id, "admin")

    def _add_user(self, user_id: int, name: str) -> None:
        c = self.cfg
        self.db.execute(
            "INSERT OR IGNORE INTO users (user_id, name, min_roi, min_price, max_price) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, name, c.min_roi, c.min_price, c.max_price))
        self.db.execute(
            "INSERT OR IGNORE INTO user_market_settings "
            "(user_id, provider, enabled, min_roi, min_price, max_price) "
            "VALUES (?, 'skinport', 1, ?, ?, ?)",
            (user_id, c.min_roi, c.min_price, c.max_price))
        self.db.execute(
            "INSERT OR IGNORE INTO user_market_settings "
            "(user_id, provider, enabled, min_price, max_price) "
            "VALUES (?, 'dmarket', 0, ?, ?)",
            (user_id, c.min_price, c.max_price))
        for provider in NEW_MARKETPLACES:
            self.db.execute(
                "INSERT OR IGNORE INTO user_market_settings "
                "(user_id, provider, enabled, min_price, max_price) VALUES (?, ?, 1, ?, ?)",
                (user_id, provider, c.min_price, c.max_price))

    def is_admin(self, user_id: int) -> bool:
        return user_id == self.cfg.admin_id

    def users(self) -> list[User]:
        """Только те, у кого доступ действует сейчас."""
        rows = self.db.execute(
            "SELECT user_id, name, min_roi, min_price, max_price, paused, expires_at "
            "FROM users WHERE expires_at IS NULL OR expires_at > ?", (time.time(),))
        return [User(*r[:5], bool(r[5]), r[6]) for r in rows]

    def get(self, user_id: int) -> User | None:
        return next((u for u in self.users() if u.user_id == user_id), None)

    def create_invite(self) -> str:
        code = secrets.token_urlsafe(16)
        with self.db:
            self.db.execute("INSERT INTO invites (code, created) VALUES (?, ?)",
                            (code, time.time()))
        return code

    def redeem(self, code: str, user_id: int, name: str) -> bool:
        """Ссылка достаётся только первому: дальше она недействительна."""
        if self.get(user_id):
            return False  # у участника ссылка не должна сгорать
        with self.db:
            cur = self.db.execute(
                "UPDATE invites SET used_by = ? WHERE code = ? AND used_by IS NULL "
                "AND created > ?",
                (user_id, code, time.time() - self.cfg.invite_ttl))
            if cur.rowcount != 1:
                return False
            self._add_user(user_id, name)
            # Приглашение даёт бессрочный доступ и снимает блокировку
            self.db.execute("UPDATE users SET expires_at = NULL WHERE user_id = ?", (user_id,))
            self.db.execute("DELETE FROM banned WHERE user_id = ?", (user_id,))
        return True

    def is_banned(self, user_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM banned WHERE user_id = ?", (user_id,)).fetchone() is not None

    def grant(self, user_id: int, name: str, days: int) -> float:
        """Продлевает доступ, но не снимает административную блокировку."""
        if self.is_banned(user_id):
            raise PermissionError("Административная блокировка не снята")
        now = time.time()
        row = self.db.execute(
            "SELECT expires_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row and row[0] is None:
            return float("inf")  # бессрочный доступ не превращаем в срочный
        expires = max(now, row[0] if row else now) + days * 86400
        self._add_user(user_id, name)
        self.db.execute("UPDATE users SET expires_at = ? WHERE user_id = ?", (expires, user_id))
        return expires

    def subscribe(self, user_id: int, days: int) -> float:
        """Ручная выдача подписки администратором. Снимает блокировку."""
        with self.db:
            self.db.execute("DELETE FROM banned WHERE user_id = ?", (user_id,))
            return self.grant(user_id, str(user_id), days)

    def revoke(self, user_id: int) -> bool:
        if not 0 < user_id < 2**53 or self.is_admin(user_id):
            return False
        with self.db:
            cur = self.db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            if cur.rowcount == 1:
                self.db.execute("INSERT OR IGNORE INTO banned VALUES (?)", (user_id,))
        return cur.rowcount == 1

    def set_roi(self, user_id: int, roi: float) -> None:
        with self.db:
            self.db.execute("UPDATE users SET min_roi = ? WHERE user_id = ?", (roi, user_id))
            self.db.execute(
                "UPDATE user_market_settings SET min_roi = ? "
                "WHERE user_id = ? AND provider = 'skinport'", (roi, user_id))

    def set_price(self, user_id: int, low: float, high: float) -> None:
        with self.db:
            self.db.execute(
                "UPDATE users SET min_price = ?, max_price = ? WHERE user_id = ?",
                (low, high, user_id))
            self.db.execute(
                "UPDATE user_market_settings SET min_price = ?, max_price = ? "
                "WHERE user_id = ? AND provider = 'skinport'", (low, high, user_id))

    @staticmethod
    def _check_provider(provider: str) -> None:
        if provider not in SUPPORTED_MARKETPLACES:
            raise ValueError("Unsupported marketplace")

    def market_settings(self, user_id: int, provider: str) -> MarketSettings | None:
        self._check_provider(provider)
        row = self.db.execute(
            "SELECT enabled, min_price, max_price, min_roi, min_float, max_float "
            "FROM user_market_settings WHERE user_id = ? AND provider = ?",
            (user_id, provider)).fetchone()
        return MarketSettings(provider, bool(row[0]), row[1], row[2], row[3], row[4], row[5]) \
            if row else None

    def set_market_enabled(self, user_id: int, provider: str, enabled: bool) -> None:
        self._check_provider(provider)
        if enabled and provider == "dmarket":
            raise ValueError(
                "DMarket отключён: условия площадки ограничивают доступ для пользователей "
                "из России и Беларуси.")
        with self.db:
            self.db.execute(
                "UPDATE user_market_settings SET enabled = ? "
                "WHERE user_id = ? AND provider = ?",
                (int(enabled), user_id, provider))

    def set_market_price(self, user_id: int, provider: str, low: float, high: float) -> None:
        self._check_provider(provider)
        if not math.isfinite(low) or not math.isfinite(high) or low < 0 or low >= high:
            raise ValueError("Invalid marketplace price range")
        with self.db:
            self.db.execute(
                "UPDATE user_market_settings SET min_price = ?, max_price = ? "
                "WHERE user_id = ? AND provider = ?",
                (low, high, user_id, provider))

    def set_market_float(self, user_id: int, provider: str,
                         low: float, high: float) -> None:
        self._check_provider(provider)
        if (provider not in ("dmarket", *NEW_MARKETPLACES) or not math.isfinite(low) or not math.isfinite(high)
                or low < 0 or high > 1 or low >= high):
            raise ValueError("Invalid marketplace float range")
        with self.db:
            self.db.execute(
                "UPDATE user_market_settings SET min_float = ?, max_float = ? "
                "WHERE user_id = ? AND provider = ?",
                (low, high, user_id, provider))

    def clear_market_float(self, user_id: int, provider: str) -> None:
        self._check_provider(provider)
        with self.db:
            self.db.execute(
                "UPDATE user_market_settings SET min_float = NULL, max_float = NULL "
                "WHERE user_id = ? AND provider = ?", (user_id, provider))

    def set_paused(self, user_id: int, paused: bool) -> None:
        with self.db:
            self.db.execute("UPDATE users SET paused = ? WHERE user_id = ?",
                            (int(paused), user_id))
