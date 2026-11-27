"""Кто имеет доступ к боту и с какими фильтрами."""
import secrets
import sqlite3
import time
from dataclasses import dataclass, replace

from config import Config


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


class Access:
    def __init__(self, db: sqlite3.Connection, cfg: Config) -> None:
        self.db, self.cfg = db, cfg
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
            self._add_user(cfg.admin_id, "admin")

    def _add_user(self, user_id: int, name: str) -> None:
        c = self.cfg
        self.db.execute(
            "INSERT OR IGNORE INTO users (user_id, name, min_roi, min_price, max_price) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, name, c.min_roi, c.min_price, c.max_price))

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

    def set_price(self, user_id: int, low: float, high: float) -> None:
        with self.db:
            self.db.execute(
                "UPDATE users SET min_price = ?, max_price = ? WHERE user_id = ?",
                (low, high, user_id))

    def set_paused(self, user_id: int, paused: bool) -> None:
        with self.db:
            self.db.execute("UPDATE users SET paused = ? WHERE user_id = ?",
                            (int(paused), user_id))
