import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.logging_config import get_logger
from app.matching import validate_price
from app.scrapers.base import BaseScraper, DisallowedByRobots, ProductRecord

logger = get_logger("scrapers.alterra")

# Ранний комментарий здесь утверждал, что robots.txt сайта запрещает параметр пагинации
# Bitrix (PAGEN_1), и собиралась только первая страница каждой подкатегории. Когда проверку
# robots.txt перенесли из комментария в код (BaseScraper.can_fetch), выяснилось, что
# пагинация разрешена — и каталог вырос с 4 662 до 15 739 товаров. Обходим мелкие
# подкатегории (их ~400) с полной пагинацией; если сайт когда-нибудь запретит PAGEN_1,
# can_fetch() остановит листание сам, а лог это отметит.
# Разделы, исключённые как не относящиеся к стройматериалам:
EXCLUDED_TOP_HREF_PARTS = ["sad-i-ogorod", "gotovye-resheniya-dlya-dachi", "/shop/stocks/"]
MAX_PAGES_PER_CATEGORY = 50


# Сеть работает в нескольких городах, и без cookie сайт показывает Барнаул: барнаульские
# остатки И барнаульские цены (у 6 цементов из 7 они отличаются от бийских). Первый прогон
# без этой cookie собрал 15 739 барнаульских цен под видом бийских.
CITY_COOKIE = {"BITRIX_SM_torgzal": "biysk"}
CITY_NAME = "Бийск"

_QTY_RE = re.compile(r"([><]?)\s*(\d+)\s*([а-яa-z.]*)", re.IGNORECASE)


