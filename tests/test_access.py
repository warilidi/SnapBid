import asyncio
import sqlite3
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from access import Access
from config import Config
from handlers import build_router
from journal import Journal
from main import handle_event
from notifier import Notifier
from stats import Stats
from test_bot import book, sale

ADMIN, GUEST, STRANGER = 1354713977, 222, 333
CFG = Config(admin_id=ADMIN, sub_days=30, sales_contact="", stars_price=0)


class FakeSession(BaseSession):
    """Подменяет Telegram: запоминает исходящие сообщения."""
    def __init__(self):
        super().__init__()
        self.sent = []
        self.markups = []
        self.answers = []

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="b", username="test_bot")
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method.text)
            return True
        assert isinstance(method, SendMessage)
        self.sent.append((method.chat_id, method.text))
        self.markups.append(method.reply_markup)
        return None

    async def stream_content(self, *a, **kw):
        yield b""

    async def close(self):
        pass


def setup(cfg=CFG, stats=None):
    journal = Journal(":memory:")
    access = Access(journal.db, cfg)
    session = FakeSession()
    bot = Bot("1:test", session=session)
    dp = Dispatcher()
    dp.include_router(build_router(access, stats=stats))

    async def say(uid, text, chat_type="private"):
        session.sent.clear()
        msg = Message(message_id=1, date=datetime.now(), text=text,
                      chat=Chat(id=uid, type=chat_type),
                      from_user=User(id=uid, is_bot=False, first_name=f"u{uid}"))
        await dp.feed_update(bot, Update(update_id=1, message=msg))
        return list(session.sent)

    async def tap(uid, data, with_message=True):
        session.sent.clear()
        user = User(id=uid, is_bot=False, first_name=f"u{uid}")
        bot_user = User(id=1, is_bot=True, first_name="b", username="test_bot")
        message = Message(
            message_id=1, date=datetime.now(), text="old menu",
            chat=Chat(id=uid, type="private"), from_user=bot_user).as_(bot)
        query = CallbackQuery(
            id="callback", from_user=user, chat_instance="c", data=data,
            message=message if with_message else None)
        await dp.feed_update(bot, Update(update_id=2, callback_query=query))
        return list(session.sent)

    return journal, access, bot, session, say, tap


def test_invite_first_only_and_permissions():
    async def run():
        _, access, _, _, say, _ = setup()
        assert await say(STRANGER, "/start") == [(STRANGER, "Нет доступа.")]
        for cmd in ("/invite", "/users", "/settings", "/roi 5", "hi"):
            assert await say(STRANGER, cmd) == []
        link = (await say(ADMIN, "/invite"))[0][1].split()[0]
        code = link.split("start=")[1]
        await say(ADMIN, f"/start {code}")  # у админа ссылка не сгорает
        assert "Доступ открыт" in (await say(GUEST, f"/start {code}"))[0][1]
        assert await say(STRANGER, f"/start {code}") == [(STRANGER, "Нет доступа.")]
        assert {u.user_id for u in access.users()} == {ADMIN, GUEST}
        assert "/invite" not in (await say(GUEST, "/start"))[0][1]
        await say(GUEST, "/invite")
        assert access.db.execute("select count(*) from invites").fetchone()[0] == 1
        await say(GUEST, f"/revoke {ADMIN}")
        await say(ADMIN, f"/revoke {ADMIN}")
        assert access.get(ADMIN)
        await say(ADMIN, f"/revoke {GUEST}")
        assert access.get(GUEST) is None
        assert "Доступ заблокирован" in (await say(GUEST, f"/start {code}"))[0][1]
        assert await say(ADMIN, "/settings", chat_type="group") == []
    asyncio.run(run())


def test_expired_invite():
    _, access, *_ = setup()
    code = access.create_invite()
    access.db.execute("update invites set created = 0")
    assert not access.redeem(code, GUEST, "g")


def test_payment_grant_does_not_remove_admin_block():
    _, access, *_ = setup()
    assert access.redeem(access.create_invite(), GUEST, "guest")
    assert access.revoke(GUEST)
    with pytest.raises(PermissionError):
        access.grant(GUEST, "guest", 30)
    assert access.is_banned(GUEST) and access.get(GUEST) is None
    assert access.subscribe(GUEST, 30) > 0
    assert not access.is_banned(GUEST) and access.get(GUEST) is not None


def test_settings_commands():
    async def run():
        _, access, _, _, say, _ = setup()
        await say(ADMIN, "/roi 12,5")
        await say(ADMIN, "/price 10 50")
        await say(ADMIN, "/pause")
        u = access.get(ADMIN)
        assert (u.min_roi, u.min_price, u.max_price, u.paused) == (0.125, 10, 50, True)
        for bad in ("/roi nan", "/roi -1", "/price 50 10", "/price x y"):
            await say(ADMIN, bad)
        assert access.get(ADMIN) == u
        assert "12.5%" in (await say(ADMIN, "/settings"))[0][1]
    asyncio.run(run())


