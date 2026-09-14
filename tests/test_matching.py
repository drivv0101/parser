# -*- coding: utf-8 -*-
"""Тесты разбора текста: размеры, фасовка, цена, запрос, строки сметы."""

import unittest

from app.matching import (
    derive_pack,
    extract_dimensions,
    extract_pack,
    like_escape,
    parse_estimate_line,
    parse_estimate_text,
    parse_price,
    parse_query,
    unit_price,
    validate_price,
)


class TestDimensions(unittest.TestCase):
    def test_triple_in_millimeters(self):
        dims = extract_dimensions("ДСП шлифованная 2750х1830х16")
        self.assertEqual((dims.length_mm, dims.width_mm, dims.thickness_mm), (2750, 1830, 16))

    def test_triple_in_meters_is_normalized(self):
        dims = extract_dimensions("Фанера 1,22*2,44*16")
        self.assertEqual((dims.length_mm, dims.width_mm, dims.thickness_mm), (1220, 2440, 16))

    def test_thickness_written_separately(self):
        dims = extract_dimensions("ЛДСП 16мм 1,22х2,44м")
        self.assertEqual((dims.length_mm, dims.width_mm, dims.thickness_mm), (1220, 2440, 16))

    def test_ambiguous_pair_is_not_trusted(self):
        # "Бур 10х1000мм" — это Ø × длина сверла, а не лист: 1000 нельзя считать толщиной,
        # а 10 нельзя считать метрами.
        dims = extract_dimensions("Бур SDS-plus 10х1000мм")
        self.assertIsNone(dims.length_mm)
        self.assertIsNone(dims.thickness_mm)

    def test_no_dimensions(self):
        self.assertTrue(extract_dimensions("Смеситель для ванны").is_empty())


