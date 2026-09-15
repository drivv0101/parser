import argparse
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from app.db import DB_PATH, get_session, init_db
from app.logging_config import get_logger, setup_logging
from app.matching import derive_pack, extract_dimensions, unit_price, validate_price
from app.models import Product, Store, PriceHistory, ScrapeRun
from app.scrapers import SCRAPERS

logger = get_logger("run_scrape")

# Если прогон принёс меньше этой доли от уже известных товаров магазина — считаем,
# что сломалась разметка сайта, и НЕ помечаем "пропавшие" товары как исчезнувшие.
MIN_COVERAGE_RATIO = 0.5


class ScrapeFailed(Exception):
    """Прогон не дал пригодных данных — база не изменена."""


class ScrapeInProgress(Exception):
    """Обход уже идёт в другом процессе."""


# Обход могут запустить одновременно планировщик внутри сервера и человек командой
# run_scrape — тогда два процесса часами ходят по одному сайту и в конце дерутся за базу.
# Файл-замок рядом с базой это исключает; замок старше LOCK_STALE_SECONDS считаем
# оставшимся после падения процесса и забираем.
LOCK_PATH = DB_PATH.with_name(DB_PATH.name + ".scrape.lock")
LOCK_STALE_SECONDS = 4 * 3600


@contextmanager
def scrape_lock():
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            raise ScrapeInProgress(f"обход уже идёт (замок {LOCK_PATH.name}, {age / 60:.0f} мин)")
        logger.warning("замок %s старше %d ч — считаем оставшимся после сбоя, забираем", LOCK_PATH.name, LOCK_STALE_SECONDS // 3600)
        LOCK_PATH.unlink()
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def _apply_record(product: Product, record, now: datetime) -> None:
    dims = extract_dimensions(record.name)
    pack = derive_pack(record.name, dims, record.category)

    product.name = record.name
    # SQLite умеет LOWER() только для ASCII ("Д" и "д" для него разные символы),
    # поэтому поисковое поле лоуеркейсим в Python.
    product.search_text = record.name.lower()
    product.price = record.price
    product.unit = record.unit
    product.category = record.category
    product.length_mm = dims.length_mm
    product.width_mm = dims.width_mm
    product.thickness_mm = dims.thickness_mm
    product.raw_dimensions_text = dims.raw_text
    product.pack_value = pack.value if pack else None
    product.pack_unit = pack.unit if pack else None
    product.unit_price = unit_price(record.price, pack)
    product.in_stock = record.in_stock
    product.stock_note = record.stock_note
    product.is_active = True
    product.scraped_at = now


def run_store(slug: str) -> int:
    """Обновляет каталог одного магазина. Возвращает число сохранённых товаров."""
    with scrape_lock():
        return _run_store_locked(slug)


def _run_store_locked(slug: str) -> int:
    session = get_session()
    run = ScrapeRun(store_slug=slug, started_at=datetime.now(UTC).replace(tzinfo=None), status="running")
    session.add(run)
    session.commit()
    run_id = run.id
    session.close()
    try:
        return _collect_store(slug, run_id)
    except Exception as exc:
        session = get_session()
        try:
            run = session.get(ScrapeRun, run_id)
            run.status = "failed"
            run.message = str(exc)[:2000]
            run.finished_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()
        finally:
            session.close()
        raise


def _collect_store(slug: str, run_id: int) -> int:
    scraper_cls = SCRAPERS[slug]
    scraper = scraper_cls()
    logger.info("[%s] обход %s ...", slug, scraper.base_url)
    def progress(done, total, count):
        session = get_session()
        try:
            run = session.get(ScrapeRun, run_id)
            run.completed_categories, run.total_categories, run.product_count = done, total, count
            session.commit()
        finally:
            session.close()
    scraper.on_progress = progress
    records = scraper.fetch_products()
    logger.info("[%s] получено %d товаров", slug, len(records))

    valid_records = []
    for record in records:
        try:
            record.price = validate_price(record.price, record.name)
        except ValueError:
            continue
        if record.name and record.url:
            valid_records.append(record)

    if not valid_records:
        # Раньше пустой результат молча записывался как успешный прогон:
        # last_scraped_at обновлялся, и сломанная разметка выглядела как "всё ок".
        raise ScrapeFailed(f"[{slug}] источник вернул 0 пригодных товаров — база не изменена")

    session = get_session()
    try:
        store = session.query(Store).filter_by(slug=slug).one_or_none()
        if store is None:
            store = Store(slug=slug, name=scraper.name, base_url=scraper.base_url)
            session.add(store)
            session.flush()

        now = datetime.now(UTC).replace(tzinfo=None)  # наивный UTC: SQLite не хранит смещение
        # Одним запросом вместо SELECT на каждый из ~15 000 товаров.
        existing_by_url: dict[str, Product] = {
            product.url: product for product in session.query(Product).filter_by(store_id=store.id)
        }

        known_before = sum(1 for p in existing_by_url.values() if p.is_active)
        seen_urls: set[str] = set()
        for record in valid_records:
            product = existing_by_url.get(record.url)
            if product is None:
                product = Product(store_id=store.id, url=record.url)
                session.add(product)
                existing_by_url[record.url] = product
            previous_price, previous_time = product.price, product.scraped_at
            _apply_record(product, record, now)
            session.flush()
            if previous_price is not None and not session.query(PriceHistory.id).filter_by(product_id=product.id).first():
                session.add(PriceHistory(product_id=product.id, price=previous_price, recorded_at=previous_time))
            if previous_price is None or previous_price != record.price:
                session.add(PriceHistory(product_id=product.id, price=record.price, recorded_at=now))
            seen_urls.add(record.url)

        disappeared = [p for url, p in existing_by_url.items() if url not in seen_urls and p.is_active]
        if not scraper.complete or (known_before and len(seen_urls) < known_before * MIN_COVERAGE_RATIO):
            scraper.complete = False
            logger.warning(
                "[%s] собрано %d товаров при %d известных — похоже на сбой разметки, "
                "пропавшие товары НЕ деактивируем",
                slug, len(seen_urls), known_before,
            )
        else:
            for product in disappeared:
                product.is_active = False
            if disappeared:
                logger.info("[%s] пропали из каталога и скрыты из выдачи: %d", slug, len(disappeared))

        if scraper.complete:
            store.last_scraped_at = now
        run = session.get(ScrapeRun, run_id)
        run.status = "success" if scraper.complete else "partial"
        run.product_count = len(seen_urls)
        run.message = "; ".join(scraper.errors)[:2000] or (None if scraper.complete else "Получен неполный каталог; старые товары сохранены")
        run.finished_at = now
        session.commit()
        logger.info("[%s] сохранено в базу: %d активных товаров", slug, len(seen_urls))
        return len(seen_urls)
    finally:
        session.close()


def recompute_derived() -> None:
    """Заново разбирает размеры и фасовку из уже сохранённых названий (без обращения к сайтам) —
    используется после правок в app/matching.py, чтобы не пересобирать весь каталог заново."""
    session = get_session()
    try:
        products = session.query(Product).all()
        changed = 0
        hidden = 0
        for product in products:
            try:
                validate_price(product.price, product.name)
            except ValueError:
                # Цена 0 ("по запросу") попала в базу до того, как появилась проверка,
                # и такой товар получал бейдж "Лучшая цена".
                if product.is_active:
                    product.is_active = False
                    hidden += 1
                continue
            dims = extract_dimensions(product.name)
            pack = derive_pack(product.name, dims, product.category)
            new_unit_price = unit_price(product.price, pack)
            before = (
                product.length_mm, product.width_mm, product.thickness_mm,
                product.pack_value, product.pack_unit, product.unit_price,
            )
            after = (
                dims.length_mm, dims.width_mm, dims.thickness_mm,
                pack.value if pack else None, pack.unit if pack else None, new_unit_price,
            )
            if before != after:
                changed += 1
            (
                product.length_mm, product.width_mm, product.thickness_mm,
                product.pack_value, product.pack_unit, product.unit_price,
            ) = after
            product.raw_dimensions_text = dims.raw_text
        session.commit()
        logger.info("пересчёт: изменено %d из %d товаров, скрыто с некорректной ценой %d", changed, len(products), hidden)
    finally:
        session.close()


def run_all() -> None:
    for slug in SCRAPERS:
        try:
            run_store(slug)
        except ScrapeInProgress as exc:
            logger.warning("[%s] пропущен: %s", slug, exc)
        except Exception:  # магазин может быть временно недоступен
            logger.exception("[%s] прогон не удался", slug)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Обновить цены из магазинов")
    parser.add_argument("--store", choices=list(SCRAPERS.keys()), help="Обновить только один магазин")
    parser.add_argument("--all", action="store_true", help="Обновить все магазины")
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="Пересчитать размеры и фасовку для уже сохранённых товаров без повторного обхода сайтов",
    )
    args = parser.parse_args()

    init_db()

    if args.store:
        run_store(args.store)
    elif args.all:
        run_all()
    elif args.recompute:
        recompute_derived()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
