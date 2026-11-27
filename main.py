import asyncio
import logging
import math

import aiohttp
import socketio
from aiogram import Bot, Dispatcher

from access import Access
from billing import Billing, CryptoPay
from config import Config
from dmarket import DMarketClient, DMarketListing
from handlers import build_router
from journal import Journal
from notifier import MarketplaceSignal, Notifier
from pricing import PriceBook, check, evaluate
from stars import Stars, build_stars_router
from stats import Stats

log = logging.getLogger("skinarb")
API = "https://api.skinport.com/v1/sales/history"
SITE = "https://skinport.com"
REFRESH = 10 * 60  # лимит API: 8 запросов за 5 минут, нам хватает одного за 10


async def refresh_prices(book: PriceBook, journal: Journal, cfg: Config) -> None:
    params = {"app_id": 730, "currency": cfg.currency}
    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            delay = REFRESH
            try:
                async with session.get(API, params=params,
                                       headers={"Accept-Encoding": "br"}) as resp:
                    resp.raise_for_status()
                    count = book.load(await resp.json())
                if not count:
                    raise ValueError("пустая история продаж")
                checked = journal.check_outcomes(book, cfg)
                log.info("Цены обновлены: %d предметов, проверено старых сигналов: %d",
                         count, checked)
            except Exception as e:
                delay = 5 * 60
                log.warning("Не удалось обновить цены: %s", e)
            await asyncio.sleep(delay)


def handle_event(event: dict, book: PriceBook, journal: Journal, notifier: Notifier,
                 access: Access, cfg: Config, stats: Stats) -> None:
    if event.get("eventType") != "listed":
        return
    fresh = book.is_fresh()
    users = [u for u in access.users()
             if not u.paused
             and (settings := access.market_settings(u.user_id, "skinport"))
             and settings.enabled]
    # Причины отсева считаем по фильтрам администратора
    admin = access.get(cfg.admin_id)
    admin_cfg = admin.apply(cfg) if admin else cfg
    for sale in event.get("sales", []):
        if not fresh:
            stats.add("цены ещё не загружены или устарели")
            continue
        try:
            matches = []
            for user in users:
                signal = evaluate(sale, book, user.apply(cfg))
                if signal:
                    matches.append((user.user_id, signal))
            # В журнал идёт всё, что прошло базовые фильтры или чьи-то личные
            found = matches[0][1] if matches else evaluate(sale, book, cfg)
            if matches:
                result = "подошли"
            else:
                result = check(sale, book, admin_cfg)[1]
                if result == "подошли":
                    result = "подошли, но сигналы на паузе"
            if found:
                is_new_signal = journal.add(found)
                claimed_users = set(journal.add_deliveries(matches))
                for user_id, signal in matches:
                    if user_id in claimed_users:
                        notifier.push(user_id, signal)
                if not is_new_signal:
                    result = "повторы"
            stats.add(result)
        except Exception:
            stats.add("ошибка обработки")
            log.exception("Ошибка обработки лота")


def handle_dmarket_listing(
    listing: DMarketListing, journal: Journal, notifier: Notifier,
    access: Access, stats: Stats,
) -> None:
    if not journal.add_market_signal("dmarket", listing.item_id, listing.price):
        stats.add("DMarket: повтор")
        return
    matches = []
    for user in access.users():
        if user.paused:
            continue
        settings = access.market_settings(user.user_id, "dmarket")
        if settings is None or not settings.enabled:
            continue
        if not settings.min_price <= listing.price <= settings.max_price:
            continue
        if settings.min_float is not None or settings.max_float is not None:
            if listing.float_value is None:
                continue
            if settings.min_float is not None and listing.float_value < settings.min_float:
                continue
            if settings.max_float is not None and listing.float_value > settings.max_float:
                continue
        matches.append(user.user_id)

    signal = MarketplaceSignal(
        provider="DMarket",
        listing_id=listing.item_id,
        name=listing.title,
        price=listing.price,
        currency="USD",
        wear=listing.float_value,
    )
    claimed_users = set(journal.add_market_deliveries(
        "dmarket", listing.item_id, listing.price, matches))
    for user_id in claimed_users:
        notifier.push(user_id, signal)
    stats.add("DMarket: подошёл" if claimed_users else "DMarket: нет получателей")


async def monitor_dmarket(
    client: DMarketClient, journal: Journal, notifier: Notifier,
    access: Access, stats: Stats,
) -> None:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            started = asyncio.get_running_loop().time()
            users = [
                user for user in access.users()
                if not user.paused
                and (settings := access.market_settings(user.user_id, "dmarket"))
                and settings.enabled
            ]
            if not users:
                await asyncio.sleep(5)
                continue
            low = min(access.market_settings(u.user_id, "dmarket").min_price for u in users)
            high = max(access.market_settings(u.user_id, "dmarket").max_price for u in users)
            initialized = journal.market_baseline_completed("dmarket")
            try:
                async for listing in client.listings(
                        session, math.floor(low * 100), math.ceil(high * 100)):
                    if initialized:
                        handle_dmarket_listing(listing, journal, notifier, access, stats)
                    else:
                        journal.add_market_signal("dmarket", listing.item_id, listing.price)
                if not initialized:
                    journal.complete_market_baseline("dmarket")
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0, 60 - elapsed))
            except asyncio.CancelledError:
                raise
            except Exception:
                stats.add("DMarket: ошибка API")
                log.exception("DMarket scan failed")
                await asyncio.sleep(60)


async def log_stats(stats: Stats) -> None:
    while True:
        await asyncio.sleep(60)
        log.info("%s", stats.text())


async def listen(book: PriceBook, journal: Journal, notifier: Notifier,
                 access: Access, cfg: Config, stats: Stats) -> None:
    while True:
        # timestamp_requests=False обязателен по документации Skinport
        sio = socketio.AsyncClient(serializer="msgpack", timestamp_requests=False,
                                   reconnection=False)

        @sio.event
        async def connect():
            await sio.emit("saleFeedJoin",
                           {"currency": cfg.currency, "locale": "en", "appid": 730})
            log.info("Подключён к live-фиду")

        @sio.on("saleFeed")
        async def on_sale_feed(event):
            handle_event(event, book, journal, notifier, access, cfg, stats)

        try:
            await sio.connect(SITE, transports=["websocket"])
            await sio.wait()
        except Exception as e:
            log.warning("Фид недоступен: %s", e)
        finally:
            await sio.disconnect()
        log.info("Переподключение через 15 секунд")
        await asyncio.sleep(15)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    stats = Stats()
    cfg = Config.from_env()
    if not cfg.token or cfg.admin_id <= 0:
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и ADMIN_ID в файле .env")
    book, journal = PriceBook(), Journal(cfg.db_path)
    access = Access(journal.db, cfg)
    bot = Bot(cfg.token)
    notifier = Notifier(bot, access)
    dispatcher = Dispatcher()
    billing = None
    if cfg.crypto_token:
        billing = Billing(access, CryptoPay(cfg.crypto_token, cfg.crypto_url), cfg)
    stars = Stars(access) if cfg.stars_price > 0 else None
    if stars:  # этот роутер должен стоять первым
        dispatcher.include_router(build_stars_router(stars, billing is not None))
    dispatcher.include_router(build_router(access, billing, stats, stars))
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(dispatcher.start_polling(bot, handle_signals=False))
            tasks.create_task(refresh_prices(book, journal, cfg))
            tasks.create_task(listen(book, journal, notifier, access, cfg, stats))
            tasks.create_task(log_stats(stats))
            if billing:
                tasks.create_task(billing.run(bot))
    finally:
        await notifier.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
