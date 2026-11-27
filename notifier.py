import asyncio
import html
import logging
from datetime import date

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from access import Access
from pricing import Signal

log = logging.getLogger("notifier")


def render(s: Signal) -> str:
    title = s.name + (f" [{s.version}]" if s.version else "")
    lines = [
        f"<b>{html.escape(title)}</b>",
        f"Цена: <b>{s.price:.2f} {s.currency}</b>",
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


class Notifier:
    """У каждого участника своя очередь: медленный получатель не тормозит других."""

    def __init__(self, bot: Bot, access: Access, pause: float = 1.1) -> None:
        self.bot, self.access, self.pause = bot, access, pause
        self.queues: dict[int, asyncio.Queue[tuple[float, Signal]]] = {}
        self.workers: dict[int, asyncio.Task] = {}

    def push(self, user_id: int, signal: Signal) -> None:
        queue = self.queues.setdefault(user_id, asyncio.Queue(maxsize=20))
        if queue.full():
            queue.get_nowait()  # старые лоты уже неактуальны
        queue.put_nowait((asyncio.get_running_loop().time(), signal))
        if user_id not in self.workers:
            self.workers[user_id] = asyncio.create_task(self._run(user_id, queue))

    async def send(self, user_id: int, s: Signal) -> None:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Открыть лот", url=s.url)]])
        await self.bot.send_message(
            user_id, render(s), parse_mode="HTML", reply_markup=keyboard)

    async def _run(self, user_id: int, queue: asyncio.Queue) -> None:
        loop = asyncio.get_running_loop()
        while True:
            created, signal = await queue.get()
            # Доступ могли отозвать, пока сигнал ждал в очереди
            user = self.access.get(user_id)
            if user is None or user.paused or loop.time() - created > 60:
                continue
            try:
                try:
                    await self.send(user_id, signal)
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                    user = self.access.get(user_id)
                    if user and not user.paused and loop.time() - created <= 60:
                        await self.send(user_id, signal)
            except Exception as e:
                log.warning("Не удалось отправить сигнал %s: %s", user_id, e)
            await asyncio.sleep(self.pause)

    async def close(self) -> None:
        for task in self.workers.values():
            task.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)
