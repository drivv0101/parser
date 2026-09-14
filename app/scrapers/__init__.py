from app.scrapers.alterra import AlterraScraper
from app.scrapers.base import BaseScraper
from app.scrapers.stroymir import StroymirScraper

SCRAPERS: dict[str, type[BaseScraper]] = {
    "stroymir": StroymirScraper,
    "alterra": AlterraScraper,
}