class AlterraScraper(BaseScraper):
    slug = "alterra"
    name = "ТГ-Алтерра"
    base_url = "https://tg-alterra.ru"
    cookies = CITY_COOKIE

    def __init__(self, category_paths: list[str] | None = None):
        super().__init__()
        self._explicit_category_paths = category_paths
        self._pagination_blocked = False
        # путь подкатегории -> "Раздел / Подкатегория" из меню сайта
        self._category_names: dict[str, str] = {}

    @staticmethod
    def parse_menu(soup: BeautifulSoup) -> dict[str, str]:
        """Пути подкатегорий и их названия из меню каталога.

        Название берём из текста ссылки в меню, а не из <h1> страницы: у большинства
        страниц подкатегорий на этом сайте заголовка нет, а у остальных <h1> — это
        заголовок SEO-статьи ("Арматура, круг, квадрат: виды, характеристики...").
        Раздел-родитель сохраняем ("Инструмент / Топоры"): по нему отличаем товар,
        который продаётся штуками, от весового.
        """
        names: dict[str, str] = {}
        for item in soup.select("li.catalog-nav__item"):
            top_link = item.select_one("a.catalog-nav__item-link")
            if not top_link:
                continue
            top_href = top_link.get("href", "")
            if not top_href or any(part in top_href for part in EXCLUDED_TOP_HREF_PARTS):
                continue
            top_name = top_link.get_text(" ", strip=True)

            for a in item.select("ul.drop-nav__list a[href]"):
                href = a.get("href", "")
                sub_name = a.get_text(" ", strip=True)
                if href and href not in names:
                    names[href] = f"{top_name} / {sub_name}" if top_name and sub_name else (sub_name or top_name or href)
        return names

    def _discover_category_paths(self) -> list[str]:
        response = self._get(f"{self.base_url}/")
        self._category_names = self.parse_menu(BeautifulSoup(response.text, "lxml"))
        return list(self._category_names)

    def fetch_products(self) -> list[ProductRecord]:
        category_paths = self._explicit_category_paths or self._discover_category_paths()
        logger.info("найдено %d категорий для обхода", len(category_paths))

        products: list[ProductRecord] = []
        seen_urls: set[str] = set()
        for number, path in enumerate(category_paths, start=1):
            for record in self._fetch_category(path):
                if record.url not in seen_urls:
                    seen_urls.add(record.url)
                    products.append(record)
            self.on_progress(number, len(category_paths), len(products))
            # Обход 400 подкатегорий занимает десятки минут: без отметок прогресса
            # непонятно, идёт работа или процесс завис.
            if number % 25 == 0 or number == len(category_paths):
                logger.info("пройдено %d из %d категорий, товаров: %d", number, len(category_paths), len(products))
        if self._pagination_blocked:
            logger.info("пагинация запрещена robots.txt — по каждой подкатегории собрана только первая страница")
        return products

    def _fetch_category(self, path: str) -> list[ProductRecord]:
        base = urljoin(self.base_url, path)
        products: list[ProductRecord] = []
        # Имя из меню; для путей, переданных вручную (без обхода меню), — <h1> страницы или путь
        category_name: str | None = self._category_names.get(path)
        seen_urls: set[str] = set()

        for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
            url = base if page == 1 else f"{base}?PAGEN_1={page}"
            try:
                response = self._get(url)
            except DisallowedByRobots:
                # Ожидаемо для страниц 2+: сайт запрещает индексацию параметра пагинации.
                self.mark_incomplete(url)
                self._pagination_blocked = True
                break
            except Exception:
                self.mark_incomplete(url)
                logger.exception("не удалось загрузить %s", url)
                break

            soup = BeautifulSoup(response.text, "lxml")
            if category_name is None:
                heading = soup.select_one("h1")
                category_name = heading.get_text(strip=True) if heading else path

            page_products = self._parse_cards(soup, category_name)
            new_products = [p for p in page_products if p.url not in seen_urls]
            if not new_products:
                if page == 1:
                    self.mark_incomplete(url + ": no products")
                break
            seen_urls.update(p.url for p in new_products)
            products.extend(new_products)
        else:
            self.mark_incomplete(base + ": page limit")

        return products

    @staticmethod
    def parse_availability(card: Tag) -> tuple[bool | None, str | None]:
        """Наличие в Бийске из блока карточки:

            <div class="availability availability_in tip"><span class="tip__name">В наличии</span>
              <ul class="tip__vlist"><li><span>Бийск, пер. Шубенский, 75</span><span><strong>20 уп</strong></span></li>…

        Считаем остаток по строкам с бийскими адресами (с cookie города сайт показывает
        только их, но проверяем адрес на случай смены поведения). ">100" читаем как 100.
        """
        block = card.select_one("div.availability")
        if block is None:
            return None, None

        total = 0
        unit = ""
        rows = 0
        for row in block.select("ul.tip__vlist li"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all("span", recursive=False)]
            if len(cells) < 2 or CITY_NAME not in cells[0]:
                continue
            match = _QTY_RE.search(cells[1])
            if not match:
                continue
            rows += 1
            total += int(match.group(2))
            unit = unit or match.group(3)

        if rows:
            if total > 0:
                return True, f"в наличии: {total}{'+' if total >= 100 else ''} {unit}".strip()
            return False, "под заказ"

        # Таблицы нет — судим по подписи блока
        label = block.select_one("span.tip__name")
        text = label.get_text(" ", strip=True).lower() if label else ""
        classes = " ".join(block.get("class", []))
        if "availability_in" in classes or text.startswith("в наличии"):
            return True, text or "в наличии"
        if text:
            return False, text
        return None, None

    def _parse_cards(self, soup: BeautifulSoup, category_name: str | None) -> list[ProductRecord]:
        products: list[ProductRecord] = []
        for card in soup.select("div.card__wrapper"):
            link = card.select_one("a.card__main")
            if not link:
                continue
            title = link.select_one("span.card__title")
            if not title:
                continue
            name = next(title.stripped_strings, "").strip()
            href = link.get("href", "")
            if not name or not href:
                continue

            price_input = card.select_one('input[id^="price-nocard"]')
            if not price_input or not price_input.get("value"):
                continue
            try:
                # "price-nocard" — витринная цена без карты магазина; личная скидка
                # пользователя учитывается отдельно, на стороне приложения.
                price = validate_price(float(price_input["value"]), name)
            except ValueError:
                logger.debug("пропущен товар с некорректной ценой: %s", name)
                continue

            in_stock, stock_note = self.parse_availability(card)
            products.append(
                ProductRecord(
                    name=name,
                    url=urljoin(self.base_url, href),
                    price=price,
                    # Сайт не публикует единицу продажи; выдумывать "шт" для мешков и
                    # рулонов нельзя — фасовка считается из названия (app/matching.py).
                    unit=None,
                    category=category_name,
                    in_stock=in_stock,
                    stock_note=stock_note,
                )
            )
        return products