def test_marketplace_settings_migrate_and_remain_per_user():
    cfg = Config(admin_id=ADMIN, dmarket_api_key="configured",
                 dmarket_secret_key="configured")
    _, access, *_ = setup(cfg)
    assert access.redeem(access.create_invite(), GUEST, "guest")

    assert access.market_settings(ADMIN, "skinport").enabled
    assert access.market_settings(GUEST, "skinport").enabled
    assert not access.market_settings(ADMIN, "dmarket").enabled
    assert not access.market_settings(GUEST, "dmarket").enabled

    access.set_market_enabled(ADMIN, "dmarket", True)
    access.set_market_price(ADMIN, "dmarket", 20, 40)
    access.set_market_float(ADMIN, "dmarket", 0.1, 0.2)
    access.set_roi(ADMIN, 0.25)
    access.set_price(ADMIN, 10, 100)

    assert access.market_settings(ADMIN, "dmarket").enabled
    assert (access.market_settings(ADMIN, "dmarket").min_price,
            access.market_settings(ADMIN, "dmarket").max_price,
            access.market_settings(ADMIN, "dmarket").min_float,
            access.market_settings(ADMIN, "dmarket").max_float) == (20, 40, 0.1, 0.2)
    assert not access.market_settings(GUEST, "dmarket").enabled
    assert (access.market_settings(GUEST, "dmarket").min_price,
            access.market_settings(GUEST, "dmarket").max_price) == (5, 300)
    assert (access.market_settings(ADMIN, "skinport").min_roi,
            access.market_settings(ADMIN, "skinport").min_price,
            access.market_settings(ADMIN, "skinport").max_price) == (0.25, 10, 100)


