"""Платный доступ: счета Crypto Bot (Crypto Pay API)."""
import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from access import Access
from config import Config

log = logging.getLogger("billing")
INVOICE_TTL = 3600
CHECK_EVERY = 10


def date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


class CryptoPay:
    def __init__(self, token: str, url: str) -> None:
        self.token, self.url = token, url.rstrip("/")

    async def call(self, method: str, params: dict):
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                    f"{self.url}/api/{method}", json=params,
                    headers={"Crypto-Pay-API-Token": self.token}) as resp:
                data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise RuntimeError(f"Crypto Pay {method}: {data.get('error')}")
        return data["result"]

    async def create_invoice(self, **params) -> dict:
        return await self.call("createInvoice", params)

    async def get_invoices(self, ids: list[int]) -> list[dict]:
        result = await self.call("getInvoices", {
            "invoice_ids": ",".join(map(str, ids)), "count": len(ids)})
        # Принимаем оба вида ответа: список или объект с полем items
        return result["items"] if isinstance(result, dict) else result


class Billing:
    def __init__(self, access: Access, api: CryptoPay, cfg: Config) -> None:
        self.access, self.api, self.cfg = access, api, cfg
        self.db = access.db
        self.lock = asyncio.Lock()
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS invoices (
                invoice_id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT,
                days INTEGER, url TEXT, status TEXT, created REAL)""")

    @property
    def offer(self) -> str:
        c = self.cfg
        return f"Доступ на {c.sub_days} дн. — {c.sub_price} {c.sub_asset}"

    async def invoice_url(self, user_id: int, name: str) -> str:
        async with self.lock:  # двойное нажатие не создаёт два счёта
            row = self.db.execute(
                "SELECT url FROM invoices WHERE user_id = ? AND status = 'pending' "
                "AND created > ?", (user_id, time.time() - INVOICE_TTL + 300)).fetchone()
            if row:
                return row[0]
            invoice = await self.api.create_invoice(
                currency_type="crypto", asset=self.cfg.sub_asset,
                amount=self.cfg.sub_price, description=self.offer,
                payload=str(user_id), expires_in=INVOICE_TTL,
                allow_anonymous=False)
            url = invoice["bot_invoice_url"]
            with self.db:
                self.db.execute(
                    "INSERT INTO invoices VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (invoice["invoice_id"], user_id, name, self.cfg.sub_days,
                     url, time.time()))
            return url

    def settle(self, invoice_id: int) -> tuple[int, float | None] | None:
        """Засчитывает оплату ровно один раз. expires=None: ID заблокирован."""
        with self.db:  # статус и доступ меняются одной транзакцией
            cur = self.db.execute(
                "UPDATE invoices SET status = 'paid' "
                "WHERE invoice_id = ? AND status = 'pending'", (invoice_id,))
            if cur.rowcount != 1:
                return None
            user_id, name, days = self.db.execute(
                "SELECT user_id, name, days FROM invoices WHERE invoice_id = ?",
                (invoice_id,)).fetchone()
            return user_id, self.access.grant(user_id, name, days)

    async def check(self, bot: Bot) -> None:
        ids = [r[0] for r in self.db.execute(
            "SELECT invoice_id FROM invoices WHERE status = 'pending' ORDER BY created")]
        for i in range(0, len(ids), 100):
            for invoice in await self.api.get_invoices(ids[i:i + 100]):
                if invoice["status"] == "expired":
                    with self.db:
                        self.db.execute(
                            "UPDATE invoices SET status = 'expired' WHERE invoice_id = ? "
                            "AND status = 'pending'", (invoice["invoice_id"],))
                elif invoice["status"] == "paid":
                    result = self.settle(invoice["invoice_id"])
                    if result:
                        await self.notify(bot, invoice["invoice_id"], *result)

    async def notify(self, bot: Bot, invoice_id: int, user_id: int,
                     expires: float | None) -> None:
        if expires == float("inf"):
            user_text = "Оплата получена, но у вас уже бессрочный доступ. Напишите /paysupport"
            admin_text = f"Лишняя оплата: ID {user_id}, счёт {invoice_id}"
        elif expires is None:
            user_text = "Оплата получена, но доступ для вас закрыт. Напишите /paysupport"
            admin_text = f"Заблокированный ID {user_id} оплатил счёт {invoice_id}: нужен возврат"
        else:
            user_text = f"Оплата получена. Доступ до {date(expires)}\nКоманды: /start"
            admin_text = f"Оплата: ID {user_id}, счёт {invoice_id}"
        for chat, text in ((user_id, user_text), (self.cfg.admin_id, admin_text)):
            try:
                await bot.send_message(chat, text)
            except Exception as e:
                log.warning("Не удалось уведомить %s: %s", chat, e)

    async def run(self, bot: Bot) -> None:
        while True:
            try:
                await self.check(bot)
            except Exception as e:
                log.warning("Не удалось проверить оплаты: %s", e)
            await asyncio.sleep(CHECK_EVERY)


def pay_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Оплатить в Crypto Bot", url=url)]])
