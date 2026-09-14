from urllib.parse import urljoin

from bs4 import BeautifulSoup

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


class AlterraScraper(BaseScraper):
    slug = "alterra"
    name = "ТГ-Алтерра"
    base_url = "https://tg-alterra.ru"

    def __init__(self, category_paths: list[str] | None = None):
        super().__init__()
        self._explicit_category_paths = category_paths
        self._pagination_blocked = False

    def _discover_category_paths(self) -> list[str]:
        response = self._get(f"{self.base_url}/")
        soup = BeautifulSoup(response.text, "lxml")

        paths: list[str] = []
        seen: set[str] = set()
        for item in soup.select("li.catalog-nav__item"):
            top_link = item.select_one("a.catalog-nav__item-link")
            if not top_link:
                continue
            top_href = top_link.get("href", "")
            if not top_href or any(part in top_href for part in EXCLUDED_TOP_HREF_PARTS):
                continue

            for a in item.select("ul.drop-nav__list a[href]"):
                href = a.get("href", "")
                if href and href not in seen:
                    seen.add(href)
                    paths.append(href)

        return paths

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
        category_name: str | None = None
        seen_urls: set[str] = set()

        for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
            url = base if page == 1 else f"{base}?PAGEN_1={page}"
            try:
                response = self._get(url)
            except DisallowedByRobots:
                # Ожидаемо для страниц 2+: сайт запрещает индексацию параметра пагинации.
                self._pagination_blocked = True
                break
            except Exception:
                logger.exception("не удалось загрузить %s", url)
                break

            soup = BeautifulSoup(response.text, "lxml")
            if category_name is None:
                heading = soup.select_one("h1")
                category_name = heading.get_text(strip=True) if heading else path

            page_products = self._parse_cards(soup, category_name)
            new_products = [p for p in page_products if p.url not in seen_urls]
            if not new_products:
                break
            seen_urls.update(p.url for p in new_products)
            products.extend(new_products)

        return products

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

            products.append(
                ProductRecord(
                    name=name,
                    url=urljoin(self.base_url, href),
                    price=price,
                    # Сайт не публикует единицу продажи; выдумывать "шт" для мешков и
                    # рулонов нельзя — фасовка считается из названия (app/matching.py).
                    unit=None,
                    category=category_name,
                )
            )
        return products