class TestPack(unittest.TestCase):
    def test_weight_in_kilograms(self):
        pack = extract_pack("Цемент Искитим ПЦ-500 50кг")
        self.assertEqual((pack.value, pack.unit), (50, "кг"))

    def test_weight_wins_over_pallet_count(self):
        pack = extract_pack("Цемент топки ПЦ-450 50кг /паллет 30шт/")
        self.assertEqual((pack.value, pack.unit), (50, "кг"))

    def test_grams_are_converted(self):
        pack = extract_pack("Клей монтажный 300 г")
        self.assertEqual((pack.value, pack.unit), (0.3, "кг"))

    def test_litres(self):
        self.assertEqual(extract_pack("Грунтовка глубокого проникновения 10 л").unit, "л")
        self.assertEqual(extract_pack("Грунтовка глубокого проникновения 10л").value, 10)

    def test_pieces_are_not_treated_as_pack(self):
        # По названию не отличить "в упаковке 300 шт" (цена за упаковку) от "(300шт)"
        # в описании при цене за штуку, поэтому штуки фасовкой не считаем вовсе.
        self.assertIsNone(extract_pack("Удлинитель профиля П60*27 (300шт)"))

    def test_dimension_is_not_a_pack(self):
        # "1,22х2,44м" — габарит листа, а не фасовка "44 метра"
        self.assertIsNone(extract_pack("ЛДСП 1,22х2,44м"))

    def test_false_positives_from_real_catalog(self):
        # Все три реально встретились в базе и давали цены вида 1 300 000 ₽/кг
        self.assertIsNone(extract_pack("Тройник Сэндвич ПиК Н/Н 150/230 45гр. 0,8мм/0,5мм"))
        self.assertIsNone(extract_pack("Топор в сборе 0,680/0,980гр. 400мм кованый"))
        self.assertIsNone(extract_pack("Кран шаровый для мет.пл.тр 20 *1/2 Г/Ц бабочка"))
        self.assertIsNone(extract_pack("Диск лепестковый по металлу Р 40 125х22мм ЛУГА 12040л"))
        self.assertIsNone(extract_pack("Кран шаровый 50 Г/Ш ручка Галлоп (Новосибирск)"))
        self.assertIsNone(extract_pack("Трос DIN3055 М2 (200/250м,)"))

    def test_sheet_area_is_derived_from_dimensions(self):
        dims = extract_dimensions("ДСП 2750х1830х16")
        pack = derive_pack("ДСП 2750х1830х16", dims)
        self.assertEqual(pack.unit, "м²")
        self.assertAlmostEqual(pack.value, 2.75 * 1.83, places=3)

    def test_small_profile_is_not_converted_to_area(self):
        # Уголок 50х50 — это профиль, а не лист площадью 0,0025 м²
        dims = extract_dimensions("Уголок стальной 50х50х5")
        self.assertIsNone(derive_pack("Уголок стальной 50х50х5", dims))

    def test_box_dimensions_are_not_converted_to_area(self):
        # Третий размер в сотни миллиметров — это высота коробки, а не толщина листа
        for name in (
            "Унитаз напольный Grossman Classic (670х360х800) белый",
            "Щит учетно-распределительный навесной ЩУ-1/1 (IEK) 310х300х170",
        ):
            self.assertIsNone(derive_pack(name, extract_dimensions(name)), name)

    def test_tile_area_is_derived(self):
        name = "Плитка керамогранит 600х600х10"
        pack = derive_pack(name, extract_dimensions(name))
        self.assertEqual((pack.unit, pack.value), ("м²", 0.36))

    def test_tool_weight_is_not_a_pack(self):
        # "Колун в сборе (3,6кг)" весит 3,6 кг, но продаётся штукой: 791 ₽/кг — бессмыслица
        name = "Колун в сборе (3,6кг) фибергласовое топорище Вихрь 73/2/1/5"
        self.assertIsNotNone(derive_pack(name, extract_dimensions(name)))  # без категории не отличить
        self.assertIsNone(derive_pack(name, extract_dimensions(name), "Инструменты"))
        self.assertIsNone(derive_pack(name, extract_dimensions(name), "/catalog/ruchnoy-instrument/"))

    def test_bulk_goods_keep_pack_in_any_category(self):
        name = "Цемент Искитим ПЦ-500 50кг"
        pack = derive_pack(name, extract_dimensions(name), "Строительные материалы")
        self.assertEqual((pack.value, pack.unit), (50, "кг"))

    def test_mixed_section_is_judged_by_subcategory(self):
        # Раздел Алтерры "Инструменты, хозтовары, крепеж" содержит и топоры, и гвозди на вес
        name = "Гвозди строительные 3,0х80 1кг"
        nails = derive_pack(name, extract_dimensions(name), "Инструменты, хозтовары, крепеж / Гвозди")
        self.assertEqual((nails.value, nails.unit), (1, "кг"))
        axe = "Колун Сибин, деревянная рукоятка, 3 кг"
        self.assertIsNone(derive_pack(axe, extract_dimensions(axe),
                                      "Инструменты, хозтовары, крепеж / Слесарно-столярный инструмент"))

    def test_sold_by_weight_means_price_per_kilogram(self):
        # Строймир: "вес" в конце названия = продаётся на вес, цена за 1 кг
        pack = extract_pack("Гвозди 3*80  вес")
        self.assertEqual((pack.value, pack.unit), (1, "кг"))
        self.assertEqual(unit_price(125, pack), 125)

    def test_explicit_pack_wins_over_weight_word(self):
        pack = extract_pack("гвоздь финишный оцинк 1.8*40 (0,1кг.) вес")
        self.assertEqual((pack.value, pack.unit), (0.1, "кг"))

    def test_weight_word_false_positives(self):
        # Бренды и характеристика "вес 4,3кг" — не признак весового товара
        for name in ("Панель ПВХ Вестанвинд №22-009", "Ревизия с крышкой ПП 50 (Вессель)",
                     "Крепление сиденья к унитазу ВестМ белый", "Весы электронные кухонные"):
            self.assertIsNone(extract_pack(name), name)
        saw = "Пила дисковая 185х16/20мм, пропил 65мм, вес 4,3кг, сбор пыли"
        self.assertEqual(extract_pack(saw).value, 4.3)  # характеристика, но явный вес — не "на вес"
        self.assertIsNone(derive_pack(saw, extract_dimensions(saw), "Инструменты"))  # и инструмент

    def test_unit_price(self):
        self.assertEqual(unit_price(580, extract_pack("Цемент 50кг")), 11.6)
        self.assertEqual(unit_price(154, extract_pack("Цементная смесь 2 кг")), 77.0)
        self.assertIsNone(unit_price(100, None))


