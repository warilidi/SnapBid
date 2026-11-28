import asyncio
import logging
import math
from dataclasses import replace

import aiohttp
import socketio
from aiogram import Bot, Dispatcher

from access import Access, marketplace_currency
from billing import Billing, CryptoPay
from config import Config
import csgomarket
from dmarket import DMarketClient, DMarketListing
from csfloat import CSFloatClient, CSFloatListing
from bitskins import BitSkinsClient, BitSkinsListing
from handlers import build_router
from journal import Journal
from notifier import MarketplaceSignal, Notifier
from pricing import PriceBook, check, evaluate, make_key, net_profit
from stars import Stars, build_stars_router
from stats import Stats
import waxpeer

log = logging.getLogger("skinarb")
API = "https://api.skinport.com/v1/sales/history"
SITE = "https://skinport.com"
MARKET_ALERT_INTERVAL = 600
REFRESH = 10 * 60  # лимит API: 8 запросов за 5 минут, нам хватает одного за 10


async def refresh_prices(book: PriceBook, journal: Journal | None, cfg: Config) -> None:
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
                checked = journal.check_outcomes(book, cfg) if journal else 0
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


def handle_marketplace_listing(
    signal: MarketplaceSignal, journal: Journal, notifier: Notifier,
    access: Access, stats: Stats, book: PriceBook | None = None,
) -> None:
    provider = signal.provider.lower()
    limited = provider in ("waxpeer", "csgomarket", "bitskins", "csfloat")
    if limited and not signal.url:
        stats.add(f"{signal.provider}: нет ссылки")
        return
    if limited:
        cfg = access.cfg
        history = book.get(make_key(signal.name, signal.phase)) if book and book.is_fresh() else None
        # Never price an unspecified or unrecognised Doppler phase as a different variant.
        if ("doppler" in signal.name.lower() and not signal.phase
                or history is None or history.volume_7d < cfg.min_volume_7d
                or history.volume_30d < cfg.min_volume_30d):
            stats.add(f"{signal.provider}: нет подходящей истории продаж")
            return
        profit = net_profit(signal.price, history.fair, cfg)
        if profit < cfg.min_profit:
            stats.add(f"{signal.provider}: прибыль ниже порога")
            return
        signal = replace(signal, fair=history.fair, profit=profit, roi=profit / signal.price)
    matches = []
    for user in access.users():
        if user.paused:
            continue
        settings = access.market_settings(user.user_id, provider)
        if settings is None or not settings.enabled:
            continue
        if signal.roi is not None and signal.roi < user.min_roi:
            continue
        if not settings.min_price <= signal.price <= settings.max_price:
            continue
        if settings.min_float is not None or settings.max_float is not None:
            if signal.wear is None:
                continue
            if settings.min_float is not None and signal.wear < settings.min_float:
                continue
            if settings.max_float is not None and signal.wear > settings.max_float:
                continue
        matches.append(user.user_id)

    if not matches:
        stats.add(f"{signal.provider}: нет получателей")
        return
    claimed_users = set(journal.add_market_deliveries(
        provider, signal.listing_id, signal.price, matches,
        cooldown=MARKET_ALERT_INTERVAL if limited else 0))
    if claimed_users:
        journal.add_market_signal(provider, signal.listing_id, signal.price)
    for user_id in claimed_users:
        notifier.push(user_id, signal)
    stats.add(
        f"{signal.provider}: подошёл" if claimed_users
        else f"{signal.provider}: повтор или лимит")


def handle_waxpeer_listing(listing: waxpeer.WaxpeerListing, journal: Journal,
                           notifier: Notifier, access: Access, stats: Stats, book: PriceBook) -> None:
    handle_marketplace_listing(
        MarketplaceSignal("Waxpeer", listing.item_id, listing.name, listing.price,
                          "USD", listing.wear, listing.url, phase=listing.phase),
        journal, notifier, access, stats, book)


def has_market_users(access: Access, provider: str) -> bool:
    return any(not u.paused and (s := access.market_settings(u.user_id, provider))
               and s.enabled for u in access.users())


