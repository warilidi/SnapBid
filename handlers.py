"""Команды бота. Всё работает только в личке и только для допущенных ID."""
import logging
import math

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from access import Access, User
from billing import Billing, date, pay_keyboard
from keyboards import (
    admin_menu, back_to_menu, main_menu, marketplaces_menu, settings_menu,
)
from stars import Stars
from stats import Stats

log = logging.getLogger("handlers")

HELP = """Команды:
/settings — мои фильтры
/status — время работы и статистика
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

    def home_keyboard(user_id: int) -> InlineKeyboardMarkup:
        user = access.get(user_id)
        can_sell = bool(stars or billing or access.cfg.sales_contact)
        can_buy = can_sell and not access.is_banned(user_id) and not (
            user and user.expires_at is None)
        return main_menu(bool(user), can_buy, access.is_admin(user_id))

    def settings_text(u: User) -> str:
        return (
            f"Мин. прибыль: {u.min_roi * 100:g}%\n"
            f"Цена: {u.min_price:g}–{u.max_price:g} {access.cfg.currency}\n"
            f"Сигналы: {'на паузе' if u.paused else 'включены'}\n"
            f"Доступ: {'до ' + date(u.expires_at) if u.expires_at else 'бессрочно'}\n"
            "Фильтры ROI и цены меняются командами /roi и /price.")

    def marketplaces_text(user_id: int) -> str:
        skinport = access.market_settings(user_id, "skinport")
        return (
            "Выбор площадок действует только для вашего аккаунта.\n"
            f"Skinport: {'включён' if skinport and skinport.enabled else 'выключен'}; "
            f"фильтры /roi и /price.\n"
            "DMarket отключён: условия площадки ограничивают доступ для пользователей "
            "из России и Беларуси.\n"
            "Доступность Skinport для оператора из России и регион Wispbyte не подтверждены.")

    def purchase_text(user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
        if access.is_banned(user_id):
            return "Доступ заблокирован. Обратитесь к администратору.", back_to_menu()
        user = access.get(user_id)
        if user and user.expires_at is None:
            return "У вас бессрочный доступ.", back_to_menu()
        if stars:
            return stars.offer(user_id, billing is not None)
        if billing:
            extra = (f"\nПо поводу покупки пишите {access.cfg.sales_contact}"
                     if access.cfg.sales_contact else "")
            return billing.offer + extra, InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Согласен, оплатить", callback_data="buy")],
                [InlineKeyboardButton(text="В главное меню", callback_data="menu:home")],
            ])
        if access.cfg.sales_contact:
            return sales_text(user_id), back_to_menu()
        return "Покупка сейчас недоступна.", back_to_menu()

    async def send_invoice(user, chat: Message):
        if access.is_banned(user.id):
            return await chat.answer("Доступ заблокирован. Обратитесь к администратору.")
        current = access.get(user.id)
        if current and current.expires_at is None:
            return await chat.answer("У вас бессрочный доступ.")
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
            if access.is_banned(uid):
                return await m.answer("Доступ заблокирован. Обратитесь к администратору.")
            user = access.get(uid)
            if user and user.expires_at is None:
                return await m.answer("У вас бессрочный доступ.")
            await m.answer(sales_text(uid), reply_markup=back_to_menu())

    if billing:
        @router.message(Command("buy"))
        async def buy(m: Message):
            await send_invoice(m.from_user, m)

        @router.callback_query(F.data == "buy")
        async def buy_button(q: CallbackQuery):
            if not isinstance(q.message, Message) or q.message.chat.type != "private":
                return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
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
            await m.answer(help_text(uid), reply_markup=home_keyboard(uid))
        elif command.args and access.redeem(command.args, uid, m.from_user.full_name):
            await m.answer("Доступ открыт.\n\n" + help_text(uid),
                           reply_markup=home_keyboard(uid))
            await m.bot.send_message(
                access.cfg.admin_id,
                f"Новый участник: {m.from_user.full_name} (ID {uid})")
        elif access.is_banned(uid):
            await m.answer("Доступ заблокирован. Обратитесь к администратору.")
        elif stars:
            text, keyboard = purchase_text(uid)
            await m.answer(text, reply_markup=keyboard)
        elif billing:
            text, keyboard = purchase_text(uid)
            await m.answer(text, reply_markup=keyboard)
        elif access.cfg.sales_contact:
            text, keyboard = purchase_text(uid)
            await m.answer(text, reply_markup=keyboard)
        else:
            await m.answer("Нет доступа.")

    @router.callback_query(F.data == "menu:home")
    async def menu_home(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        uid = q.from_user.id
        if access.is_banned(uid):
            await q.answer("Доступ заблокирован. Обратитесь к администратору.", show_alert=True)
            return
        await q.answer()
        if access.get(uid):
            text = help_text(uid)
        else:
            text, _ = purchase_text(uid)
        await q.bot.send_message(uid, text, reply_markup=home_keyboard(uid))

    @router.callback_query(F.data == "menu:buy")
    async def menu_buy(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        uid = q.from_user.id
        if access.is_banned(uid):
            return await q.answer("Доступ заблокирован. Обратитесь к администратору.",
                                  show_alert=True)
        await q.answer()
        text, keyboard = purchase_text(uid)
        await q.bot.send_message(uid, text, reply_markup=keyboard)

    @router.callback_query(F.data == "menu:status")
    async def menu_status(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        if not access.get(q.from_user.id):
            return await q.answer("Доступ закрыт.", show_alert=True)
        await q.answer()
        await q.bot.send_message(
            q.from_user.id, stats.text() if stats else "Статистика недоступна.",
            reply_markup=home_keyboard(q.from_user.id))

    @router.callback_query(F.data == "menu:settings")
    async def menu_settings(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        user = access.get(q.from_user.id)
        if user is None:
            return await q.answer("Доступ закрыт.", show_alert=True)
        await q.answer()
        await q.bot.send_message(
            q.from_user.id, settings_text(user),
            reply_markup=settings_menu(user.paused))

    @router.callback_query(F.data == "markets:open")
    async def menu_markets(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        user = access.get(q.from_user.id)
        if user is None:
            return await q.answer("Доступ закрыт.", show_alert=True)
        skinport = access.market_settings(user.user_id, "skinport")
        await q.answer()
        await q.bot.send_message(
            user.user_id, marketplaces_text(user.user_id),
            reply_markup=marketplaces_menu(bool(skinport and skinport.enabled)))

    @router.callback_query(F.data.in_({
        "markets:toggle:skinport", "markets:toggle:dmarket"}))
    async def toggle_market(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        user = access.get(q.from_user.id)
        if user is None:
            return await q.answer("Доступ закрыт.", show_alert=True)
        provider = q.data.rsplit(":", 1)[1]
        settings = access.market_settings(user.user_id, provider)
        if settings is None:
            return await q.answer("Настройки площадки недоступны.", show_alert=True)
        try:
            access.set_market_enabled(user.user_id, provider, not settings.enabled)
        except ValueError as exc:
            return await q.answer(str(exc), show_alert=True)
        await q.answer("Настройки площадки обновлены.")
        skinport = access.market_settings(user.user_id, "skinport")
        await q.bot.send_message(
            user.user_id, marketplaces_text(user.user_id),
            reply_markup=marketplaces_menu(bool(skinport and skinport.enabled)))

    @router.callback_query(F.data == "markets:filters:dmarket")
    async def menu_dmarket_filters(q: CallbackQuery):
        return await q.answer(
            "DMarket отключён для пользователей из России и Беларуси.",
            show_alert=True)

    @router.callback_query(F.data.in_({"settings:pause", "settings:resume"}))
    async def menu_pause(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        user = access.get(q.from_user.id)
        if user is None:
            return await q.answer("Доступ закрыт.", show_alert=True)
        paused = q.data == "settings:pause"
        access.set_paused(q.from_user.id, paused)
        user = access.get(q.from_user.id)
        if user is None:
            return await q.answer("Доступ истёк.", show_alert=True)
        await q.answer("Сигналы поставлены на паузу." if paused else "Сигналы включены.")
        await q.bot.send_message(
            q.from_user.id, settings_text(user), reply_markup=settings_menu(user.paused))

    @router.callback_query(F.data == "menu:admin")
    async def menu_admin(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        if not access.is_admin(q.from_user.id):
            return await q.answer("Недостаточно прав.", show_alert=True)
        await q.answer()
        await q.bot.send_message(
            q.from_user.id, "Администрирование доступа:",
            reply_markup=admin_menu(bool(stars)))

    @router.callback_query(F.data == "admin:users")
    async def menu_users(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        if not access.is_admin(q.from_user.id):
            return await q.answer("Недостаточно прав.", show_alert=True)
        await q.answer()
        text = "\n".join(
            f"{u.user_id} — {u.name}, "
            f"{'до ' + date(u.expires_at) if u.expires_at else 'бессрочно'}"
            f"{' (пауза)' if u.paused else ''}" for u in access.users())[:4000]
        await q.bot.send_message(q.from_user.id, text or "Активных участников нет.",
                                 reply_markup=back_to_menu())

    @router.callback_query(F.data == "admin:invite")
    async def menu_invite(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        if not access.is_admin(q.from_user.id):
            return await q.answer("Недостаточно прав.", show_alert=True)
        await q.answer()
        me = await q.bot.me()
        code = access.create_invite()
        hours = access.cfg.invite_ttl // 3600
        await q.bot.send_message(
            q.from_user.id,
            f"https://t.me/{me.username}?start={code}\n"
            f"Одноразовая, действует {hours} ч. Доступ получит первый, кто нажмёт Start.",
            reply_markup=back_to_menu())

    @router.callback_query(F.data.in_({
        "admin:usage:sub", "admin:usage:revoke", "admin:usage:refund"}))
    async def menu_admin_usage(q: CallbackQuery):
        if not isinstance(q.message, Message) or q.message.chat.type != "private":
            return await q.answer("Сообщение устарело. Нажмите /start.", show_alert=True)
        if not access.is_admin(q.from_user.id):
            return await q.answer("Недостаточно прав.", show_alert=True)
        if q.data == "admin:usage:refund" and not stars:
            return await q.answer("Возврат Stars сейчас недоступен.", show_alert=True)
        messages = {
            "admin:usage:sub": "Выдать или продлить доступ: /sub ID",
            "admin:usage:revoke": "Отозвать доступ: /revoke ID",
            "admin:usage:refund": "Вернуть Stars и списать дни: /refund КОД",
        }
        await q.answer()
        await q.bot.send_message(
            q.from_user.id, messages[q.data], reply_markup=back_to_menu())

    @router.callback_query()
    async def stale_callback(q: CallbackQuery):
        await q.answer("Кнопка устарела. Нажмите /start.", show_alert=True)

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
            f"{' (пауза)' if u.paused else ''}" for u in access.users())[:4000],
                       reply_markup=back_to_menu())

    @router.message(Command("revoke"), admin)
    async def revoke(m: Message, command: CommandObject):
        arg = (command.args or "").strip()
        ok = (arg.isascii() and arg.isdecimal() and 0 < int(arg) < 2**53
              and access.revoke(int(arg)))
        await m.answer("Доступ отозван." if ok else "Не найдено. Пример: /revoke 123456789")

    @router.message(Command("settings"), allowed)
    async def settings(m: Message):
        u = access.get(m.from_user.id)
        if u is None:
            return await m.answer("Доступ закрыт.")
        await m.answer(settings_text(u), reply_markup=settings_menu(u.paused))

    @router.message(Command("status"), allowed)
    async def status(m: Message):
        await m.answer(stats.text() if stats else "Статистика недоступна.",
                       reply_markup=home_keyboard(m.from_user.id))

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
        user = access.get(m.from_user.id)
        if user is None:
            return await m.answer("Доступ закрыт.")
        await m.answer("Сигналы на паузе." if paused else "Сигналы включены.",
                       reply_markup=settings_menu(user.paused))

    @router.message(allowed)
    async def fallback(m: Message):
        await m.answer(help_text(m.from_user.id), reply_markup=home_keyboard(m.from_user.id))

    return router
