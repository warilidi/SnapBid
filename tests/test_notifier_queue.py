import asyncio

from notifier import MarketplaceSignal, Notifier
from pricing import evaluate
from test_bot import CFG, book, sale


def test_new_marketplace_events_do_not_evict_pending_skinport_alert():
    async def run():
        notifier = Notifier(None, None)
        alert = evaluate(sale(), book(), CFG)
        notifier.push(1, alert)
        for i in range(25):
            notifier.push(1, MarketplaceSignal("Waxpeer", str(i), "item", 10, "USD"))
        pending = [notifier.queues[1].get_nowait()[1] for _ in range(20)]
        assert pending[0] == alert
        assert [s.listing_id for s in pending[1:]] == [str(i) for i in range(6, 25)]
        await notifier.close()
    asyncio.run(run())


def test_full_queue_of_skinport_alerts_rejects_marketplace_event():
    async def run():
        notifier = Notifier(None, None)
        alert = evaluate(sale(), book(), CFG)
        for _ in range(20):
            notifier.push(1, alert)
        notifier.push(1, MarketplaceSignal("Waxpeer", "1", "item", 10, "USD"))
        assert all(notifier.queues[1].get_nowait()[1] == alert for _ in range(20))
        await notifier.close()
    asyncio.run(run())
