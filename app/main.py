import json
import math
import os
import secrets
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, joinedload

from app.db import get_session, init_db
from app.logging_config import get_logger, setup_logging
from app.matching import (
    ParsedQuery,
    format_dimensions,
    like_escape,
    parse_estimate_text,
    parse_query,
    sold_by_weight,
    material_signature,
)
from app.models import Product, Store, PriceHistory, ScrapeRun
from app.run_scrape import run_all, run_store
from app.scrapers import SCRAPERS

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SCRAPE_INTERVAL_HOURS = int(os.getenv("STROY_SCRAPE_INTERVAL_HOURS", "12"))
# Автообновление можно выключить на время тестов: STROY_AUTO_SCRAPE=0
AUTO_SCRAPE_ENABLED = os.getenv("STROY_AUTO_SCRAPE", "1") != "0"
# Токен для ручного запуска обхода. Не задан — эндпоинт доступен только с localhost.
ADMIN_TOKEN = os.getenv("STROY_ADMIN_TOKEN")
REFRESH_COOLDOWN_SECONDS = 600
MAX_QUERY_LEN = 200
MAX_ESTIMATE_ITEMS = 200
# Во сколько раз фасовка может превышать типичную, чтобы её ещё подставляли в смету
MAX_PACK_RATIO = 5.0

setup_logging()
logger = get_logger("main")

scheduler = BackgroundScheduler()


