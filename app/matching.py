"""Разбор текста: размеры товара, фасовка, цена, поисковый запрос и строки сметы.

Все парсеры собраны в одном модуле сознательно: раньше количество из строки сметы
разбирал JS на фронте, а размеры — Python на бэкенде, и они противоречили друг другу
на одном и том же синтаксисе ("Уголок 50 х 50" фронт читал как «50 штук уголка»).
Теперь источник правды один, и он покрыт тестами (tests/test_matching.py).
"""

import math
import re
from dataclasses import dataclass

# Матчит "2750х1830х16", "2750x1830x16", "2750*1830*16", "2750×1830×16", "2750 x 1830 x 16 мм",
# а также метрический формат листов "1,22*2,44*16" (длина/ширина в метрах, толщина в мм)
# Разделители: латинская x, кириллическая х/Х, "*" и знак умножения "×" (U+00D7)
_TRIPLE_RE = re.compile(
    r"(\d{1,5}(?:[.,]\d+)?)\s*[xхХX*×]\s*(\d{1,5}(?:[.,]\d+)?)\s*[xхХX*×]\s*(\d{1,4}(?:[.,]\d+)?)\s*(?:мм|mm)?"
)
# Матчит "16 мм" отдельно (толщина без габаритов листа)
_SINGLE_MM_RE = re.compile(r"(\d{1,4}(?:[.,]\d+)?)\s*(?:мм|mm)\b")
# Матчит "1,22х2,44м" / "1220x2440" — только длину и ширину, без толщины в этой же группе
# (толщина у части магазинов указывается отдельным словом, напр. "16мм 1,22х2,44м")
_DOUBLE_RE = re.compile(r"(\d{1,5}(?:[.,]\d+)?)\s*[xхХX*×]\s*(\d{1,5}(?:[.,]\d+)?)\s*(?:м\b|m\b)?")

# Ниже этого значения длина/ширина считается указанной в метрах и переводится в мм
_METERS_THRESHOLD = 20

# Слова, которыми пользователь описывает размер словами ("толщиной 16 мм"), а не числом —
# в названиях товаров таких слов обычно нет, поэтому после извлечения размера их убираем
# из ключевых слов, иначе поиск по названию товара ничего не найдёт.
_DESCRIPTOR_WORDS_RE = re.compile(
    r"\b(толщин\w*|длин\w*|ширин\w*|размер\w*|габарит\w*)\b", re.IGNORECASE
)


@dataclass
class Dimensions:
    length_mm: float | None = None
    width_mm: float | None = None
    thickness_mm: float | None = None
    raw_text: str | None = None

    def is_empty(self) -> bool:
        return self.length_mm is None and self.width_mm is None and self.thickness_mm is None


def _to_float(s: str) -> float:
    return float(s.replace(",", "."))


def _normalize_to_mm(value: float) -> float:
    return value * 1000 if value < _METERS_THRESHOLD else value


def extract_dimensions(text: str) -> Dimensions:
    match = _TRIPLE_RE.search(text)
    if match:
        return Dimensions(
            length_mm=_normalize_to_mm(_to_float(match.group(1))),
            width_mm=_normalize_to_mm(_to_float(match.group(2))),
            thickness_mm=_to_float(match.group(3)),
            raw_text=match.group(0).strip(),
        )
    thickness_match = _SINGLE_MM_RE.search(text)
    double_match = _DOUBLE_RE.search(text)
    if double_match and thickness_match:
        # Если "толщина" и пара чисел пересекаются в тексте — это одно и то же число,
        # найденное дважды двумя разными регэкспами (например, "Бур 10х1000мм": 1000 —
        # это длина сверла, а не отдельная толщина листа). Такой комбинации не доверяем.
        overlap = thickness_match.start() < double_match.end() and double_match.start() < thickness_match.end()
        if overlap:
            # Неоднозначному совпадению не доверяем целиком: у "Бур 10х1000мм" 1000 — это
            # длина сверла, и записывать её толщиной (лист толщиной в метр) тоже нельзя.
            return Dimensions()
        raw_parts = sorted([double_match.group(0), thickness_match.group(0)], key=lambda s: text.index(s))
        return Dimensions(
            length_mm=_normalize_to_mm(_to_float(double_match.group(1))),
            width_mm=_normalize_to_mm(_to_float(double_match.group(2))),
            thickness_mm=_to_float(thickness_match.group(1)),
            raw_text=" ".join(part.strip() for part in raw_parts),
        )
    if thickness_match:
        return Dimensions(thickness_mm=_to_float(thickness_match.group(1)), raw_text=thickness_match.group(0).strip())
    return Dimensions()