class TestPrice(unittest.TestCase):
    def test_plain_and_spaced(self):
        self.assertEqual(parse_price("1 250 руб"), 1250.0)
        self.assertEqual(parse_price("1\xa0250\xa0₽"), 1250.0)

    def test_kopecks(self):
        self.assertEqual(parse_price("1 250,50 ₽"), 1250.5)
        self.assertEqual(parse_price("1,5"), 1.5)

    def test_dot_as_thousands_separator(self):
        self.assertEqual(parse_price("1.250"), 1250.0)

    def test_range_takes_first_number_not_glued(self):
        # Раньше вырезались все нецифровые символы и получалось 9001200
        self.assertEqual(parse_price("от 900 до 1 200 ₽"), 900.0)
        self.assertEqual(parse_price("1 200 ₽ 1 500 ₽"), 1200.0)

    def test_unparseable(self):
        with self.assertRaises(ValueError):
            parse_price("Цена по запросу")

    def test_zero_and_absurd_prices_rejected(self):
        for bad in (0, -10, float("nan"), float("inf"), 99_000_000):
            with self.assertRaises(ValueError):
                validate_price(bad, "тест")

    def test_valid_price_rounded(self):
        self.assertEqual(validate_price(1250.456), 1250.46)


class TestQuery(unittest.TestCase):
    def test_keywords_are_lowercased(self):
        # search_text в базе лоуеркейснут: без этого "Цемент" не совпадал с началом названия
        self.assertEqual(parse_query("Цемент М500").keywords, ["цемент", "м500"])

    def test_dimensions_removed_from_keywords(self):
        parsed = parse_query("ДСП 1,22х2,44х16")
        self.assertEqual(parsed.keywords, ["дсп"])
        self.assertEqual(parsed.dimensions.thickness_mm, 16)

    def test_descriptor_words_removed(self):
        parsed = parse_query("ДСП толщиной 16 мм")
        self.assertEqual(parsed.keywords, ["дсп"])
        self.assertEqual(parsed.dimensions.thickness_mm, 16)

    def test_empty_query(self):
        self.assertTrue(parse_query("   ").is_empty())
        self.assertTrue(parse_query("толщиной").is_empty())

    def test_like_escape(self):
        self.assertEqual(like_escape("100%_"), r"100\%\_")


class TestEstimateLines(unittest.TestCase):
    def test_explicit_x_quantity(self):
        line = parse_estimate_line("ДСП 1,22х2,44х16 x10")
        self.assertEqual((line.query, line.qty), ("ДСП 1,22х2,44х16", 10))

    def test_excel_style_unit_then_quantity(self):
        line = parse_estimate_line("Цемент М500 - меш - 40")
        self.assertEqual((line.query, line.qty), ("Цемент М500", 40))

    def test_excel_style_quantity_then_unit(self):
        line = parse_estimate_line("Плитка керамическая, 15, м2")
        self.assertEqual((line.query, line.qty), ("Плитка керамическая", 15))

    def test_running_meters(self):
        line = parse_estimate_line("Кабель ВВГ 3х2,5 - м - 100")
        self.assertEqual((line.query, line.qty), ("Кабель ВВГ 3х2,5", 100))

    def test_size_is_not_quantity(self):
        # Главная причина, по которой парсер переехал на бэкенд: "50 х 50" — это размер
        for text in ("Уголок 50 х 50", "Труба 20 х 3000", "Профиль 60х27 х 25"):
            line = parse_estimate_line(text)
            self.assertEqual(line.qty, 1, text)
            self.assertEqual(line.query, text, text)

    def test_size_in_name_survives(self):
        line = parse_estimate_line("Гвозди 100 мм 5 кг")
        self.assertEqual((line.query, line.qty), ("Гвозди 100 мм", 5))

    def test_line_without_quantity(self):
        line = parse_estimate_line("Перфоратор")
        self.assertEqual((line.query, line.qty), ("Перфоратор", 1))

    def test_blank_lines_skipped(self):
        items = parse_estimate_text("Цемент М500 - меш - 40\n\n   \nПерфоратор\n")
        self.assertEqual([(i.query, i.qty) for i in items], [("Цемент М500", 40), ("Перфоратор", 1)])


if __name__ == "__main__":
    unittest.main()
