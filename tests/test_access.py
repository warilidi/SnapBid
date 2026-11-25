import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendMessage
from aiogram.types import Chat, Message, Update, User

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

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="b", username="test_bot")
        assert isinstance(method, SendMessage)
        self.sent.append((method.chat_id, method.text))
        return None

    async def stream_content(self, *a, **kw):
        yield b""

    async def close(self):
        pass


def setup(cfg=CFG):
    journal = Journal(":memory:")
    access = Access(journal.db, cfg)
    session = FakeSession()
    bot = Bot("1:test", session=session)
    dp = Dispatcher()
    dp.include_router(build_router(access))

    async def say(uid, text, chat_type="private"):
        session.sent.clear()
        msg = Message(message_id=1, date=datetime.now(), text=text,
                      chat=Chat(id=uid, type=chat_type),
                      from_user=User(id=uid, is_bot=False, first_name=f"u{uid}"))
        await dp.feed_update(bot, Update(update_id=1, message=msg))
        return list(session.sent)

    return journal, access, bot, session, say


def test_invite_first_only_and_permissions():
    async def run():
        _, access, _, _, say = setup()
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
        assert await say(GUEST, f"/start {code}") == [(GUEST, "Нет доступа.")]
        assert await say(ADMIN, "/settings", chat_type="group") == []
    asyncio.run(run())


def test_expired_invite():
    _, access, *_ = setup()
    code = access.create_invite()
    access.db.execute("update invites set created = 0")
    assert not access.redeem(code, GUEST, "g")


def test_settings_commands():
    async def run():
        _, access, _, _, say = setup()
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


def test_personal_filters_delivery_and_dedup():
    async def run():
        journal, access, bot, session, _ = setup()
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
        handle_event({"eventType": "listed", "sales": [sale(saleId=2, salePrice=6000)]},
                     prices, journal, notifier, access, CFG, stats)
        await asyncio.sleep(0.05)
        assert sorted(c for c, _ in session.sent) == [GUEST, ADMIN]
        session.sent.clear()
        prices.updated_at = 0
        handle_event({"eventType": "listed", "sales": [sale(saleId=3)]},
                     prices, journal, notifier, access, CFG, stats)
        await asyncio.sleep(0.05)
        assert session.sent == []
        assert stats.lots == 4 and stats.results["подошли"] == 2
        assert stats.results["повторы"] == 1
        assert stats.results["цены ещё не загружены или устарели"] == 1
        await notifier.close()
    asyncio.run(run())


def test_manual_subscription():
    async def run():
        _, access, _, _, say = setup(Config(admin_id=ADMIN, sub_days=30, stars_price=0))
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
        assert await say(GUEST, "/start") == [(GUEST, "Нет доступа.")]
        await say(ADMIN, f"/sub {GUEST}")
        assert access.get(GUEST)
        for bad in ("/sub", "/sub abc", "/sub -5"):
            assert "Пример" in (await say(ADMIN, bad))[0][1]
    asyncio.run(run())