def _fmt_num(value: float) -> str:
    return f"{value:g}"


def format_dimensions(length_mm: float | None, width_mm: float | None, thickness_mm: float | None) -> str | None:
    """Единый вид размеров для отображения, независимо от того, как их написал магазин-источник."""
    if length_mm is not None and width_mm is not None and thickness_mm is not None:
        return f"{_fmt_num(length_mm)}×{_fmt_num(width_mm)}×{_fmt_num(thickness_mm)} мм"
    if length_mm is not None and width_mm is not None:
        return f"{_fmt_num(length_mm)}×{_fmt_num(width_mm)} мм"
    if thickness_mm is not None:
        return f"{_fmt_num(thickness_mm)} мм"
    return None


# --- Фасовка и цена за единицу ------------------------------------------------
# Без этого сравнение "где дешевле" бессмысленно: пакет смеси 2 кг за 154 ₽ выглядит
# выгоднее мешка 50 кг за 580 ₽, хотя стоит 77 ₽/кг против 11,6 ₽/кг.

MIN_PRICE = 0.01
MAX_PRICE = 10_000_000.0
# Ниже этой стороны пара чисел в названии — это профиль/сверло/уголок, а не лист,
# и переводить их в площадь нельзя ("Уголок 50х50х5" — не 0,0025 м²).
MIN_SHEET_SIDE_MM = 300.0
# А выше этой толщины три числа — это габарит коробки (унитаз 670×360×800, щит 310×300×170),
# и его площадь пола к цене за м² отношения не имеет.
MAX_SHEET_THICKNESS_MM = 60.0

# (фрагмент регэкспа единицы, каноническая единица, множитель к канонической).
# Порядок = приоритет: если в названии есть и вес, и количество штук
# ("цемент 50кг /паллет 30шт/"), берём вес — по нему и сравнивают цену.
# Диапазоны правдоподобия. Каталог полон чисел, которые выглядят как фасовка, но ею не
# являются: "45гр." — это градусы, "ЛУГА 12040л" — артикул диска, "1/2 Г/Ц" — резьба.
# Значение вне диапазона отбрасывается, а не превращается в 1 300 000 ₽/кг.
_PACK_RANGES: dict[str, tuple[float, float]] = {
    "кг": (0.05, 2000.0),
    "л": (0.05, 1000.0),
    "м": (0.1, 500.0),
    "м²": (0.05, 200.0),
    "м³": (0.001, 100.0),
}

# (фрагмент регэкспа единицы, каноническая единица, множитель, нужен ли пробел перед единицей)
_PACK_PATTERNS: list[tuple[str, str, float, bool]] = [
    (r"м2|м²|кв\.?\s*м", "м²", 1.0, False),
    (r"м3|м³|куб\.?\s*м", "м³", 1.0, False),
    (r"кг", "кг", 1.0, False),
    (r"тн|тонн\w*", "кг", 1000.0, False),
    # Граммы только через пробел ("3+3г" и "0,980гр." — не фасовка) и не перед слэшем
    # ("Кран шаровый 50 Г/Ш" — это тип резьбы, а не 50 граммов)
    (r"гр?(?!\s*/)|грамм\w*", "кг", 0.001, True),
    (r"мл", "л", 0.001, False),
    (r"л|литр\w*", "л", 1.0, False),
    (r"пог\.?\s*м|п\.?\s*м|мп|м", "м", 1.0, False),
]
# Единицы штук сознательно не считаем фасовкой: по названию не отличить "в упаковке 300 шт"
# (цена за упаковку) от "(300шт)" в описании при цене за штуку — а ошибка здесь сразу
# рисует бейдж "Лучшая цена" не тому товару.
_PACK_COMPILED = [
    # (?<![\w.,/]) — чтобы не выхватывать хвост чужого числа: в "1,22х2,44м" это не "44 м",
    # а в "Трос (200/250м)" перечислены длины бухт, а не фасовка
    (
        re.compile(rf"(?<![\w.,/])(\d+(?:[.,]\d+)?){'\\s+' if needs_space else r'\s*'}(?:{frag})(?![\w])", re.IGNORECASE),
        unit,
        factor,
    )
    for frag, unit, factor, needs_space in _PACK_PATTERNS
]


@dataclass
class Pack:
    """Фасовка товара: сколько канонических единиц в одной продаваемой позиции."""

    value: float
    unit: str

    def format(self) -> str:
        return f"{_fmt_num(self.value)} {self.unit}"


