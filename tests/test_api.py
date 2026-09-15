# -*- coding: utf-8 -*-
"""Тесты поиска и сметы на отдельной временной базе.

Эндпоинты — обычные функции, поэтому вызываем их напрямую и не тянем в зависимости
HTTP-клиент ради тестов.
"""

import os
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="stroy-test-"), "test.db")
os.environ["STROY_DB_PATH"] = _TMP_DB
os.environ["STROY_AUTO_SCRAPE"] = "0"
os.environ["STROY_LOG_TO_FILE"] = "0"

from app import main as api  # noqa: E402  (после подмены пути к базе)
from app.db import get_session, init_db  # noqa: E402
from app.matching import derive_pack, extract_dimensions, unit_price  # noqa: E402
from app.models import Product, Store  # noqa: E402

NOW = datetime(2026, 9, 9, 18, 39, 4)

CATALOG = [
    # (магазин, название, цена, активен)
    ("stroymir", "Цемент Искитим ПЦ-500 50кг", 610.0, True),
    ("stroymir", "Цемент топки ПЦ-450 50кг", 580.0, True),
    ("stroymir", "Наличник МДФ 2150х70, цемент светлый", 320.0, True),
    ("stroymir", "Снятый с продажи цемент 50кг", 100.0, False),
    ("alterra", "Цементная смесь серая М-400 (Диола) 2 кг", 154.0, True),
    ("alterra", "Цемент (Искитимцемент) М400 Д20 50кг", 650.0, True),
    ("alterra", "ДСП шлифованная 2750х1830х16", 1800.0, True),
]


def setUpModule():
    init_db()
    session = get_session()
    try:
        stores = {
            "stroymir": Store(slug="stroymir", name="Строймир", base_url="https://stroymir.su"),
            "alterra": Store(slug="alterra", name="ТГ-Алтерра", base_url="https://tg-alterra.ru"),
        }
        for store in stores.values():
            session.add(store)
        session.flush()

        for index, (slug, name, price, active) in enumerate(CATALOG):
            dims = extract_dimensions(name)
            pack = derive_pack(name, dims)
            session.add(Product(
                store_id=stores[slug].id,
                name=name,
                search_text=name.lower(),
                url=f"https://example.test/p{index}",
                price=price,
                length_mm=dims.length_mm,
                width_mm=dims.width_mm,
                thickness_mm=dims.thickness_mm,
                pack_value=pack.value if pack else None,
                pack_unit=pack.unit if pack else None,
                unit_price=unit_price(price, pack),
                is_active=active,
                scraped_at=NOW,
            ))
        session.commit()
    finally:
        session.close()


