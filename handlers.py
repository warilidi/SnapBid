"""Команды бота. Всё работает только в личке и только для допущенных ID."""
import logging
import math

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from access import Access
from billing import Billing, date, pay_keyboard
from stars import Stars
from stats import Stats

log = logging.getLogger("handlers")

HELP = """Команды:
/settings — мои фильтры
/status — сколько лотов проверено
/roi 12 — минимальная чистая прибыль, %
/price 5 300 — диапазон цены лота
/pause — остановить сигналы
/resume — включить сигналы"""

ADMIN_HELP = """

Администратору:
/invite — бесплатная одноразовая ссылка (бессрочно)
/sub ID — выдать или продлить подписку
/users — список участников
/revoke ID — убрать участника"""


def numbers(args: str | None, count: int) -> list[float] | None:
    try:
        values = [float(x.replace(",", ".")) for x in (args or "").split()]
    except ValueError:
        return None
    ok = len(values) == count and all(math.isfinite(v) and v >= 0 for v in values)
    return values if ok else None


BUY = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="Купить доступ", callback_data="buy")]])


def build_router(access: Access, billing: Billing | None = None,
                 stats: Stats | None = None, stars: Stars | None = None) -> Router:
    router = Router()
    router.message.filter(F.chat.type == "private", F.from_user)

    async def allowed(m: Message) -> bool:
        return access.get(m.from_user.id) is not None

    async def admin(m: Message) -> bool:
        return access.is_admin(m.from_user.id)

    def help_text(user_id: int) -> str:
        user = access.get(user_id)
        selling = stars or billing or access.cfg.sales_contact
        buy = "\n/buy — продлить доступ" if selling and user and user.expires_at else ""
        return HELP + buy + (ADMIN_HELP if access.is_admin(user_id) else "") + (
            "\n/refund КОД — вернуть звёзды" if stars and access.is_admin(user_id) else "")

    async def send_invoice(user, chat: Message):
        current = access.get(user.id)
        if current and current.expires_at is None:
            return await chat.answer("У вас бессрочный доступ.")
        if access.is_banned(user.id):
            return await chat.answer("Нет доступа.")
        try:
            url = await billing.invoice_url(user.id, user.full_name)
        except Exception as e:
            log.warning("Счёт не создан: %s", e)
            return await chat.answer("Не удалось создать счёт, попробуйте позже.")
        await chat.answer(
            f"{billing.offer}\nСчёт действует 1 час. Доступ откроется после оплаты.",
            reply_markup=pay_keyboard(url))

    def sales_text(user_id: int) -> str:
        c = access.cfg
        return (f"Подписка на {c.sub_days} дн. — {c.sub_price} $.\n"
                f"По поводу покупки пишите {c.sales_contact}\n"
                f"Ваш ID: {user_id} (отправьте его при покупке)")

    if access.cfg.sales_contact and not billing:
        @router.message(Command("buy"))
        async def buy_manual(m: Message):
            uid = m.from_user.id
            user = access.get(uid)
            if user and user.expires_at is None:
                return await m.answer("У вас бессрочный доступ.")
            if access.is_banned(uid):
                return await m.answer("Нет доступа.")
            await m.answer(sales_text(uid))

    if billing:
        @router.message(Command("buy"))
        async def buy(m: Message):
            await send_invoice(m.from_user, m)

        @router.callback_query(F.data == "buy", F.message.chat.type == "private")
        async def buy_button(q: CallbackQuery):
            await q.answer()
            await send_invoice(q.from_user, q.message)

    if stars or billing or access.cfg.sales_contact:
        @router.message(Command("support", "paysupport"))
        async def support(m: Message):
            text = (m.text or "").partition(" ")[2].strip()
            if not text:
                contact = access.cfg.sales_contact
                return await m.answer(
                    "Опишите вопрос после команды: /paysupport текст"
                    + (f"\nИли напишите {contact}" if contact else "")
                    + "\nПоддержка Telegram с покупками в этом боте не помогает.")
            await m.bot.send_message(
                access.cfg.admin_id,
                f"Поддержка от ID {m.from_user.id} ({m.from_user.full_name}):\n{text[:3000]}")
            await m.answer("Сообщение передано администратору.")

    @router.message(CommandStart())
    async def start(m: Message, command: CommandObject):
        uid = m.from_user.id
        if access.get(uid):
            await m.answer(help_text(uid))
        elif command.args and access.redeem(command.args, uid, m.from_user.full_name):
            await m.answer("Доступ открыт.\n\n" + help_text(uid))
            await m.bot.send_message(
                access.cfg.admin_id,
                f"Новый участник: {m.from_user.full_name} (ID {uid})")
        elif stars and not access.is_banned(uid):
            text, keyboard = stars.offer(uid, billing is not None)
            await m.answer(text, reply_markup=keyboard)
        elif billing and not access.is_banned(uid):
            contact = access.cfg.sales_contact
            extra = f"\nПо поводу покупки: {contact}" if contact else ""
            await m.answer(billing.offer + extra, reply_markup=BUY)
        elif access.cfg.sales_contact and not access.is_banned(uid):
            await m.answer(sales_text(uid))
        else:
            await m.answer("Нет доступа.")

    # Всё, что ниже, для посторонних не существует
    @router.message(Command("invite"), admin)
    async def invite(m: Message):
        me = await m.bot.me()
        code = access.create_invite()
        hours = access.cfg.invite_ttl // 3600
        await m.answer(
            f"https://t.me/{me.username}?start={code}\n"
            f"Одноразовая, действует {hours} ч. Доступ получит первый, кто нажмёт Start.")

    @router.message(Command("sub"), admin)
    async def subscribe(m: Message, command: CommandObject):
        arg = (command.args or "").strip()
        if not (arg.isascii() and arg.isdecimal() and 0 < int(arg) < 2**53):
            return await m.answer("Пример: /sub 123456789")
        uid = int(arg)
        expires = access.subscribe(uid, access.cfg.sub_days)
        if expires == float("inf"):
            return await m.answer("У этого ID уже бессрочный доступ.")
        await m.answer(f"ID {uid}: подписка до {date(expires)}")
        try:
            await m.bot.send_message(
                uid, f"Подписка активна до {date(expires)}\nКоманды: /start")
        except Exception as e:
            log.warning("Не удалось уведомить %s: %s", uid, e)
            await m.answer("Уведомить не удалось: пусть участник откроет бота и нажмёт Start.")

    @router.message(Command("users"), admin)
    async def users(m: Message):
        await m.answer("\n".join(
            f"{u.user_id} — {u.name}, "
            f"{'до ' + date(u.expires_at) if u.expires_at else 'бессрочно'}"
            f"{' (пауза)' if u.paused else ''}" for u in access.users())[:4000])

    @router.message(Command("revoke"), admin)
    async def revoke(m: Message, command: CommandObject):
        arg = (command.args or "").strip()
        ok = arg.isdecimal() and access.revoke(int(arg))
        await m.answer("Доступ отозван." if ok else "Не найдено. Пример: /revoke 123456789")

    @router.message(Command("settings"), allowed)
    async def settings(m: Message):
        u = access.get(m.from_user.id)
        await m.answer(
            f"Мин. прибыль: {u.min_roi * 100:g}%\n"
            f"Цена: {u.min_price:g}–{u.max_price:g} {access.cfg.currency}\n"
            f"Сигналы: {'на паузе' if u.paused else 'включены'}\n"
            f"Доступ: {'до ' + date(u.expires_at) if u.expires_at else 'бессрочно'}")

    @router.message(Command("status"), allowed)
    async def status(m: Message):
        await m.answer(stats.text() if stats else "Статистика недоступна.")

    @router.message(Command("roi"), allowed)
    async def roi(m: Message, command: CommandObject):
        v = numbers(command.args, 1)
        if not v or not 1 <= v[0] <= 500:
            return await m.answer("Пример: /roi 12 (от 1 до 500)")
        access.set_roi(m.from_user.id, v[0] / 100)
        await m.answer(f"Минимальная прибыль: {v[0]:g}%")

    @router.message(Command("price"), allowed)
    async def price(m: Message, command: CommandObject):
        v = numbers(command.args, 2)
        if not v or v[0] >= v[1]:
            return await m.answer("Пример: /price 5 300")
        access.set_price(m.from_user.id, *v)
        await m.answer(f"Цена: {v[0]:g}–{v[1]:g} {access.cfg.currency}")

    @router.message(Command("pause", "resume"), allowed)
    async def pause(m: Message, command: CommandObject):
        paused = command.command == "pause"
        access.set_paused(m.from_user.id, paused)
        await m.answer("Сигналы на паузе." if paused else "Сигналы включены.")

    @router.message(allowed)
    async def fallback(m: Message):
        await m.answer(help_text(m.from_user.id))

    return router
