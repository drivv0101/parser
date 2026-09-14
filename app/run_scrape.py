import argparse
from datetime import UTC, datetime

from app.db import get_session, init_db
from app.logging_config import get_logger, setup_logging
from app.matching import derive_pack, extract_dimensions, unit_price, validate_price
from app.models import Product, Store
from app.scrapers import SCRAPERS

logger = get_logger("run_scrape")

# Если прогон принёс меньше этой доли от уже известных товаров магазина — считаем,
# что сломалась разметка сайта, и НЕ помечаем "пропавшие" товары как исчезнувшие.
MIN_COVERAGE_RATIO = 0.5


class ScrapeFailed(Exception):
    """Прогон не дал пригодных данных — база не изменена."""


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
    product.is_active = True
    product.scraped_at = now


def run_store(slug: str) -> int:
    """Обновляет каталог одного магазина. Возвращает число сохранённых товаров."""
    scraper_cls = SCRAPERS[slug]
    scraper = scraper_cls()
    logger.info("[%s] обход %s ...", slug, scraper.base_url)
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

        known_before = len(existing_by_url)
        seen_urls: set[str] = set()
        for record in valid_records:
            product = existing_by_url.get(record.url)
            if product is None:
                product = Product(store_id=store.id, url=record.url)
                session.add(product)
                existing_by_url[record.url] = product
            _apply_record(product, record, now)
            seen_urls.add(record.url)

        disappeared = [p for url, p in existing_by_url.items() if url not in seen_urls and p.is_active]
        if known_before and len(seen_urls) < known_before * MIN_COVERAGE_RATIO:
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

        store.last_scraped_at = now
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
