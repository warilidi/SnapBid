import asyncio
import html
import json
import logging
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import TypeAlias
from urllib.parse import parse_qs, urlsplit

import aiohttp

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from access import Access
from pricing import Signal

log = logging.getLogger("notifier")
LISTING_HOSTS = {"waxpeer": "waxpeer.com", "csgomarket": "market.csgo.com"}
MAX_LISTING_PAGE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class MarketplaceSignal:
    provider: str
    listing_id: str
    name: str
    price: float
    currency: str
    wear: float | None = None
    url: str | None = None
    phase: str | None = None
    fair: float | None = None
    profit: float | None = None
    roi: float | None = None


Notification: TypeAlias = Signal | MarketplaceSignal


def render(s: Signal) -> str:
    title = s.name + (f" [{s.version}]" if s.version else "")
    lines = [
        f"<b>{html.escape(title)}</b>",
        f"Цена в событии размещения: <b>{s.price:.2f} {s.currency}</b>",
        f"Медиана продаж: {s.fair:.2f} {s.currency} (за 7 дней продаж: {s.volume_7d})",
        f"Расчётная прибыль после комиссии: {s.profit:.2f} {s.currency} ({s.roi:.1%})",
    ]
    if s.wear is not None:
        lines.append(f"Float: {s.wear:.4f}")
    if isinstance(s.lock, str):
        try:
            lock_date = date.fromisoformat(s.lock[:10]).isoformat()
        except ValueError:
            pass
        else:
            lines.append(f"Trade lock до: {html.escape(lock_date)}")
    lines.append("<i>Это оценка, а не гарантия: цена может измениться.</i>")
    return "\n".join(lines)


def render_marketplace(s: MarketplaceSignal) -> str:
    lines = [
        f"<b>{html.escape(s.name)}</b>",
        f"Цена объявления: <b>{s.price:.2f} {html.escape(s.currency)}</b>",
        f"Площадка: {html.escape(s.provider)}",
        f"ID лота: <code>{html.escape(s.listing_id)}</code>",
    ]
    if s.wear is not None:
        lines.append(f"Float: {s.wear:.4f}")
    if s.fair is not None and s.profit is not None and s.roi is not None:
        currency = html.escape(s.currency)
        if s.provider.lower() == "waxpeer":
            lines.extend([
                f"Макс. заявка на Waxpeer: {s.fair:.2f} {currency}",
                f"Оценка прибыли после комиссии Waxpeer: {s.profit:.2f} {currency} ({s.roi:.1%})",
                "<i>Заявка может измениться или иметь условия.</i>",
            ])
        else:
            lines.extend([
                f"Ориентир по продажам Skinport: {s.fair:.2f} {currency}",
                f"Оценка прибыли при продаже на Skinport: {s.profit:.2f} {currency} ({s.roi:.1%})",
            ])
    else:
        lines.append("<i>История продаж и комиссия не включены: данные не подтверждены.</i>")
    return "\n".join(lines)


class ListingPage(HTMLParser):
    def __init__(self, signal: MarketplaceSignal) -> None:
        super().__init__()
        self.signal = signal
        self.matched = False
        self.read_product = False

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if self.signal.provider.lower() == "csgomarket":
            if (tag == "span" and data.get("data-id") == self.signal.listing_id
                    and data.get("data-hash") == self.signal.name):
                self.matched = True
        elif tag == "script":
            self.read_product = data.get("type") == "application/ld+json"

    def handle_endtag(self, tag):
        if tag == "script":
            self.read_product = False

    def handle_data(self, data):
        if not self.read_product:
            return
        try:
            product = json.loads(data)
        except ValueError:
            return
        if (isinstance(product, dict) and product.get("@type") == "Product"
                and str(product.get("sku")) == self.signal.listing_id
                and product.get("name") == self.signal.name):
            self.matched = True


async def confirms_listing(signal: MarketplaceSignal) -> bool:
    provider = signal.provider.lower()
    if not signal.url:
        return False
    url = urlsplit(signal.url)
    if (url.scheme != "https" or url.netloc != LISTING_HOSTS.get(provider)
            or parse_qs(url.query).get("id") != [signal.listing_id]):
        return False
    timeout = aiohttp.ClientTimeout(total=6)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(signal.url, allow_redirects=False) as response:
            if response.status != 200:
                return False
            payload = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                payload.extend(chunk)
                if len(payload) > MAX_LISTING_PAGE_BYTES:
                    return False
    page = ListingPage(signal)
    page.feed(payload.decode("utf-8", errors="replace"))
    page.close()
    return page.matched


class Notifier:
    """У каждого участника своя очередь: медленный получатель не тормозит других."""

    def __init__(self, bot: Bot, access: Access, pause: float = 1.1) -> None:
        self.bot, self.access, self.pause = bot, access, pause
        self.queues: dict[int, asyncio.Queue[tuple[float, Notification]]] = {}
        self.workers: dict[int, asyncio.Task] = {}

    def push(self, user_id: int, signal: Notification) -> None:
        queue = self.queues.setdefault(user_id, asyncio.Queue(maxsize=20))
        if queue.full():
            # Keep Skinport profit alerts from being displaced by the larger raw feeds.
            pending = []
            while not queue.empty():
                pending.append(queue.get_nowait())
                queue.task_done()
            discard = next((i for i, (_, item) in enumerate(pending)
                            if isinstance(item, MarketplaceSignal)), None)
            if discard is None and isinstance(signal, Signal):
                discard = 0
            if discard is not None:
                pending.pop(discard)
            for item in pending:
                queue.put_nowait(item)
            if discard is None:
                return
        queue.put_nowait((asyncio.get_running_loop().time(), signal))
        if user_id not in self.workers:
            self.workers[user_id] = asyncio.create_task(self._run(user_id, queue))

    async def send(self, user_id: int, s: Notification) -> None:
        if isinstance(s, Signal):
            text = render(s)
            url = s.url
        else:
            text = render_marketplace(s)
            url = s.url
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Открыть лот", url=url)]
        ]) if url else None
        await self.bot.send_message(
            user_id, text, parse_mode="HTML", reply_markup=keyboard)

    def _deliverable(self, user_id: int, signal: Notification) -> bool:
        user = self.access.get(user_id)
        provider = "skinport" if isinstance(signal, Signal) else signal.provider.lower()
        settings = self.access.market_settings(user_id, provider)
        return bool(user and not user.paused and settings and settings.enabled)

    async def _send_current(self, user_id: int, signal: Notification, created: float) -> None:
        loop = asyncio.get_running_loop()
        if not self._deliverable(user_id, signal) or loop.time() - created > 60:
            return
        if isinstance(signal, MarketplaceSignal) and signal.provider.lower() in LISTING_HOSTS:
            if not await confirms_listing(signal):
                log.info("%s: страница не подтвердила лот %s", signal.provider, signal.listing_id)
                return
            if not self._deliverable(user_id, signal) or loop.time() - created > 60:
                return
        await self.send(user_id, signal)

    async def _run(self, user_id: int, queue: asyncio.Queue) -> None:
        while True:
            created, signal = await queue.get()
            try:
                try:
                    try:
                        await self._send_current(user_id, signal, created)
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(e.retry_after)
                        await self._send_current(user_id, signal, created)
                except Exception as e:
                    log.warning("Не удалось отправить сигнал %s: %s", user_id, e)
                await asyncio.sleep(self.pause)
            finally:
                queue.task_done()

    async def close(self) -> None:
        for task in self.workers.values():
            task.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)