class TestSearch(unittest.TestCase):
    def test_capitalized_query_still_finds_products(self):
        # Регрессия: запрос с большой буквы уводил всю выдачу в "возможно, вас заинтересует"
        lowercase = api.search(q="цемент")
        capitalized = api.search(q="Цемент")
        self.assertEqual(len(lowercase["results"]), len(capitalized["results"]))
        self.assertGreater(len(capitalized["results"]), 0)

    def test_irrelevant_matches_go_to_maybe_also(self):
        data = api.search(q="цемент")
        self.assertNotIn("Наличник", " ".join(it["name"] for it in data["results"]))
        self.assertIn("Наличник", " ".join(it["name"] for it in data["maybe_also"]))

    def test_best_price_uses_price_per_unit(self):
        # Пакет 2 кг за 154 ₽ дешевле мешка, но 77 ₽/кг против 11,6 ₽/кг — не выгоднее
        data = api.search(q="цемент")
        best = data["results"][data["best_index"]]
        self.assertEqual(data["comparison_unit"], "кг")
        self.assertEqual(best["name"], "Цемент топки ПЦ-450 50кг")
        self.assertEqual(best["unit_price"], 11.6)

    def test_no_badge_when_most_goods_are_sold_by_piece(self):
        # Вес указан у двух колунов из пяти — значит этот товар меряют штуками,
        # и выделять "лучшую цену за кг" нельзя
        session = get_session()
        try:
            store = session.query(Store).filter_by(slug="stroymir").one()
            catalog = [
                ("Колун в сборе (3,6кг) фибергласовое топорище", 2850.0, 3.6, 791.67),
                ("Колун в сборе (1,9кг) кованный, дер. топорище", 1700.0, 1.9, 894.74),
                ("Колун-топор ТК17", 2800.0, None, None),
                ("Колун-топор ТК21", 3350.0, None, None),
                ("Колун-топор ТК25", 4500.0, None, None),
            ]
            for index, (name, price, pack, per_unit) in enumerate(catalog):
                session.add(Product(
                    store_id=store.id, name=name, search_text=name.lower(),
                    url=f"https://example.test/axe{index}", price=price,
                    pack_value=pack, pack_unit="кг" if pack else None, unit_price=per_unit,
                    is_active=True, scraped_at=NOW,
                ))
            session.commit()
        finally:
            session.close()

        data = api.search(q="колун")
        self.assertEqual(len(data["results"]), 5)
        self.assertIsNone(data["comparison_unit"])
        self.assertIsNone(data["best_index"])

    def test_on_order_items_rank_below_in_stock_and_get_no_badge(self):
        session = get_session()
        try:
            store = session.query(Store).filter_by(slug="alterra").one()
            catalog = [
                ("Грунтовка глубокая 10 л (Бийск)", 500.0, True, "в наличии: 12 шт"),
                ("Грунтовка глубокая 10 л (Барнаул)", 400.0, False, "под заказ"),
                ("Грунтовка глубокая 10 л (без данных)", 450.0, None, None),
            ]
            for index, (name, price, in_stock, note) in enumerate(catalog):
                session.add(Product(
                    store_id=store.id, name=name, search_text=name.lower(),
                    url=f"https://example.test/gr{index}", price=price,
                    pack_value=10, pack_unit="л", unit_price=price / 10,
                    in_stock=in_stock, stock_note=note, is_active=True, scraped_at=NOW,
                ))
            session.commit()
        finally:
            session.close()

        data = api.search(q="грунтовка")
        names = [it["name"] for it in data["results"]]
        # самый дешёвый — под заказ, но он уходит в конец, а бейдж — самому дешёвому из бийских
        self.assertEqual(names[-1], "Грунтовка глубокая 10 л (Барнаул)")
        self.assertEqual(data["results"][data["best_index"]]["name"], "Грунтовка глубокая 10 л (без данных)")

        line = api.estimate(api.EstimateRequest(items=[api.EstimateItem(query="грунтовка", qty=1)]))["lines"][0]
        self.assertNotEqual(line["best"]["name"], "Грунтовка глубокая 10 л (Барнаул)")
        self.assertFalse(line["best"]["in_stock"] is False)

    def test_inactive_products_are_hidden(self):
        names = [it["name"] for it in api.search(q="цемент")["results"]]
        self.assertNotIn("Снятый с продажи цемент 50кг", names)

    def test_multiword_query_falls_back_to_first_word(self):
        # "Цемент М500" не совпадает ни с одним названием целиком — раньше был пустой ответ
        data = api.search(q="Цемент М500")
        self.assertTrue(data["relaxed"])
        self.assertGreater(len(data["results"]), 0)

    def test_like_wildcards_are_escaped(self):
        self.assertEqual(api.search(q="100%_")["results"], [])

    def test_empty_query_rejected(self):
        with self.assertRaises(Exception):
            api.search(q="   ")

    def test_discounts_applied_by_slug(self):
        data = api.search(q="цемент", discounts='{"stroymir": 10}')
        row = next(it for it in data["results"] if it["name"] == "Цемент топки ПЦ-450 50кг")
        self.assertEqual(row["effective_price"], 522.0)
        self.assertEqual(row["unit_price"], 10.44)

    def test_nan_discount_is_ignored(self):
        data = api.search(q="цемент", discounts='{"stroymir": NaN}')
        row = next(it for it in data["results"] if it["name"] == "Цемент топки ПЦ-450 50кг")
        self.assertEqual(row["effective_price"], 580.0)

    def test_scraped_at_carries_timezone(self):
        row = api.search(q="цемент")["results"][0]
        self.assertTrue(row["scraped_at"].endswith("+00:00"))


