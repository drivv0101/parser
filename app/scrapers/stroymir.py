from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.logging_config import get_logger
from app.matching import parse_price
from app.scrapers.base import BaseScraper, DisallowedByRobots, ProductRecord

logger = get_logger("scrapers.stroymir")

# Верхнеуровневые категории, которые считаем "стройматериалами" для целей этого приложения.
# Страница каждой такой категории на stroymir.su (OpenCart) агрегирует товары из ВСЕХ вложенных
# подкатегорий, поэтому достаточно пролистать эти 9 разделов — отдельно обходить листовые
# подкатегории (ДСП, фанера и т.п.) не нужно.
# Сознательно исключены: Товары для дома/интерьер, Сад и огород, Отдых и туризм,
# Корма для животных, Продукты питания, Товары для авто, Товары для животных — не стройматериалы.
CATEGORY_PATHS = [
    "/GR0056-instrumenti",
    "/GR0001-vnutrennyaya-otdelka",
    "/GR0199-santehnika-i-sanfayans",
    "/GR0235-stroitelnie-materiali",
    "/GR0123-osveschenie-i-elektrika",
    "/GR0088-krovli-i-fasadi",
    "/GR0165-otoplenie",
    "/GR0052-dveri",
    "/GR0114-metalloprokat-chyornij",
]


class StroymirScraper(BaseScraper):
    slug = "stroymir"
    name = "Строймир"
    base_url = "https://stroymir.su"

    def __init__(self, category_paths: list[str] | None = None, max_pages_per_category: int = 200):
        super().__init__()
        self.category_paths = category_paths if category_paths is not None else CATEGORY_PATHS
        self.max_pages_per_category = max_pages_per_category

    def fetch_products(self) -> list[ProductRecord]:
        products: list[ProductRecord] = []
        seen_urls: set[str] = set()
        for path in self.category_paths:
            for record in self._fetch_category(path):
                if record.url not in seen_urls:
                    seen_urls.add(record.url)
                    products.append(record)
        return products

    def _fetch_category(self, path: str) -> list[ProductRecord]:
        products: list[ProductRecord] = []
        page = 1
        category_name = None
        truncated = False
        while True:
            if page > self.max_pages_per_category:
                truncated = True
                break
            url = f"{self.base_url}{path}"
            if page > 1:
                url = f"{url}?page={page}"
            try:
                response = self._get(url)
            except DisallowedByRobots:
                logger.info("robots.txt запрещает %s — категория собрана частично", url)
                break
            except Exception:
                logger.exception("не удалось загрузить %s", url)
                break
            soup = BeautifulSoup(response.text, "lxml")

            if category_name is None:
                heading = soup.select_one("h1")
                category_name = heading.get_text(strip=True) if heading else path

            cards = soup.select("div.product-thumb")
            if not cards:
                break

            for card in cards:
                link = card.select_one(".caption h4 a")
                price_tag = card.select_one(".caption p.price")
                if not link or not price_tag:
                    continue
                name = link.get_text(strip=True)
                href = link.get("href", "")
                if not name or not href:
                    continue
                try:
                    price = parse_price(price_tag.get_text(strip=True))
                except ValueError:
                    # "Цена по запросу", 0 ₽ и прочее, что нельзя сравнивать.
                    logger.debug("пропущен товар с некорректной ценой: %s", name)
                    continue
                products.append(
                    ProductRecord(
                        name=name,
                        url=urljoin(self.base_url, href),
                        price=price,
                        unit=None,
                        category=category_name,
                    )
                )

            has_next = soup.select_one('link[rel="next"]') is not None
            if not has_next:
                break
            page += 1

        if truncated:
            logger.warning("%s: достигнут лимит в %d страниц", category_name or path, self.max_pages_per_category)
        logger.info("%s: %d товаров", category_name or path, len(products))
        return products
