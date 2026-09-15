import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from app.logging_config import get_logger

logger = get_logger("scrapers.base")

USER_AGENT = "StroyParserBot/1.0 (personal price comparison tool)"
DEFAULT_HEADERS = {"User-Agent": f"Mozilla/5.0 (compatible; {USER_AGENT})"}
REQUEST_TIMEOUT = 15
REQUEST_DELAY_SECONDS = 1.0


class DisallowedByRobots(Exception):
    """Запрошенный URL запрещён robots.txt сайта."""


@dataclass
class ProductRecord:
    name: str
    url: str
    price: float
    unit: str | None = None
    category: str | None = None
    # Наличие в Бийске: True — есть на складе/в магазине, False — под заказ или нет,
    # None — сайт не сообщил. stock_note — как это сформулировал магазин ("20 уп").
    in_stock: bool | None = None
    stock_note: str | None = None


class BaseScraper:
    slug: str
    name: str
    base_url: str
    # Cookie, которые нужны сайту, чтобы показывать нужный город (см. AlterraScraper)
    cookies: dict[str, str] = {}

    def __init__(self) -> None:
        self.complete = True
        self.errors: list[str] = []
        self.on_progress = lambda done, total, count: None
        self._robots: RobotFileParser | None = None
        self._robots_loaded = False
        self._last_request_at = 0.0

    def mark_incomplete(self, message: str) -> None:
        self.complete = False
        self.errors.append(message)

    def fetch_products(self) -> list[ProductRecord]:
        raise NotImplementedError

    # --- robots.txt ---------------------------------------------------------
    # Раньше правила robots.txt были только в комментариях к коду ("у Алтерры нельзя
    # PAGEN_1"), то есть держались на памяти автора. Теперь файл действительно читается.

    def _load_robots(self) -> RobotFileParser | None:
        if self._robots_loaded:
            return self._robots
        self._robots_loaded = True
        robots_url = urljoin(self.base_url, "/robots.txt")
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            response = requests.get(robots_url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
            if response.status_code >= 400:
                logger.info("[%s] robots.txt недоступен (%d) — считаем, что ограничений нет", self.slug, response.status_code)
                return None
            parser.parse(response.text.splitlines())
            self._robots = parser
            logger.info("[%s] robots.txt загружен", self.slug)
        except Exception:
            # Сеть недоступна/таймаут: по общепринятому правилу отсутствие robots.txt
            # не запрещает обход, но факт логируем.
            logger.warning("[%s] не удалось прочитать robots.txt — продолжаем без ограничений", self.slug)
        return self._robots

    def can_fetch(self, url: str) -> bool:
        robots = self._load_robots()
        return True if robots is None else robots.can_fetch(USER_AGENT, url)

    def crawl_delay(self) -> float:
        robots = self._load_robots()
        if robots is not None:
            declared = robots.crawl_delay(USER_AGENT)
            if declared:
                return max(float(declared), REQUEST_DELAY_SECONDS)
        return REQUEST_DELAY_SECONDS

    def wait_between_requests(self) -> None:
        """Пауза, отсчитанная от предыдущего запроса: не спим лишнего, если разбор
        страницы сам занял время."""
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.crawl_delay() - elapsed
        if self._last_request_at and remaining > 0:
            time.sleep(remaining)

    def _get(self, url: str) -> requests.Response:
        if not self.can_fetch(url):
            raise DisallowedByRobots(f"robots.txt запрещает {url}")
        self.wait_between_requests()
        response = requests.get(url, headers=DEFAULT_HEADERS, cookies=self.cookies, timeout=REQUEST_TIMEOUT)
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        return response

    def host(self) -> str:
        return urlparse(self.base_url).netloc