# "Гвозди 3*80 вес" — так Строймир помечает товар, который продаётся на вес: цена за 1 кг.
# Отдельное слово, не часть "Вестанвинд"/"Вессель" (бренды) и не "вес 4,3кг" (характеристика
# пилы — там число после слова).
_SOLD_BY_WEIGHT_RE = re.compile(r"\b(?:на\s+вес|вес|весов\w*)\b(?!\s*[:=]?\s*\d)", re.IGNORECASE)


def sold_by_weight(text: str) -> bool:
    return bool(_SOLD_BY_WEIGHT_RE.search(text))


def _plausible(value: float, unit: str) -> bool:
    low, high = _PACK_RANGES[unit]
    return low <= value <= high


def extract_pack(text: str) -> Pack | None:
    """Фасовка из названия: "цемент ... 50кг" -> 50 кг, "грунтовка 10 л" -> 10 л."""
    for pattern, unit, factor in _PACK_COMPILED:
        for match in pattern.finditer(text):
            value = round(_to_float(match.group(1)) * factor, 4)
            if _plausible(value, unit):
                return Pack(value=value, unit=unit)
    # Явной фасовки нет, но товар помечен как весовой — цена указана за килограмм
    if sold_by_weight(text):
        return Pack(value=1.0, unit="кг")
    return None


# Категории, где число с единицей веса — характеристика товара, а не фасовка:
# "Колун в сборе (3,6кг)" весит 3,6 кг, но продаётся штукой, и 791 ₽/кг о нём ничего
# не говорит. Инструмент и инвентарь продаются поштучно, поэтому фасовку из их
# названий не извлекаем вовсе.
_UNIT_LESS_CATEGORY_RE = re.compile(r"инструмент|instrument|инвентар", re.IGNORECASE)
# Раздел вроде "Инструменты, хозтовары, крепеж" — смешанный: гвозди и саморезы в нём
# продаются килограммами. Такой сегмент сам по себе ничего не решает.
_MIXED_SECTION_RE = re.compile(r"крепеж|крепёж|хозтовар|стройматериал", re.IGNORECASE)


def category_sold_by_piece(category: str | None) -> bool:
    """Категория вида "Раздел / Подкатегория": решает любой сегмент, который про
    инструмент и при этом не смешанный. "Инструменты, хозтовары, крепеж / Гвозди" -> нет,
    "Инструменты, хозтовары, крепеж / Слесарно-столярный инструмент" -> да."""
    if not category:
        return False
    for segment in re.split(r"\s*/\s*", category):
        if _UNIT_LESS_CATEGORY_RE.search(segment) and not _MIXED_SECTION_RE.search(segment):
            return True
    return False


def _looks_like_sheet(dims: Dimensions) -> bool:
    if not dims.length_mm or not dims.width_mm:
        return False
    if min(dims.length_mm, dims.width_mm) < MIN_SHEET_SIDE_MM:
        return False
    # Толщина обязательна: без неё "900×900" может быть чем угодно, включая душевую кабину
    return dims.thickness_mm is not None and dims.thickness_mm <= MAX_SHEET_THICKNESS_MM


def derive_pack(text: str, dims: Dimensions, category: str | None = None) -> Pack | None:
    """Фасовка из названия, а для листовых материалов — площадь из распознанных габаритов
    (ДСП 1220×2440 продаётся листами, но сравнивают его по цене за м²)."""
    if category_sold_by_piece(category):
        return None
    pack = extract_pack(text)
    if pack is not None:
        return pack
    if _looks_like_sheet(dims):
        area = round((dims.length_mm / 1000) * (dims.width_mm / 1000), 4)
        if _plausible(area, "м²"):
            return Pack(value=area, unit="м²")
    return None


def unit_price(price: float, pack: Pack | None) -> float | None:
    if pack is None or pack.value <= 0:
        return None
    return round(price / pack.value, 2)


_PRICE_TOKEN_RE = re.compile(r"\d[\d\s ]*(?:[.,]\d+)?")


def parse_price(text: str) -> float:
    """Первое число из текста цены.

    Раньше из строки вырезались все нецифровые символы, и "от 900 до 1 200 ₽"
    превращалось в 9001200 — склейку двух чисел. Берём именно первое число
    и проверяем его на вменяемость.
    """
    match = _PRICE_TOKEN_RE.search(text or "")
    if not match:
        raise ValueError(f"не удалось разобрать цену из {text!r}")
    token = match.group(0).replace(" ", "").replace(" ", "").strip()

    if re.fullmatch(r"\d+[.,]\d{1,2}", token):
        value = float(token.replace(",", "."))  # "1250,50" — копейки
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", token):
        value = float(token.replace(",", "").replace(".", ""))  # "1.250" — разделитель тысяч
    else:
        value = float(token.replace(",", "").replace(".", ""))

    return validate_price(value, text)