class TestEstimate(unittest.TestCase):
    def test_line_picks_sensible_package(self):
        req = api.EstimateRequest(items=[api.EstimateItem(query="цемент", qty=10)])
        line = api.estimate(req)["lines"][0]
        self.assertEqual(line["status"], "ok")
        self.assertEqual(line["best"]["name"], "Цемент топки ПЦ-450 50кг")
        self.assertEqual(line["best"]["line_total"], 5800.0)

    def test_oversized_pack_is_not_substituted(self):
        # 40 мешков шпатлёвки — это не 40 биг-бэгов по полтонны, даже если тонна выгоднее за кг
        session = get_session()
        try:
            store = session.query(Store).filter_by(slug="alterra").one()
            catalog = [
                ("Шпатлёвка финишная Knauf 20кг", 400.0, 20, 20.0),
                ("Шпатлёвка финишная Волма 20кг", 440.0, 20, 22.0),
                ("Шпатлёвка финишная Основит 20кг", 460.0, 20, 23.0),
                ("Шпатлёвка финишная МКР 500кг", 8000.0, 500, 16.0),
            ]
            for index, (name, price, pack, per_unit) in enumerate(catalog):
                session.add(Product(
                    store_id=store.id, name=name, search_text=name.lower(),
                    url=f"https://example.test/sh{index}", price=price,
                    pack_value=pack, pack_unit="кг", unit_price=per_unit,
                    is_active=True, scraped_at=NOW,
                ))
            session.commit()
        finally:
            session.close()

        req = api.EstimateRequest(items=[api.EstimateItem(query="шпатлёвка", qty=40)])
        line = api.estimate(req)["lines"][0]
        self.assertEqual(line["best"]["name"], "Шпатлёвка финишная Knauf 20кг")
        self.assertNotIn("МКР", line["by_store"]["alterra"]["name"])

    def test_pinned_item_uses_current_price_and_discount(self):
        url = next(
            it["url"] for it in api.search(q="цемент")["results"]
            if it["name"] == "Цемент (Искитимцемент) М400 Д20 50кг"
        )
        req = api.EstimateRequest(
            items=[api.EstimateItem(query="что угодно", qty=2, url=url)],
            discounts={"alterra": 50},
        )
        line = api.estimate(req)["lines"][0]
        self.assertTrue(line["pinned"])
        self.assertEqual(line["best"]["line_total"], 650.0)  # 650 * 0.5 * 2

    def test_pinned_item_that_disappeared(self):
        req = api.EstimateRequest(items=[api.EstimateItem(query="х", qty=1, url="https://example.test/gone")])
        line = api.estimate(req)["lines"][0]
        self.assertEqual(line["status"], "unavailable")

    def test_lines_keep_request_order(self):
        req = api.EstimateRequest(items=[
            api.EstimateItem(query="дсп", qty=1),
            api.EstimateItem(query="несуществующий товар", qty=1),
            api.EstimateItem(query="цемент", qty=1),
        ])
        data = api.estimate(req)
        self.assertEqual([line["index"] for line in data["lines"]], [0, 1, 2])
        self.assertEqual(data["lines"][1]["status"], "not_found")
        self.assertEqual(data["unresolved"], ["несуществующий товар"])

    def test_store_options_and_savings(self):
        req = api.EstimateRequest(items=[
            api.EstimateItem(query="цемент", qty=1),
            api.EstimateItem(query="дсп", qty=1),
        ])
        data = api.estimate(req)
        # ДСП есть только у Алтерры, поэтому полного покрытия у Строймира нет
        stroymir = next(o for o in data["store_options"] if o["store_slug"] == "stroymir")
        self.assertFalse(stroymir["complete"])
        self.assertEqual(stroymir["missing"], ["дсп"])
        self.assertGreaterEqual(data["optimal_total"], 0)


class TestScrapeSchedule(unittest.TestCase):
    def test_fresh_data_is_not_due(self):
        session = get_session()
        try:
            for store in session.query(Store).all():
                store.last_scraped_at = NOW
            session.commit()
            self.assertFalse(api.scrape_is_due(session, 12, now=NOW + timedelta(hours=1)))
            self.assertTrue(api.scrape_is_due(session, 12, now=NOW + timedelta(hours=12)))
        finally:
            session.close()

    def test_never_scraped_store_is_due(self):
        session = get_session()
        try:
            store = session.query(Store).filter_by(slug="alterra").one()
            store.last_scraped_at = None
            session.commit()
            self.assertTrue(api.scrape_is_due(session, 12, now=NOW))
            store.last_scraped_at = NOW
            session.commit()
        finally:
            session.close()

    def test_display_category(self):
        self.assertEqual(api._display_category("Инструменты, хозтовары, крепеж / Гвозди"), "Гвозди")
        self.assertEqual(api._display_category("Строительные материалы"), "Строительные материалы")
        self.assertIsNone(api._display_category("/catalog/gvozdi_1/"))
        self.assertIsNone(api._display_category("Арматура, круг, квадрат: виды, характеристики и применение"))
        self.assertIsNone(api._display_category(None))

    def test_health(self):
        data = api.health()
        self.assertEqual(data["status"], "ok")
        self.assertGreater(data["active_products"], 0)


