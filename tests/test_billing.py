import asyncio
import sqlite3
import time

from aiohttp import web

from access import Access
from billing import Billing, CryptoPay
from config import Config
from test_access import ADMIN, GUEST

CFG = Config(admin_id=ADMIN, sub_days=30, crypto_token="t")


class FakeApi:
    def __init__(self):
        self.status, self.created = {}, []

    async def create_invoice(self, **p):
        self.created.append(p)
        i = len(self.created)
        self.status[i] = "active"
        return {"invoice_id": i, "bot_invoice_url": f"https://t.me/CryptoBot?start={i}"}

    async def get_invoices(self, ids):
        return [{"invoice_id": i, "status": self.status[i]} for i in ids]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))


def setup():
    access = Access(sqlite3.connect(":memory:"), CFG)
    api = FakeApi()
    return access, api, Billing(access, api, CFG), FakeBot()


def test_pay_once_extend_expire():
    async def run():
        access, api, billing, bot = setup()
        urls = await asyncio.gather(*(billing.invoice_url(GUEST, "g") for _ in range(2)))
        assert urls[0] == urls[1] and len(api.created) == 1
        await billing.check(bot)
        assert access.get(GUEST) is None
        api.status[1] = "paid"
        await billing.check(bot)
        first = access.get(GUEST).expires_at
        assert abs(first - time.time() - 30 * 86400) < 5
        assert billing.settle(1) is None
        assert access.get(GUEST).expires_at == first and len(bot.sent) == 2
        access.set_roi(GUEST, 0.3)
        await billing.invoice_url(GUEST, "g")
        api.status[2] = "paid"
        await billing.check(bot)
        user = access.get(GUEST)
        assert user.expires_at == first + 30 * 86400 and user.min_roi == 0.3
        access.db.execute("update users set expires_at = 1 where user_id = ?", (GUEST,))
        assert access.get(GUEST) is None
        await billing.invoice_url(GUEST, "g")
        api.status[3] = "paid"
        await billing.check(bot)
        assert access.get(GUEST).min_roi == 0.3
        assert access.get(GUEST).expires_at < time.time() + 31 * 86400
    asyncio.run(run())


def test_expired_revoked_and_permanent():
    async def run():
        access, api, billing, bot = setup()
        await billing.invoice_url(GUEST, "g")
        api.status[1] = "expired"
        await billing.check(bot)
        assert access.get(GUEST) is None
        await billing.invoice_url(GUEST, "g")  # новый счёт вместо истёкшего
        assert access.redeem(access.create_invite(), GUEST, "g")
        api.status[2] = "paid"
        await billing.check(bot)
        assert access.get(GUEST).expires_at is None
        assert "бессрочный" in bot.sent[0][1]
        access.revoke(GUEST)
        await billing.invoice_url(GUEST, "g")
        api.status[3] = "paid"
        await billing.check(bot)
        assert access.get(GUEST) is not None
        assert not access.is_banned(GUEST)
        assert "Оплата получена" in bot.sent[-2][1]
    asyncio.run(run())


def test_old_database_is_migrated():
    db = sqlite3.connect(":memory:")
    db.execute("""CREATE TABLE users (user_id INTEGER PRIMARY KEY, name TEXT, min_roi REAL,
        min_price REAL, max_price REAL, paused INTEGER DEFAULT 0)""")
    db.execute("INSERT INTO users VALUES (222, 'g', 0.2, 1, 2, 0)")
    assert Access(db, CFG).get(222).expires_at is None


def test_http_client():
    async def run():
        async def handler(request):
            assert request.headers["Crypto-Pay-API-Token"] == "t"
            if request.path.endswith("createInvoice"):
                return web.json_response({"ok": True, "result": await request.json()})
            if (await request.json())["invoice_ids"] == "0":
                return web.json_response({"ok": False, "error": "x"}, status=400)
            return web.json_response({"ok": True, "result": {"items": [{"invoice_id": 1}]}})
        app = web.Application()
        app.router.add_post("/api/{m}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 8766).start()
        api = CryptoPay("t", "http://127.0.0.1:8766")
        assert await api.create_invoice(amount="10") == {"amount": "10"}
        assert await api.get_invoices([1]) == [{"invoice_id": 1}]
        try:
            await api.get_invoices([0])
            raise AssertionError("ожидалась ошибка")
        except RuntimeError:
            pass
        await runner.cleanup()
    asyncio.run(run())


def test_bot_flow():
    async def run():
        from datetime import datetime
        from aiogram import Dispatcher
        from aiogram.methods import AnswerCallbackQuery
        from aiogram.types import CallbackQuery, Chat, Message, Update, User
        from handlers import build_router
        from test_access import setup as bot_setup

        journal, access, bot, session, _ = bot_setup()
        billing = Billing(access, FakeApi(), CFG)
        dp = Dispatcher()
        dp.include_router(build_router(access, billing))
        original = session.make_request

        async def request(b, method, timeout=None):
            if isinstance(method, AnswerCallbackQuery):
                return True
            result = await original(b, method, timeout)
            return result
        session.make_request = request
        user = User(id=GUEST, is_bot=False, first_name="g")

        def message(text):
            return Message(message_id=1, date=datetime.now(), text=text, from_user=user,
                           chat=Chat(id=GUEST, type="private")).as_(bot)

        await dp.feed_update(bot, Update(update_id=1, message=message("/start")))
        assert "6 USDT" in session.sent[-1][1]
        query = CallbackQuery(id="1", from_user=user, chat_instance="c",
                              data="buy", message=message("x"))
        await dp.feed_update(bot, Update(update_id=2, callback_query=query))
        assert "Счёт действует" in session.sent[-1][1]
        assert billing.api.created[0]["payload"] == str(GUEST)
        await dp.feed_update(bot, Update(update_id=3, message=message("/paysupport помогите")))
        assert session.sent[-2][0] == ADMIN
        count = len(session.sent)
        await dp.feed_update(bot, Update(update_id=4, message=message("/settings")))
        assert len(session.sent) == count
        access.revoke(GUEST)
        await dp.feed_update(bot, Update(update_id=5, message=message("/start")))
        assert "6 USDT" in session.sent[-1][1]
        query = CallbackQuery(id="2", from_user=user, chat_instance="c",
                              data="buy", message=message("x"))
        await dp.feed_update(bot, Update(update_id=6, callback_query=query))
        assert "Счёт действует" in session.sent[-1][1]
    asyncio.run(run())
