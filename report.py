"""Статистика по сигналам: python report.py"""
import statistics

from config import Config
from journal import Journal


def main() -> None:
    cfg = Config.from_env()
    db = Journal(cfg.db_path).db
    total = db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    rois = [r[0] for r in db.execute(
        "SELECT later_roi FROM signals WHERE later_roi IS NOT NULL")]
    print(f"Всего сигналов: {total}, проверено через 7+ дней: {len(rois)}")
    if rois:
        print(f"Средний расчётный ROI:   {statistics.mean(rois):.1%}")
        print(f"Медианный расчётный ROI: {statistics.median(rois):.1%}")
        print(f"Доля убыточных:          {sum(r < 0 for r in rois) / len(rois):.0%}")
        print("Расчёт по медиане продаж, это не фактические сделки.")


if __name__ == "__main__":
    main()