class TestScrapeLock(unittest.TestCase):
    def test_second_run_is_refused_while_first_holds_lock(self):
        from app import run_scrape

        with run_scrape.scrape_lock():
            self.assertTrue(run_scrape.LOCK_PATH.exists())
            with self.assertRaises(run_scrape.ScrapeInProgress):
                run_scrape.run_store("stroymir")  # до сети не доходит — замок раньше
        self.assertFalse(run_scrape.LOCK_PATH.exists())

    def test_stale_lock_is_taken_over(self):
        from app import run_scrape

        run_scrape.LOCK_PATH.write_text("dead")
        old = time.time() - run_scrape.LOCK_STALE_SECONDS - 60
        os.utime(run_scrape.LOCK_PATH, (old, old))
        with run_scrape.scrape_lock():
            self.assertNotEqual(run_scrape.LOCK_PATH.read_text(), "dead")
        self.assertFalse(run_scrape.LOCK_PATH.exists())


class TestEstimateUnitsAndDelivery(unittest.TestCase):
    def test_quantity_in_kilograms_buys_whole_bags(self):
        # 100 кг цемента = 2 мешка по 50 кг самого выгодного за килограмм
        req = api.EstimateRequest(items=[api.EstimateItem(query="цемент", qty=100, qty_unit="кг")])
        line = api.estimate(req)["lines"][0]
        self.assertEqual(line["status"], "ok")
        self.assertEqual(line["best"]["name"], "Цемент топки ПЦ-450 50кг")
        self.assertEqual(line["best"]["packages"], 2)
        self.assertEqual(line["best"]["line_total"], 1160.0)
        self.assertEqual(line["best"]["surplus"], 0)

    def test_different_brands_are_flagged_not_refused(self):
        line = api.estimate(api.EstimateRequest(items=[api.EstimateItem(query="цемент", qty=1)]))["lines"][0]
        self.assertEqual(line["status"], "ok")
        self.assertTrue(line["needs_review"])

    def test_delivery_cost_changes_store_choice(self):
        items = [api.EstimateItem(query="цемент", qty=1)]
        cheap = api.estimate(api.EstimateRequest(items=items))
        self.assertEqual(cheap["lines"][0]["best"]["store_slug"], "stroymir")
        # Доставка из Строймира дороже разницы в цене — выгоднее Алтерра
        expensive = api.estimate(api.EstimateRequest(items=items, delivery={"stroymir": 5000, "alterra": 0}))
        self.assertEqual(expensive["lines"][0]["best"]["store_slug"], "alterra")
        self.assertEqual(expensive["delivery_total"], 0)

    def test_xlsx_export_is_a_zip_with_text_cells(self):
        from app.exporting import workbook

        data = api.estimate(api.EstimateRequest(items=[api.EstimateItem(query="=цемент", qty=1)]))
        blob = workbook(data)
        self.assertTrue(blob.startswith(b"PK"))
        import zipfile, io
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn('t="inlineStr"', sheet)   # текст остаётся текстом, "=цемент" не станет формулой


class TestPriceHistory(unittest.TestCase):
    def test_history_endpoint_returns_current_price_when_no_history(self):
        url = api.search(q="цемент")["results"][0]["url"]
        data = api.price_history(url=url)
        self.assertEqual(len(data["history"]), 1)
        self.assertTrue(data["active"])


class TestParseLines(unittest.TestCase):
    def test_parse_lines_endpoint(self):
        data = api.parse_lines(api.ParseLinesRequest(text="Цемент М500 - меш - 40\nУголок 50 х 50\nГвозди - кг - 5"))
        self.assertEqual(data["items"], [
            {"query": "Цемент М500", "qty": 40, "qty_unit": "уп", "needs_review": False},
            {"query": "Уголок 50 х 50", "qty": 1, "qty_unit": "уп", "needs_review": False},
            {"query": "Гвозди", "qty": 5, "qty_unit": "кг", "needs_review": False},
        ])


if __name__ == "__main__":
    unittest.main()
