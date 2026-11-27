"""Счётчики с момента запуска: видно, что бот работает и почему молчит."""
import time
from collections import Counter
from typing import Callable


class Stats:
    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._started_at = self._clock()
        self.lots = 0
        self.results: Counter[str] = Counter()

    def add(self, result: str) -> None:
        self.lots += 1
        self.results[result] += 1

    def uptime(self) -> str:
        minutes = max(0, int(self._clock() - self._started_at) // 60)
        days, remaining = divmod(minutes, 24 * 60)
        hours, minutes = divmod(remaining, 60)
        if days:
            return f"{days} дн. {hours} ч. {minutes} мин."
        if hours:
            return f"{hours} ч. {minutes} мин."
        return f"{minutes} мин."

    def text(self) -> str:
        lines = [
            f"Время работы процесса: {self.uptime()}",
            f"Лотов получено с запуска: {self.lots}",
        ]
        lines += [f"  {reason}: {count}" for reason, count in self.results.most_common()]
        return "\n".join(lines)