async def monitor_waxpeer(journal: Journal, notifier: Notifier,
                          access: Access, stats: Stats, book: PriceBook) -> None:
    task = None
    try:
        while True:
            active = has_market_users(access, "waxpeer")
            if active and (task is None or task.done()):
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
                task = asyncio.create_task(waxpeer.listen(
                    lambda item: handle_waxpeer_listing(item, journal, notifier, access, stats, book)))
            elif not active and task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                task = None
            await asyncio.sleep(5)
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def scan_csgomarket(session: aiohttp.ClientSession, currency: str, journal: Journal,
                          notifier: Notifier, access: Access, stats: Stats, book: PriceBook) -> None:
    """Compare complete snapshots; do not promote or emit from a failed scan."""
    db = journal.db
    state = f"csgomarket:{currency}"
    db.execute("CREATE TABLE IF NOT EXISTS csgomarket_snapshot "
               "(currency TEXT, listing_id TEXT, price REAL NOT NULL, "
               "PRIMARY KEY (currency, listing_id))")
    db.execute("CREATE TEMP TABLE IF NOT EXISTS csgomarket_staging "
               "(listing_id TEXT PRIMARY KEY, name TEXT, price REAL, wear REAL, phase TEXT)")
    with db:
        db.execute("DELETE FROM csgomarket_staging")
    batch = []
    async for item in csgomarket.listings(session, currency):
        batch.append((item.item_id, item.name, item.price, item.wear, item.phase))
        if len(batch) >= 1000:
            with db:
                db.executemany("INSERT OR REPLACE INTO csgomarket_staging VALUES (?, ?, ?, ?, ?)", batch)
            batch.clear()
            await asyncio.sleep(0)
    if batch:
        with db:
            db.executemany("INSERT OR REPLACE INTO csgomarket_staging VALUES (?, ?, ?, ?, ?)", batch)
    if not db.execute("SELECT 1 FROM csgomarket_staging LIMIT 1").fetchone():
        raise ValueError("Empty CS:GO Market snapshot")

    if journal.market_baseline_completed(state):
        cursor = db.execute(
            "SELECT s.listing_id, s.name, s.price, s.wear, s.phase FROM csgomarket_staging s "
            "LEFT JOIN csgomarket_snapshot old ON old.currency = ? "
            "AND old.listing_id = s.listing_id "
            "WHERE old.listing_id IS NULL OR old.price != s.price", (currency,))
        while rows := cursor.fetchmany(500):
            for item_id, name, price, wear, phase in rows:
                item = csgomarket.Listing(item_id, name, price, currency, wear, phase)
                handle_marketplace_listing(
                    MarketplaceSignal("CSGOMarket", item_id, name, price, currency, wear, item.url, phase=phase),
                    journal, notifier, access, stats, book)
            await asyncio.sleep(0)
    with db:
        db.execute("DELETE FROM csgomarket_snapshot WHERE currency = ?", (currency,))
        db.execute("INSERT INTO csgomarket_snapshot SELECT ?, listing_id, price "
                   "FROM csgomarket_staging", (currency,))
        db.execute("INSERT INTO market_provider_state (provider, baseline_completed) VALUES (?, 1) "
                   "ON CONFLICT(provider) DO UPDATE SET baseline_completed = 1", (state,))


async def monitor_csgomarket(journal: Journal, notifier: Notifier,
                             access: Access, stats: Stats, book: PriceBook) -> None:
    timeout = aiohttp.ClientTimeout(total=90)
    currency = marketplace_currency("csgomarket", access.cfg)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            active = has_market_users(access, "csgomarket")
            if active:
                try:
                    await scan_csgomarket(session, currency, journal, notifier, access, stats, book)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("CS:GO Market scan failed")
            elif journal.market_baseline_completed(f"csgomarket:{currency}"):
                with journal.db:
                    journal.db.execute(
                        "UPDATE market_provider_state SET baseline_completed = 0 WHERE provider = ?",
                        (f"csgomarket:{currency}",))
            await asyncio.sleep(300 if active else 5)


def handle_dmarket_listing(
    listing: DMarketListing, journal: Journal, notifier: Notifier,
    access: Access, stats: Stats,
) -> None:
    handle_marketplace_listing(
        MarketplaceSignal(
            provider="DMarket",
            listing_id=listing.item_id,
            name=listing.title,
            price=listing.price,
            currency="USD",
            wear=listing.float_value,
        ),
        journal, notifier, access, stats)


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


