import os
from dataclasses import dataclass, field
from pathlib import Path


def load_env(path: str = ".env") -> None:
    """Читает .env; уже заданные переменные окружения не перезаписывает."""
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Config:
    token: str = ""
    admin_id: int = 0
    invite_ttl: int = 24 * 3600
    crypto_token: str = ""
    crypto_url: str = "https://pay.crypt.bot"
    sub_price: str = "6"
    sales_contact: str = "@warilidi"  # пусто: продажи через админа выключены
    sub_asset: str = "USDT"
    sub_days: int = 14
    stars_price: int = 400  # 0: оплата звёздами выключена
    currency: str = "EUR"
    min_roi: float = 0.10
    min_profit: float = 1.0
    min_price: float = 5.0
    max_price: float = 300.0
    min_volume_7d: int = 5
    min_volume_30d: int = 20
    fee: float = 0.08
    fee_high: float = 0.06
    fee_high_from: float = 1000.0
    db_path: str = "signals.db"
    dmarket_api_key: str = field(default="", repr=False)
    dmarket_secret_key: str = field(default="", repr=False)
    bitskins_api_key: str = field(default="", repr=False)
    bitskins_api_secret: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "Config":
        load_env()
        d = cls()
        env = os.environ.get
        return cls(
            token=env("TELEGRAM_BOT_TOKEN", ""),
            admin_id=int(env("ADMIN_ID") or 0),
            crypto_token=env("CRYPTO_PAY_TOKEN", ""),
            crypto_url=env("CRYPTO_PAY_URL") or d.crypto_url,
            sub_price=env("SUB_PRICE") or d.sub_price,
            sales_contact=env("SALES_CONTACT", d.sales_contact),
            sub_asset=env("SUB_ASSET") or d.sub_asset,
            sub_days=int(env("SUB_DAYS") or d.sub_days),
            stars_price=int(env("STARS_PRICE") or d.stars_price),
            currency=env("CURRENCY", d.currency),
            min_roi=float(env("MIN_ROI", d.min_roi)),
            min_profit=float(env("MIN_PROFIT", d.min_profit)),
            min_price=float(env("MIN_PRICE", d.min_price)),
            max_price=float(env("MAX_PRICE", d.max_price)),
            min_volume_7d=int(env("MIN_VOLUME_7D", d.min_volume_7d)),
            min_volume_30d=int(env("MIN_VOLUME_30D", d.min_volume_30d)),
            fee=float(env("FEE", d.fee)),
            fee_high=float(env("FEE_HIGH", d.fee_high)),
            fee_high_from=float(env("FEE_HIGH_FROM", d.fee_high_from)),
            db_path=env("DB_PATH", d.db_path),
            dmarket_api_key=env("DMARKET_API_KEY", ""),
            dmarket_secret_key=env("DMARKET_SECRET_KEY", ""),
            bitskins_api_key=env("BITSKINS_API_KEY", ""),
            bitskins_api_secret=env("BITSKINS_API_SECRET", ""),
        )
