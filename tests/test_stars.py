import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.methods import (AnswerCallbackQuery, AnswerPreCheckoutQuery,
                             RefundStarPayment, SendInvoice)
from aiogram.types import (CallbackQuery, Chat, Message, PreCheckoutQuery,
                           SuccessfulPayment, Update, User)

from access import Access
from config import Config
from handlers import build_router
from journal import Journal
from stars import Stars, build_stars_router
from test_access import ADMIN, GUEST, FakeSession

CFG = Config(admin_id=ADMIN, sub_days=30)


class StarSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, (AnswerCallbackQuery, AnswerPreCheckoutQuery,
                               RefundStarPayment, SendInvoice)):
            self.calls.append(method)
            return True
        return await super().make_request(bot, method, timeout)


def test_stars_flow():
    async def run():
        access = Access(Journal(":memory:").db, CFG)
        stars, session = Stars(access), StarSession()
        bot, dp = Bot("1:test", session=session), Dispatcher()
        dp.include_router(build_stars_router(stars))
        dp.include_router(build_router(access, stars=stars))
        user = User(id=GUEST, is_bot=False, first_name="g")

        def message(who=user, **kw):
            return Message(message_id=1, date=datetime.now(), from_user=who,
                           chat=Chat(id=who.id, type="private"), **kw).as_(bot)

        async def feed(**kw):
            session.sent.clear()
            session.calls.clear()
            await dp.feed_update(bot, Update(update_id=1, **kw))

        assert {"pre_checkout_query", "callback_query"} <= set(dp.resolve_used_update_types())
        for command in ("/start", "/buy"):
            await feed(message=message(text=command))
            assert "400 Stars" in session.sent[0][1] and "@warilidi" in session.sent[0][1]
        await feed(message=message(text="/terms"))
        assert "не гарантирована" in session.sent[0][1]
        await feed(callback_query=CallbackQuery(
            id="terms", from_user=user, chat_instance="c", data="menu:terms",
            message=message(text="offer")))
        assert "Условия подписки" in session.sent[0][1]
        assert "menu:buy" in {
            button.callback_data for row in session.markups[-1].inline_keyboard
            for button in row}
        await feed(message=message(text="/start"))
        callback_data = [button.callback_data for row in session.markups[-1].inline_keyboard
                         for button in row]
        assert "menu:terms" in callback_data
        assert len(callback_data) == len(set(callback_data))
        await feed(message=message(text="/paysupport"))
        assert "/paysupport текст" in session.sent[0][1]

        await feed(callback_query=CallbackQuery(
            id="1", from_user=user, chat_instance="c", data="stars", message=message(text="x")))
        invoice = session.calls[-1]
        assert (invoice.currency, invoice.provider_token, invoice.prices[0].amount) == \
            ("XTR", "", 400)

        async def precheck(amount=400, payload=invoice.payload):
            await feed(pre_checkout_query=PreCheckoutQuery(
                id="p", from_user=user, currency="XTR", total_amount=amount,
                invoice_payload=payload).as_(bot))
            return session.calls[-1].ok

        assert await precheck()
        assert not await precheck(amount=1)
        assert not await precheck(payload="sub:999:30")
        assert access.get(GUEST) is None  # до оплаты доступа нет

        def payment(charge, currency="XTR", amount=400, payload=None):
            return message(successful_payment=SuccessfulPayment(
                currency=currency, total_amount=amount,
                invoice_payload=payload if payload is not None else invoice.payload,
                telegram_payment_charge_id=charge, provider_payment_charge_id=""))

        await feed(message=payment("bad", amount=1, payload="sub:222:999"))
        assert access.get(GUEST) is None
        assert stars.payment("bad")[2] == "refunded"
        assert isinstance(session.calls[0], RefundStarPayment)

        await feed(message=payment("c1"))
        first = access.get(GUEST).expires_at
        assert [c for c, _ in session.sent] == [GUEST, ADMIN]
        await feed(message=payment("c1"))  # повтор того же платежа
        assert access.get(GUEST).expires_at == first and session.sent == []
        await feed(message=payment("c2"))  # продление участником
        assert access.get(GUEST).expires_at == first + 30 * 86400

        await feed(message=message(text="/refund c2"))  # не админ
        assert not session.calls
        admin = User(id=ADMIN, is_bot=False, first_name="a")
        await feed(message=message(admin, text="/refund c2"))
        assert isinstance(session.calls[0], RefundStarPayment)
        assert access.get(GUEST).expires_at == first
        await feed(message=message(admin, text="/refund c2"))
        assert not session.calls

        access.revoke(GUEST)
        assert not await precheck()
        for command in ("/start", "/buy"):
            await feed(message=message(text=command))
            assert "Доступ заблокирован" in session.sent[0][1]
        await feed(callback_query=CallbackQuery(
            id="2", from_user=user, chat_instance="c", data="stars", message=message(text="x")))
        assert not any(isinstance(c, SendInvoice) for c in session.calls)
        await feed(message=payment("c3"))
        assert access.get(GUEST) is None
        assert access.is_banned(GUEST) and stars.payment("c3")[2] == "refunded"
        assert isinstance(session.calls[0], RefundStarPayment)
    asyncio.run(run())
