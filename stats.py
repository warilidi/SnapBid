"""Счётчики с момента запуска: видно, что бот работает и почему молчит."""
from collections import Counter


class Stats:
    def __init__(self) -> None:
        self.lots = 0
        self.results: Counter[str] = Counter()

    def add(self, result: str) -> None:
        self.lots += 1
        self.results[result] += 1

    def text(self) -> str:
        lines = [f"Лотов получено с запуска: {self.lots}"]
        lines += [f"  {reason}: {count}" for reason, count in self.results.most_common()]
        return "\n".join(lines)