def handle_csfloat_listing(
    listing: CSFloatListing, journal: Journal, notifier: Notifier,
    access: Access, stats: Stats, book: PriceBook,
) -> None:
    handle_marketplace_listing(
        MarketplaceSignal(
            provider="CSFloat",
            listing_id=listing.listing_id,
            name=listing.name,
            price=listing.price,
            currency="USD",
            wear=listing.wear,
            url=listing.url,
        ),
        journal, notifier, access, stats, book)


async def monitor_csfloat(
    journal: Journal, notifier: Notifier, access: Access,
    stats: Stats, book: PriceBook,
) -> None:
    client = CSFloatClient()
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            active = has_market_users(access, "csfloat")
            if active:
                users = [u for u in access.users() if not u.paused]
                low = min(
                    access.market_settings(u.user_id, "csfloat").min_price
                    for u in users
                    if access.market_settings(u.user_id, "csfloat"))
                high = max(
                    access.market_settings(u.user_id, "csfloat").max_price
                    for u in users
                    if access.market_settings(u.user_id, "csfloat"))
                initialized = journal.market_baseline_completed("csfloat")
                try:
                    async for listing in client.listings(session, low, high):
                        if initialized:
                            handle_csfloat_listing(
                                listing, journal, notifier, access, stats, book)
                        else:
                            journal.add_market_signal(
                                "csfloat", listing.listing_id, listing.price)
                    if not initialized:
                        journal.complete_market_baseline("csfloat")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    stats.add("CSFloat: ошибка API")
                    log.exception("CSFloat scan failed")
            await asyncio.sleep(60 if active else 5)


def handle_bitskins_listing(
    listing: BitSkinsListing, journal: Journal, notifier: Notifier,
    access: Access, stats: Stats, book: PriceBook,
) -> None:
    handle_marketplace_listing(
        MarketplaceSignal(
            provider="BitSkins",
            listing_id=listing.listing_id,
            name=listing.name,
            price=listing.price,
            currency="USD",
            wear=listing.wear,
            url=listing.url,
        ),
        journal, notifier, access, stats, book)


async def monitor_bitskins(
    journal: Journal, notifier: Notifier, access: Access,
    stats: Stats, book: PriceBook,
) -> None:
    if not access.cfg.bitskins_api_key or not access.cfg.bitskins_api_secret:
        while True:
            await asyncio.sleep(60)
    client = BitSkinsClient(
        access.cfg.bitskins_api_key, access.cfg.bitskins_api_secret)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            active = has_market_users(access, "bitskins")
            if active:
                users = [u for u in access.users() if not u.paused]
                settings = [
                    access.market_settings(u.user_id, "bitskins")
                    for u in users
                ]
                settings = [s for s in settings if s]
                low = min(s.min_price for s in settings)
                high = max(s.max_price for s in settings)
                initialized = journal.market_baseline_completed("bitskins")
                try:
                    async for listing in client.listings(session, low, high):
                        if initialized:
                            handle_bitskins_listing(
                                listing, journal, notifier, access, stats, book)
                        else:
                            journal.add_market_signal(
                                "bitskins", listing.listing_id, listing.price)
                    if not initialized:
                        journal.complete_market_baseline("bitskins")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    stats.add("BitSkins: ошибка API")
                    log.exception("BitSkins scan failed")
            await asyncio.sleep(60 if active else 5)


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
    market_currency = marketplace_currency("csgomarket", cfg)
    usd_book = book if cfg.currency == "USD" else PriceBook()
    cs_book = usd_book if market_currency == "USD" else book
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
            if usd_book is not book:
                tasks.create_task(refresh_prices(usd_book, None, replace(cfg, currency="USD")))
            tasks.create_task(listen(book, journal, notifier, access, cfg, stats))
            tasks.create_task(monitor_waxpeer(journal, notifier, access, stats, usd_book))
            tasks.create_task(monitor_csgomarket(journal, notifier, access, stats, cs_book))
            tasks.create_task(monitor_csfloat(journal, notifier, access, stats, usd_book))
            tasks.create_task(monitor_bitskins(journal, notifier, access, stats, usd_book))
            tasks.create_task(log_stats(stats))
            if billing:
                tasks.create_task(billing.run(bot))
    finally:
        try:
            await notifier.close()
        finally:
            try:
                await bot.session.close()
            finally:
                journal.db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
