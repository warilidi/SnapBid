"""Рыночная цена и отбор выгодных лотов. Без сети, легко тестировать."""
import time
from dataclasses import dataclass

import msgpack

from config import Config

Key = tuple[str, str | None]


def make_key(name: str, version: str | None) -> Key:
    # В фиде обычная версия называется "default", в истории продаж это null
    return name, None if version in (None, "", "default") else version


@dataclass(frozen=True)
class Stats:
    median_7d: float
    median_30d: float
    volume_7d: int
    volume_30d: int

    @property
    def fair(self) -> float:
        # Берём меньшую медиану: защита от краткого скачка цены
        return min(self.median_7d, self.median_30d)


class PriceBook:
    MAX_AGE = 30 * 60  # по более старым ценам сигналы не выдаём

    def __init__(self) -> None:
        self._data: dict[Key, Stats] = {}
        self.updated_at = 0.0

    def load(self, history: list[dict]) -> int:
        data = {}
        for row in history:
            week = row.get("last_7_days") or {}
            month = row.get("last_30_days") or {}
            if not week.get("median") or not month.get("median"):
                continue
            data[make_key(row["market_hash_name"], row.get("version"))] = Stats(
                week["median"], month["median"], week["volume"], month["volume"]
            )
        if data:  # пустой ответ не стирает прошлые цены
            self._data = data
            self.updated_at = time.time()
        return len(data)

    def get(self, key: Key) -> Stats | None:
        return self._data.get(key)

    def is_fresh(self) -> bool:
        return time.time() - self.updated_at < self.MAX_AGE


@dataclass(frozen=True)
class Signal:
    uid: str
    name: str
    version: str | None
    price: float
    fair: float
    profit: float
    roi: float
    volume_7d: int
    currency: str
    wear: float | None
    lock: str | None
    sale_id: str | None
    asset_id: str | None
    url: str


def _numeric_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value
    return None


def _normalize_lock(value: str | msgpack.ExtType | None) -> str | None:
    if not isinstance(value, msgpack.ExtType):
        return value
    try:
        return value.data.decode("utf-8")
    except UnicodeDecodeError:
        return f"msgpack-ext-{value.code}:{value.data.hex()}"


def seller_fee(sell_price: float, cfg: Config) -> float:
    return cfg.fee_high if sell_price >= cfg.fee_high_from else cfg.fee


def net_profit(buy: float, sell: float, cfg: Config) -> float:
    return sell * (1 - seller_fee(sell, cfg)) - buy


def check(sale: dict, book: PriceBook, cfg: Config) -> tuple[Signal | None, str]:
    """Возвращает сигнал или причину, по которой лот отсеян."""
    if sale.get("saleStatus") != "listed" or sale.get("saleType") != "public":
        return None, "не обычное публичное объявление"
    if sale.get("currency") != cfg.currency:
        return None, "другая валюта"
    cents = sale.get("salePrice")
    name = sale.get("marketHashName")
    sale_id = _numeric_id(sale.get("saleId"))
    asset_id = _numeric_id(sale.get("assetid"))
    uid = sale_id or asset_id
    if not cents or not name or not uid:
        return None, "неполные данные"
    price = cents / 100  # фид отдаёт цены в центах, история — в единицах
    if not cfg.min_price <= price <= cfg.max_price:
        return None, "цена вне диапазона"
    key = make_key(name, sale.get("version"))
    stats = book.get(key)
    if stats is None:
        return None, "нет истории продаж"
    if stats.volume_7d < cfg.min_volume_7d or stats.volume_30d < cfg.min_volume_30d:
        return None, "мало продаж"
    profit = net_profit(price, stats.fair, cfg)
    roi = profit / price
    if profit < cfg.min_profit or roi < cfg.min_roi:
        return None, "прибыль ниже порога"
    slug = sale.get("url")
    if sale_id and isinstance(slug, str) and slug:
        url = f"https://skinport.com/item/{slug}/{sale_id}"
    elif asset_id:
        url = f"https://skinport.com/a/{asset_id}"
    else:
        return None, "неполные данные"
    signal = Signal(
        uid=str(uid),
        name=name,
        version=key[1],
        price=price,
        fair=stats.fair,
        profit=profit,
        roi=roi,
        volume_7d=stats.volume_7d,
        currency=cfg.currency,
        wear=sale.get("wear"),
        lock=_normalize_lock(sale.get("lock")),
        sale_id=sale_id,
        asset_id=asset_id,
        url=url,
    )
    return signal, "подошли"


def evaluate(sale: dict, book: PriceBook, cfg: Config) -> Signal | None:
    return check(sale, book, cfg)[0]