def test_existing_skinport_user_filters_are_preserved_by_market_migration():
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE users (
        user_id INTEGER PRIMARY KEY, name TEXT, min_roi REAL,
        min_price REAL, max_price REAL, paused INTEGER DEFAULT 0)""")
    db.execute(
        "INSERT INTO users (user_id, name, min_roi, min_price, max_price) "
        "VALUES (?, 'admin', 0.27, 11, 77)", (ADMIN,))

    access = Access(db, Config(admin_id=ADMIN))
    settings = access.market_settings(ADMIN, "skinport")
    assert settings.enabled
    assert (settings.min_roi, settings.min_price, settings.max_price) == (0.27, 11, 77)


def test_marketplace_inline_controls_and_dmarket_filters():
    async def run():
        cfg = Config(admin_id=ADMIN, dmarket_api_key="configured",
                     dmarket_secret_key="configured")
        _, access, _, session, say, tap = setup(cfg)
        assert access.redeem(access.create_invite(), GUEST, "guest")

        await tap(GUEST, "markets:open")
        assert "Skinport: включён" in session.sent[-1][1]
        assert any(
            button.callback_data == "markets:toggle:dmarket"
            for row in session.markups[-1].inline_keyboard for button in row)

        await tap(GUEST, "markets:toggle:dmarket")
        assert access.market_settings(GUEST, "dmarket").enabled
        await say(GUEST, "/dmarket_price 12 50")
        await say(GUEST, "/dmarket_float 0.1 0.2")
        settings = access.market_settings(GUEST, "dmarket")
        assert (settings.min_price, settings.max_price) == (12, 50)
        assert (settings.min_float, settings.max_float) == (0.1, 0.2)
        for invalid in ("/dmarket_price 20 10", "/dmarket_float 0.5 1.1"):
            await say(GUEST, invalid)
        assert access.market_settings(GUEST, "dmarket") == settings

    asyncio.run(run())


def test_marketplace_callback_explains_missing_credentials():
    async def run():
        _, access, _, session, _, tap = setup()
        await tap(ADMIN, "markets:toggle:dmarket")
        assert "DMARKET_API_KEY" in session.answers[-1]
        assert not access.market_settings(ADMIN, "dmarket").enabled

    asyncio.run(run())


def test_status_command_includes_process_uptime():
    async def run():
        stats = Stats()
        _, _, _, _, say, _ = setup(stats=stats)
        response = await say(ADMIN, "/status")
        assert "Время работы процесса:" in response[0][1]
        assert "Лотов получено с запуска: 0" in response[0][1]

    asyncio.run(run())


def test_personal_filters_delivery_and_dedup():
    async def run():
        journal, access, bot, session, _, _ = setup()
        assert access.redeem(access.create_invite(), GUEST, "g")
        access.set_roi(GUEST, 0.30)
        notifier = Notifier(bot, access, pause=0)
        stats = Stats()
        prices = book()
        event = {"eventType": "listed", "sales": [sale(), sale()]}  # ROI 15%
        handle_event(event, prices, journal, notifier, access, CFG, stats)
        await asyncio.sleep(0.05)
        assert [c for c, _ in session.sent] == [ADMIN]
        session.sent.clear()

        access.set_roi(GUEST, 0.10)
        handle_event({"eventType": "listed", "sales": [sale()]},
                     prices, journal, notifier, access, CFG, stats)
        await asyncio.sleep(0.05)
        assert [c for c, _ in session.sent] == [GUEST]
        assert journal.db.execute(
            "SELECT COUNT(*) FROM signal_deliveries WHERE user_id = ?", (ADMIN,)
        ).fetchone()[0] == 1
        assert journal.db.execute(
            "SELECT COUNT(*) FROM signal_deliveries WHERE user_id = ?", (GUEST,)
        ).fetchone()[0] == 1
        session.sent.clear()

        access.set_roi(GUEST, 0.60)
        handle_event({"eventType": "listed", "sales": [sale(saleId=2, salePrice=6000)]},
                     prices, journal, notifier, access, CFG, stats)
        await asyncio.sleep(0.05)
        assert [c for c, _ in session.sent] == [ADMIN]
        session.sent.clear()

        access.set_roi(GUEST, 0.30)
        handle_event({"eventType": "listed", "sales": [sale(saleId=2, salePrice=6000)]},
                     prices, journal, notifier, access, CFG, stats)
        await asyncio.sleep(0.05)
        assert [c for c, _ in session.sent] == [GUEST]
        session.sent.clear()

        prices.updated_at = 0
        handle_event({"eventType": "listed", "sales": [sale(saleId=3)]},
                     prices, journal, notifier, access, CFG, stats)
        await asyncio.sleep(0.05)
        assert session.sent == []
        assert stats.lots == 6 and stats.results["подошли"] == 2
        assert stats.results["повторы"] == 3
        assert stats.results["цены ещё не загружены или устарели"] == 1
        await notifier.close()
    asyncio.run(run())


def test_manual_subscription():
    async def run():
        _, access, _, _, say, _ = setup(Config(admin_id=ADMIN, sub_days=30, stars_price=0))
        for cmd in ("/start", "/buy"):
            text = (await say(GUEST, cmd))[0][1]
            assert "6 $" in text and "@warilidi" in text and str(GUEST) in text
        assert await say(GUEST, f"/sub {GUEST}") == []
        assert access.get(GUEST) is None
        replies = await say(ADMIN, f"/sub {GUEST}")
        assert [c for c, _ in replies] == [ADMIN, GUEST]
        first = access.get(GUEST).expires_at
        await say(ADMIN, f"/sub {GUEST}")
        assert access.get(GUEST).expires_at == first + 30 * 86400
        await say(ADMIN, f"/revoke {GUEST}")
        for cmd in ("/start", "/buy"):
            text = (await say(GUEST, cmd))[0][1]
            assert "Доступ заблокирован" in text
        await say(ADMIN, f"/sub {GUEST}")
        assert access.get(GUEST)
        for bad in ("/sub", "/sub abc", "/sub -5"):
            assert "Пример" in (await say(ADMIN, bad))[0][1]
        for bad in ("/revoke 999999999999999999999999999", "/revoke １２３"):
            assert "Не найдено" in (await say(ADMIN, bad))[0][1]
    asyncio.run(run())


def test_inline_navigation_authorization_and_repeated_actions():
    async def run():
        _, access, _, session, say, tap = setup()
        assert access.redeem(access.create_invite(), GUEST, "guest")

        await say(GUEST, "/start")
        assert {button.callback_data for row in session.markups[-1].inline_keyboard
                for button in row} == {"menu:status", "menu:settings"}

        await tap(STRANGER, "menu:settings")
        assert session.sent == []
        assert session.answers[-1] == "Доступ закрыт."
        await tap(STRANGER, "admin:users")
        assert session.sent == []
        assert session.answers[-1] == "Недостаточно прав."

        await tap(GUEST, "menu:settings")
        assert "Сигналы: включены" in session.sent[-1][1]
        await tap(GUEST, "menu:status")
        assert session.sent[-1][1] == "Статистика недоступна."
        await tap(GUEST, "settings:pause")
        assert access.get(GUEST).paused
        await tap(GUEST, "settings:pause")
        assert access.get(GUEST).paused
        await tap(GUEST, "settings:resume")
        assert not access.get(GUEST).paused

        await tap(ADMIN, "menu:admin")
        assert {button.callback_data for row in session.markups[-1].inline_keyboard
                for button in row} >= {"admin:users", "admin:invite", "admin:usage:revoke"}
        invite_count = access.db.execute("SELECT COUNT(*) FROM invites").fetchone()[0]
        await tap(ADMIN, "admin:invite")
        assert "https://t.me/test_bot?start=" in session.sent[-1][1]
        assert access.db.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == invite_count + 1
        invite_count += 1
        await tap(GUEST, "admin:invite")
        assert access.db.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == invite_count
        await tap(ADMIN, "admin:usage:refund")
        assert session.answers[-1] == "Возврат Stars сейчас недоступен."

        access.revoke(GUEST)
        await tap(GUEST, "settings:pause")
        assert access.is_banned(GUEST) and session.sent == []
        await tap(STRANGER, "not-a-real-callback", with_message=False)
        assert session.sent == []
        assert session.answers[-1] == "Кнопка устарела. Нажмите /start."
    asyncio.run(run())
