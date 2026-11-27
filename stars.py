"""Продажа подписки за Telegram Stars (валюта XTR)."""
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, Message, PreCheckoutQuery)

from access import Access
from billing import date

log = logging.getLogger("stars")
FOREVER = float("inf")


class Stars:
    def __init__(self, access: Access) -> None:
        self.access, self.db, self.cfg = access, access.db, access.cfg
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS star_payments (
                charge_id TEXT PRIMARY KEY, user_id INTEGER, amount INTEGER,
                days INTEGER, status TEXT, created REAL)""")

    def refusal(self, user_id: int) -> str | None:
        """Почему этому ID нельзя продавать подписку."""
        if self.access.is_banned(user_id):
            return "Доступ заблокирован. Обратитесь к администратору."
        user = self.access.get(user_id)
        if user and user.expires_at is None:
            return "У вас уже бессрочный доступ."
        return None

    def offer(self, user_id: int, crypto: bool) -> tuple[str, InlineKeyboardMarkup]:
        c = self.cfg
        text = f"Подписка на {c.sub_days} дн.\n⭐ {c.stars_price} Stars: оплата прямо здесь"
        if c.sales_contact:
            text += (f"\nИли {c.sub_price} $: по поводу покупки пишите {c.sales_contact}"
                     f"\nВаш ID: {user_id}")
        text += "\n\nУсловия: /terms\nНажимая кнопку оплаты, вы соглашаетесь с ними."
        rows = [[InlineKeyboardButton(
            text=f"Согласен, оплатить {c.stars_price} ⭐", callback_data="stars")]]
        if crypto:
            rows.append([InlineKeyboardButton(
                text=f"Согласен, оплатить {c.sub_price} {c.sub_asset}", callback_data="buy")])
        rows.append([InlineKeyboardButton(text="Условия подписки", callback_data="menu:terms")])
        rows.append([InlineKeyboardButton(text="В главное меню", callback_data="menu:home")])
        return text, InlineKeyboardMarkup(inline_keyboard=rows)

    def terms(self) -> str:
        c = self.cfg
        return (
            f"Условия подписки\n\n"
            f"1. {c.sub_days} дн. доступа к сигналам о лотах CS2 на Skinport. "
            f"Автопродления нет.\n"
            f"2. Прибыль в сигналах расчётная и не гарантирована. Цена может упасть, "
            f"лот могут купить раньше. Решение о покупке принимаете вы.\n"
            f"3. Бот ничего не покупает и не продаёт за вас.\n"
            f"4. Количество сигналов зависит от рынка и ваших фильтров. "
            f"Возможны перерывы в работе.\n"
            f"5. Вопросы и возвраты: /paysupport. Поддержка Telegram и поддержка "
            f"ботов с покупками в этом боте не помогают.")

    def check(self, user_id: int, payload: str, currency: str, amount: int) -> str | None:
        expected = f"sub:{user_id}:{self.cfg.sub_days}"
        if payload != expected or currency != "XTR" or amount != self.cfg.stars_price:
            return "Счёт устарел. Отправьте /buy и оплатите новый."
        return self.refusal(user_id)

    def settle(self, charge_id: str, user_id: int, name: str, amount: int,
               days: int, grant_access: bool = True) -> float | bool | None:
        """False: duplicate; None: no access granted; inf: existing permanent access."""
        with self.db:  # запись о платеже и доступ меняются вместе
            cur = self.db.execute(
                "INSERT OR IGNORE INTO star_payments VALUES (?, ?, ?, ?, 'paid', ?)",
                (charge_id, user_id, amount, days, time.time()))
            if cur.rowcount != 1:
                return False
            if not grant_access or self.access.is_banned(user_id):
                return None
            return self.access.grant(user_id, name, days)

    def payment(self, charge_id: str) -> tuple[int, int, str] | None:
        return self.db.execute(
            "SELECT user_id, days, status FROM star_payments WHERE charge_id = ?",
            (charge_id,)).fetchone()

    def mark_refunded(self, charge_id: str, take_days: bool = True) -> None:
        with self.db:
            cur = self.db.execute(
                "UPDATE star_payments SET status = 'refunded' "
                "WHERE charge_id = ? AND status = 'paid'", (charge_id,))
            if cur.rowcount == 1 and take_days:
                user_id, days, _ = self.payment(charge_id)
                self.db.execute(
                    "UPDATE users SET expires_at = expires_at - ? "
                    "WHERE user_id = ? AND expires_at IS NOT NULL", (days * 86400, user_id))


def build_stars_router(stars: Stars, crypto: bool = False) -> Router:
    """Подключать раньше основного роутера, иначе платёж перехватит справка."""
    router = Router()
    router.message.filter(F.chat.type == "private", F.from_user)
    cfg = stars.cfg

    @router.message(F.successful_payment)
    async def paid(m: Message):
        p, uid = m.successful_payment, m.from_user.id
        charge = p.telegram_payment_charge_id
        error = stars.check(uid, p.invoice_payload, p.currency, p.total_amount)
        try:
            prefix, payload_uid, payload_days = p.invoice_payload.split(":")
            days = int(payload_days) if prefix == "sub" and int(payload_uid) == uid else 0
        except (AttributeError, TypeError, ValueError):
            days = 0
        result = stars.settle(
            charge, uid, m.from_user.full_name, p.total_amount, days,
            grant_access=error is None and days > 0)
        if result is False:
            return
        if result is None or result == FOREVER:
            try:
                await m.bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=charge)
                stars.mark_refunded(charge, take_days=False)
                note = ("Подписка недоступна, звёзды возвращены."
                        if result is None else
                        "Подписка не нужна, звёзды возвращены.")
            except Exception as e:
                log.error("Возврат %s не прошёл: %s", charge, e)
                note = "Подписка не выдана. Администратор проверит возврат."
            reason = f" Причина: {error}" if error else ""
            admin_note = f"Платёж ID {uid} без выдачи доступа.{reason} {note}\n/refund {charge}"
        else:
            note = f"Оплата получена. Доступ до {date(result)}\nКоманды: /start"
            admin_note = (f"⭐ {p.total_amount} от ID {uid} ({m.from_user.full_name})\n"
                          f"Возврат: /refund {charge}")
        await m.answer(note)
        await m.bot.send_message(cfg.admin_id, admin_note)

    @router.pre_checkout_query()
    async def pre_checkout(q: PreCheckoutQuery):
        # Telegram ждёт ответ не дольше 10 секунд
        error = stars.check(q.from_user.id, q.invoice_payload, q.currency, q.total_amount)
        await q.answer(ok=error is None, error_message=error)

    @router.message(Command("terms"))
    async def terms(m: Message):
        await m.answer(stars.terms())

    @router.callback_query(F.data == "menu:terms", F.message.chat.type == "private")
    async def terms_button(q: CallbackQuery):
        await q.answer()
        await q.bot.send_message(
            q.from_user.id, stars.terms(),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="К покупке", callback_data="menu:buy"),
                InlineKeyboardButton(text="В главное меню", callback_data="menu:home"),
            ]]))

    @router.message(Command("buy"))
    async def buy(m: Message):
        error = stars.refusal(m.from_user.id)
        if error:
            return await m.answer(error)
        text, keyboard = stars.offer(m.from_user.id, crypto)
        await m.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data == "stars", F.message.chat.type == "private")
    async def invoice(q: CallbackQuery):
        uid = q.from_user.id
        error = stars.refusal(uid)
        if error:
            await q.answer(error, show_alert=True)
            return
        await q.answer()
        await q.bot.send_invoice(
            chat_id=uid, title=f"Подписка на {cfg.sub_days} дн.",
            description=f"Сигналы о лотах на {cfg.sub_days} дн. Прибыль не гарантирована.",
            payload=f"sub:{uid}:{cfg.sub_days}", provider_token="", currency="XTR",
            prices=[LabeledPrice(label=f"{cfg.sub_days} дн.", amount=cfg.stars_price)])

    async def admin(m: Message) -> bool:
        return stars.access.is_admin(m.from_user.id)

    @router.message(Command("refund"), admin)
    async def refund(m: Message, command: CommandObject):
        charge = (command.args or "").strip()
        row = stars.payment(charge)
        if not row or row[2] != "paid":
            return await m.answer("Платёж не найден или уже возвращён.")
        try:
            await m.bot.refund_star_payment(user_id=row[0], telegram_payment_charge_id=charge)
        except Exception as e:
            return await m.answer(f"Возврат не прошёл: {e}")
        stars.mark_refunded(charge)
        await m.answer(f"Звёзды возвращены ID {row[0]}, оплаченные дни списаны.")

    return router