def scrape_is_due(session: Session, interval_hours: int, now: datetime | None = None) -> bool:
    """База пуста или старше интервала обновления — обход нужен прямо сейчас, а не через
    interval_hours после старта. Иначе свежий сервер полдня работает без данных, а после
    каждого перезапуска отсчёт начинается заново."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    stores = session.query(Store).all()
    if len(stores) < len(SCRAPERS):
        return True
    for store in stores:
        if store.last_scraped_at is None:
            return True
        if (now - store.last_scraped_at).total_seconds() >= interval_hours * 3600:
            return True
    return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if AUTO_SCRAPE_ENABLED:
        session = get_session()
        try:
            due = scrape_is_due(session, SCRAPE_INTERVAL_HOURS)
        finally:
            session.close()
        # next_run_time=None у APScheduler означает "задача на паузе", поэтому параметр
        # передаём только когда обход нужен сразу; иначе первый запуск — через интервал.
        extra = {"next_run_time": datetime.now()} if due else {}
        scheduler.add_job(run_all, "interval", hours=SCRAPE_INTERVAL_HOURS, id="scrape_all", **extra)
        scheduler.start()
        logger.info(
            "Приложение запущено, фоновое обновление каждые %d ч.%s",
            SCRAPE_INTERVAL_HOURS, " Данные устарели — обход начинается сразу." if due else "",
        )
    else:
        logger.info("Приложение запущено, автообновление ВЫКЛЮЧЕНО (STROY_AUTO_SCRAPE=0)")
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Стройцены Бийск", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"error": "internal_error"})
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    logger.info("%s %s?%s -> %d (%sms)", request.method, request.url.path, request.url.query, response.status_code, elapsed_ms)
    return response


def _utc_iso(value: datetime | None) -> str | None:
    """В базе время лежит наивным UTC. Без явного смещения браузер считал его локальным
    и показывал "обновлено" на несколько часов раньше реального."""
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    """Для healthcheck в Docker/хостинге: приложение живо и база открывается."""
    session = get_session()
    try:
        active = session.query(Product).filter(Product.is_active.is_(True)).count()
        return {"status": "ok", "active_products": active, "auto_scrape": AUTO_SCRAPE_ENABLED}
    finally:
        session.close()


@app.get("/api/stores")
def list_stores() -> list[dict]:
    session = get_session()
    try:
        return [
            {"slug": s.slug, "name": s.name, "last_scraped_at": _utc_iso(s.last_scraped_at)}
            for s in session.query(Store).order_by(Store.name).all()
        ]
    finally:
        session.close()


# --- Поиск --------------------------------------------------------------------


class Matches:
    """Результат подбора товаров под один запрос."""

    def __init__(self, products: list[Product], parsed: ParsedQuery, relaxed: bool):
        self.products = products
        self.keywords = parsed.keywords
        self.dimensions = parsed.dimensions
        self.relaxed = relaxed  # искали не по всем словам запроса, а по первому


def _query_products(session: Session, keywords: list[str], parsed: ParsedQuery, limit: int) -> list[Product]:
    query = session.query(Product).options(joinedload(Product.store)).filter(Product.is_active.is_(True))
    for keyword in keywords:
        query = query.filter(Product.search_text.like(f"%{like_escape(keyword)}%", escape="\\"))

    dims = parsed.dimensions
    tolerance = 1.0
    if dims.length_mm is not None:
        query = query.filter(Product.length_mm.between(dims.length_mm - tolerance, dims.length_mm + tolerance))
    if dims.width_mm is not None:
        query = query.filter(Product.width_mm.between(dims.width_mm - tolerance, dims.width_mm + tolerance))
    if dims.thickness_mm is not None:
        query = query.filter(Product.thickness_mm.between(dims.thickness_mm - tolerance, dims.thickness_mm + tolerance))

    return query.order_by(Product.price.asc()).all()


def find_matches(session: Session, q: str, limit: int = 500) -> Matches:
    parsed = parse_query(q)
    if parsed.is_empty():
        return Matches([], parsed, relaxed=False)

    products = _query_products(session, parsed.keywords, parsed, limit)
    relaxed = False
    if not products and len(parsed.keywords) > 1:
        # Строка из сметы ("Кирпич керамический рядовой полнотелый М150") почти никогда
        # не совпадает с названием магазина целиком, а AND по всем словам давал пустоту.
        # Отступаем к первому слову — это тип товара — и ранжируем по числу совпавших слов.
        products = _query_products(session, parsed.keywords[:1], parsed, limit)
        relaxed = bool(products)
    return Matches(products, parsed, relaxed)


def _relevance_tier(product: Product, keywords: list[str]) -> int:
    """0 — искомое слово стоит в начале названия (это и есть тип товара, напр. "Цемент...").
    1 — слово встречается где-то в середине названия (может быть цветом, составом и т.п.,
    напр. "Наличник ... цемент светлый") — менее вероятно, что это именно искомый товар."""
    if not keywords:
        return 0
    return 0 if product.search_text.startswith(keywords[0]) else 1


def _matched_words(product: Product, keywords: list[str]) -> int:
    return sum(1 for word in keywords if word in product.search_text)


def dominant_pack_unit(products: list[Product]) -> str | None:
    """Единица, по которой товары этой выдачи вообще сопоставимы между собой.
    Сравнивать 77 ₽/кг с 300 ₽/шт бессмысленно, поэтому цена за единицу используется
    только внутри одной единицы измерения.

    Единица должна покрывать хотя бы половину выдачи: если из восьми колунов вес указан
    у двух, этот товар меряют штуками, а не килограммами, и сравнивать их по ₽/кг нельзя.
    """
    if not products:
        return None
    counts = Counter(p.pack_unit for p in products if p.pack_unit and p.unit_price)
    if not counts:
        return None
    unit, count = counts.most_common(1)[0]
    return unit if count >= 2 and count * 2 >= len(products) else None


def _clean_discounts(discounts: dict | None) -> dict[str, float]:
    """Скидки приходят от клиента (localStorage), ключ — slug магазина."""
    if not discounts or not isinstance(discounts, dict):
        return {}
    cleaned: dict[str, float] = {}
    for slug, percent in discounts.items():
        try:
            percent = float(percent)
        except (TypeError, ValueError):
            continue
        # NaN/Infinity пролезали в ответ и делали JSON невалидным для браузера.
        if not math.isfinite(percent):
            continue
        cleaned[str(slug)] = min(max(percent, 0), 90)  # разумные границы скидки
    return cleaned


def _effective_price(price: float, store_slug: str, discounts: dict[str, float]) -> float:
    percent = discounts.get(store_slug, 0)
    return round(price * (1 - percent / 100), 2)


MAX_CATEGORY_DISPLAY_LEN = 50


def _display_category(category: str | None) -> str | None:
    """Категория для карточки товара. Путь ("/catalog/gvozdi_1/") и заголовок SEO-статьи
    ("Арматура, круг, квадрат: виды, характеристики и применение") пользователю не нужны."""
    if not category:
        return None
    # "Раздел / Подкатегория" — показываем только подкатегорию, она конкретнее и короче
    category = category.strip().rsplit(" / ", 1)[-1].strip()
    if not category or category.startswith("/") or len(category) > MAX_CATEGORY_DISPLAY_LEN or ":" in category:
        return None
    return category


def _display_pack(product: Product) -> str | None:
    if not product.pack_value or not product.pack_unit:
        return None
    # "Гвозди 3*80 вес": фасовки нет, цена за килограмм — так и пишем, а не "1 кг"
    if product.pack_unit == "кг" and product.pack_value == 1 and sold_by_weight(product.name):
        return "на вес"
    return f"{product.pack_value:g} {product.pack_unit}"


def _product_to_dict(product: Product, discounts: dict[str, float]) -> dict:
    discount_percent = discounts.get(product.store.slug, 0)
    effective = _effective_price(product.price, product.store.slug, discounts)
    effective_unit_price = None
    if product.pack_value:
        effective_unit_price = round(effective / product.pack_value, 2)
    return {
        "store": product.store.name,
        "store_slug": product.store.slug,
        "name": product.name,
        "price": product.price,
        "discount_percent": discount_percent,
        "effective_price": effective,
        "unit": product.unit,
        "category": _display_category(product.category),
        "url": product.url,
        "dimensions": format_dimensions(product.length_mm, product.width_mm, product.thickness_mm),
        "pack": _display_pack(product),
        "pack_unit": product.pack_unit,
        "unit_price": effective_unit_price,
        "in_stock": product.in_stock,
        "stock_note": product.stock_note,
        "scraped_at": _utc_iso(product.scraped_at),
    }


def _availability_rank(product: Product) -> int:
    """0 — есть в Бийске (или сайт не сообщил), 1 — под заказ. Товар из другого города
    не должен выигрывать у того, что можно купить сегодня."""
    return 1 if product.in_stock is False else 0


def _best_index(items: list[dict], unit: str | None) -> int | None:
    """Индекс действительно самого выгодного предложения — по цене за единицу
    и только среди сопоставимых товаров. Без этого бейдж "Лучшая цена" получал
    пакет смеси 2 кг за 154 ₽ вместо мешка 50 кг за 580 ₽."""
    if not unit:
        return None
    # Только то, что есть в Бийске: "лучшая цена" на товар под заказ бесполезна
    candidates = [
        (i, it) for i, it in enumerate(items)
        if it["pack_unit"] == unit and it["unit_price"] and it["in_stock"] is not False
    ]
    if len(candidates) < 2:
        return None
    return min(candidates, key=lambda pair: pair[1]["unit_price"])[0]


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_LEN),
    discounts: str | None = None,
    store: str = "",
    category: str = "",
    stock_only: bool = False,
    pack_unit: str = "",
    min_price: float = 0,
    max_price: float | None = None,
    sort: str = "relevance",
    offset: int = 0,
    exact: bool = False,
) -> dict:
    parsed_discounts = None
    if discounts:
        try:
            parsed_discounts = json.loads(discounts)
        except (json.JSONDecodeError, TypeError):
            parsed_discounts = None
    discount_map = _clean_discounts(parsed_discounts)

    session = get_session()
    try:
        matches = find_matches(session, q, limit=500)
        if not matches.products and not matches.keywords and matches.dimensions.is_empty():
            raise HTTPException(status_code=422, detail="Запрос не содержит ни слов, ни размеров")

        keywords = matches.keywords
        if exact and matches.relaxed:
            matches.products = []
        matches.products = [p for p in matches.products
            if (not store or p.store.slug == store)
            and (not category or category.lower() in (p.category or "").lower())
            and (not stock_only or p.in_stock is True)
            and (not pack_unit or p.pack_unit == pack_unit)
            and _effective_price(p.price, p.store.slug, discount_map) >= min_price
            and (max_price is None or _effective_price(p.price, p.store.slug, discount_map) <= max_price)]
        ranked = sorted(
            ((_product_to_dict(p, discount_map), p) for p in matches.products),
            key=lambda pair: (
                _relevance_tier(pair[1], keywords),
                -_matched_words(pair[1], keywords),
                _availability_rank(pair[1]),
                pair[0]["effective_price"],
            ),
        )

        if sort in {"price", "unit_price"}:
            ranked.sort(key=lambda pair: (pair[0].get(sort if sort == "unit_price" else "effective_price") or float("inf")))
        offset = max(0, offset)
        main_ranked = [(it, p) for it, p in ranked if _relevance_tier(p, keywords) == 0]
        results = [it for it, p in main_ranked[offset:offset + 100]]
        maybe_also = [it for it, p in ranked if _relevance_tier(p, keywords) != 0][:20]
        # Запрос без слов ("16 мм") возвращает разнородный список: изолента, труба, кабель.
        # Сравнивать их между собой по ₽/м бессмысленно, поэтому бейджа там нет.
        unit = dominant_pack_unit([p for it, p in main_ranked]) if keywords else None
        # Разные названия (марки, бренды) — повод предупредить, а не отказаться сравнивать:
        # "где дешевле похожий товар" и есть задача сервиса, а марку человек видит в названии.
        mixed = len({material_signature(p) for it, p in main_ranked}) > 1
        return {
            "total": len(main_ranked),
            "has_more": offset + len(results) < len(main_ranked),
            "results": results,
            "maybe_also": maybe_also,
            "relaxed": matches.relaxed,
            "comparison_unit": unit,
            "mixed_materials": mixed,
            "best_index": _best_index(results, unit),
        }
    finally:
        session.close()


# --- Смета --------------------------------------------------------------------


class EstimateItem(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    query: str = Field(min_length=1, max_length=MAX_QUERY_LEN)
    qty: float = Field(default=1, gt=0, le=1_000_000)
    # Закреплённый пользователем товар: считаем по актуальной цене из базы, а не по
    # снимку, сохранённому в браузере месяц назад.
    url: str | None = None
    qty_unit: str = Field(default="уп", pattern="^(уп|кг|л|м|м²|м³)$")


class EstimateRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    items: list[EstimateItem] = Field(default_factory=list, max_length=MAX_ESTIMATE_ITEMS)
    discounts: dict[str, float] = Field(default_factory=dict)
    delivery: dict[str, float] = Field(default_factory=dict)


class ParseLinesRequest(BaseModel):
    text: str = Field(default="", max_length=20_000)


@app.post("/api/parse-lines")
def parse_lines(req: ParseLinesRequest) -> dict:
    """Разбор вставленной сметы на позиции. Живёт на бэкенде, чтобы количество и размеры
    разбирал один и тот же код (раньше JS на фронте читал "Уголок 50 х 50" как 50 штук)."""
    lines = parse_estimate_text(req.text)[:MAX_ESTIMATE_ITEMS]
    return {"items": [{"query": line.query, "qty": line.qty, "qty_unit": line.qty_unit, "needs_review": line.needs_review} for line in lines]}


def _pinned_product(session: Session, url: str) -> Product | None:
    return (
        session.query(Product)
        .options(joinedload(Product.store))
        .filter(Product.url == url, Product.is_active.is_(True))
        .first()
    )


def typical_pack_value(products: list[Product], unit: str | None) -> float | None:
    """Самая частая фасовка среди сопоставимых товаров — эталон «обычной упаковки».
    Возвращает значение, только если оно действительно повторяется: у плитки все площади
    разные, и объявлять "нетипичной" самую большую из них нельзя."""
    if not unit:
        return None
    counts = Counter(p.pack_value for p in products if p.pack_unit == unit and p.pack_value)
    if not counts:
        return None
    value, count = counts.most_common(1)[0]
    return value if count >= 2 else None


def _line_key(product: Product, keywords: list[str], unit: str | None, typical: float | None) -> tuple:
    """Порядок выбора товара под строку сметы: сначала релевантность, затем — при
    сопоставимых единицах — цена за единицу, и лишь потом цена за упаковку.

    Количество в смете считается в упаковках ("Цемент - меш - 40"), поэтому товар
    с фасовкой на порядок больше обычной (биг-бэг 1000 кг вместо мешка 50 кг) в
    автоподбор не идёт, даже если он выгоднее за килограмм: 40 биг-бэгов — не то,
    что имел в виду человек. Он остаётся в списке последним вариантом, если других нет.
    """
    comparable = bool(unit and product.pack_unit == unit and product.unit_price)
    oversized = bool(comparable and typical and product.pack_value > typical * MAX_PACK_RATIO)
    return (
        _relevance_tier(product, keywords),
        -_matched_words(product, keywords),
        _availability_rank(product),
        1 if oversized else 0,
        0 if comparable else 1,
        product.unit_price if comparable else product.price,
    )


@app.post("/api/estimate")
def estimate(req: EstimateRequest) -> dict:
    from app.estimates import calculate
    return calculate(req)


# --- Ручное обновление --------------------------------------------------------

_refresh_lock = threading.Lock()
_refresh_started_at: dict[str, float] = {}


def _require_admin(request: Request) -> None:
    if ADMIN_TOKEN:
        provided = request.headers.get("X-Admin-Token", "")
        if not secrets.compare_digest(provided, ADMIN_TOKEN):
            raise HTTPException(status_code=403, detail="Неверный токен")
        return
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(
            status_code=403,
            detail="Ручное обновление доступно только с localhost. Задайте STROY_ADMIN_TOKEN для удалённого запуска.",
        )


def _run_store_guarded(slug: str) -> None:
    try:
        run_store(slug)
    except Exception:
        logger.exception("[%s] ручное обновление не удалось", slug)
    finally:
        with _refresh_lock:
            _refresh_started_at[slug] = time.monotonic()


@app.post("/api/refresh/{slug}")
def refresh(slug: str, request: Request, background: BackgroundTasks) -> dict:
    if slug not in SCRAPERS:
        raise HTTPException(status_code=404, detail="Неизвестный магазин")
    _require_admin(request)

    # Обход чужого сайта занимает 5-15 минут: без ограничения любой желающий мог
    # запускать его в цикле и превратить приложение в источник паразитной нагрузки.
    with _refresh_lock:
        started = _refresh_started_at.get(slug)
        if started is not None and time.monotonic() - started < REFRESH_COOLDOWN_SECONDS:
            raise HTTPException(status_code=429, detail="Обновление этого магазина уже запускалось недавно")
        _refresh_started_at[slug] = time.monotonic()

    background.add_task(_run_store_guarded, slug)
    return {"status": "started", "store": slug}


@app.get("/api/scrape-status")
def scrape_status():
    session = get_session()
    try:
        result = []
        for slug in SCRAPERS:
            run = session.query(ScrapeRun).filter_by(store_slug=slug).order_by(ScrapeRun.id.desc()).first()
            result.append({"store_slug": slug, "status": run.status if run else "unknown",
                "started_at": _utc_iso(run.started_at) if run else None,
                "finished_at": _utc_iso(run.finished_at) if run else None,
                "done": run.completed_categories if run else 0,
                "total": run.total_categories if run else 0,
                "products": run.product_count if run else 0,
                "message": run.message if run else None})
        return result
    finally:
        session.close()


@app.get("/api/price-history")
def price_history(url: str):
    session = get_session()
    try:
        product = session.query(Product).filter_by(url=url).first()
        if not product:
            raise HTTPException(404, "Товар не найден")
        rows = session.query(PriceHistory).filter_by(product_id=product.id).order_by(PriceHistory.recorded_at.desc()).limit(365).all()
        values = [{"price": row.price, "date": _utc_iso(row.recorded_at)} for row in reversed(rows)]
        if not values:
            values = [{"price": product.price, "date": _utc_iso(product.scraped_at)}]
        return {"name": product.name, "active": product.is_active, "history": values}
    finally:
        session.close()


@app.get("/api/categories")
def categories():
    session = get_session()
    try:
        return [r[0] for r in session.query(Product.category).filter(Product.is_active.is_(True), Product.category.isnot(None)).distinct().order_by(Product.category).all()]
    finally:
        session.close()


@app.post("/api/export/xlsx")
def export_xlsx(req: EstimateRequest):
    from fastapi.responses import Response
    from app.exporting import workbook
    return Response(workbook(estimate(req)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="estimate.xlsx"'})