def validate_price(value: float, source: str = "") -> float:
    """Цена 0 в каталоге означает «цена по запросу», а не «бесплатно»: такой товар
    получал бейдж «Лучшая цена». Отсекаем на входе, а не в выдаче."""
    if not math.isfinite(value):
        raise ValueError(f"нечисловая цена (NaN/inf): {source!r}")
    if not (MIN_PRICE <= value <= MAX_PRICE):
        raise ValueError(f"цена вне разумных границ: {value} ({source!r})")
    return round(value, 2)


# --- Поисковый запрос ---------------------------------------------------------


@dataclass
class ParsedQuery:
    keywords: list[str]
    dimensions: Dimensions

    def is_empty(self) -> bool:
        return not self.keywords and self.dimensions.is_empty()


def parse_query(query: str) -> ParsedQuery:
    dims = extract_dimensions(query)
    keywords = query
    if dims.raw_text:
        keywords = keywords.replace(dims.raw_text, " ")
    keywords = _DESCRIPTOR_WORDS_RE.sub(" ", keywords)
    # Ключевые слова всегда в нижнем регистре: search_text в базе лоуеркейснут Python-ом
    # (SQLite умеет lower() только для ASCII), и без этого запрос "Цемент" с большой буквы
    # не совпадал с началом названия — вся выдача уезжала в "возможно, вас заинтересует".
    return ParsedQuery(keywords=keywords.lower().split(), dimensions=dims)


def like_escape(term: str) -> str:
    """Экранирует спецсимволы LIKE: без этого запрос "100%_" превращался в маску,
    совпадающую с любым товаром."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Строки сметы -------------------------------------------------------------

# Единицы измерения КОЛИЧЕСТВА (не путать с мм/см — это единицы размера товара,
# они остаются частью названия и разбираются extract_dimensions).
_QTY_UNIT_WORDS = (
    r"шт\w*|компл(?:ект\w*)?|уп(?:ак\w*)?|меш(?:ок|ка|ков)?|лист(?:ов|а)?|рулон(?:ов|а)?"
    r"|пач(?:ка|ек|ки)?|кг|гр?|тн|тонн\w*|мл|л|м2|м3|м²|м³|кв\.?\s?м|куб\.?\s?м|пог\.?\s?м|мп|м"
)
# "Название - ед - кол", "Название, кол, ед", "Название   кол   ед" (вставка из Excel):
# число в конце строки рядом с распознанной единицей количества, в любом порядке.
_QTY_TAIL_RE = re.compile(
    r"^(.*?)[\s,;:\-–—]+(?:(" + _QTY_UNIT_WORDS + r")\.?[\s,;:\-–—]*)?"
    r"(\d+(?:[.,]\d+)?)[\s,;:\-–—]*(?:(" + _QTY_UNIT_WORDS + r")\.?)?\s*$",
    re.IGNORECASE,
)
# Явный формат "название x5". Между "x" и числом пробела быть не должно — иначе
# "Уголок 50 х 50" (это размер!) читался как «50 штук уголка».
_QTY_X_RE = re.compile(r"^(.*?)\s[xх×](\d+(?:[.,]\d+)?)\s*$", re.IGNORECASE)


@dataclass
class EstimateLine:
    query: str
    qty: float


def parse_estimate_line(line: str) -> EstimateLine | None:
    line = line.strip()
    if not line:
        return None

    match = _QTY_X_RE.match(line)
    if match:
        return EstimateLine(query=match.group(1).strip(), qty=_to_float(match.group(2)))

    match = _QTY_TAIL_RE.match(line)
    if match and (match.group(2) or match.group(4)):
        qty = _to_float(match.group(3))
        query = match.group(1).strip()
        if query and qty > 0:
            return EstimateLine(query=query, qty=qty)

    # количество не распознано — считаем 1 шт., в поиск уходит вся строка как есть
    return EstimateLine(query=line, qty=1)


def parse_estimate_text(text: str) -> list[EstimateLine]:
    parsed = (parse_estimate_line(line) for line in (text or "").splitlines())
    return [line for line in parsed if line is not None and line.query]
