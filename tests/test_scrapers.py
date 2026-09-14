# -*- coding: utf-8 -*-
"""Разбор разметки магазинов на сохранённых фрагментах — без обращения к сайтам."""

import unittest

from bs4 import BeautifulSoup

from app.scrapers.alterra import CITY_COOKIE, AlterraScraper
from app.scrapers.stroymir import StroymirScraper

MENU_HTML = """
<ul>
  <li class="catalog-nav__item">
    <a class="catalog-nav__item-link" href="/catalog/instrument/">Инструмент</a>
    <ul class="drop-nav__list">
      <li><a href="/catalog/topory/">Топоры и колуны</a></li>
      <li><a href="/catalog/slesarno-stolyarnyy-instrument/">Слесарно-столярный инструмент</a></li>
    </ul>
  </li>
  <li class="catalog-nav__item">
    <a class="catalog-nav__item-link" href="/catalog/krepezh/">Крепёж</a>
    <ul class="drop-nav__list">
      <li><a href="/catalog/gvozdi_1/">Гвозди</a></li>
      <li><a href="/catalog/gvozdi_1/">Гвозди (дубль в меню)</a></li>
    </ul>
  </li>
  <li class="catalog-nav__item">
    <a class="catalog-nav__item-link" href="/catalog/sad-i-ogorod/">Сад и огород</a>
    <ul class="drop-nav__list"><li><a href="/catalog/semena/">Семена</a></li></ul>
  </li>
</ul>
"""


class TestAlterraMenu(unittest.TestCase):
    def setUp(self):
        self.names = AlterraScraper.parse_menu(BeautifulSoup(MENU_HTML, "lxml"))

    def test_names_come_from_menu_with_parent_section(self):
        # Раньше категорией становился путь "/catalog/gvozdi_1/" — на сайте нет <h1>
        self.assertEqual(self.names["/catalog/gvozdi_1/"], "Крепёж / Гвозди")
        self.assertEqual(self.names["/catalog/topory/"], "Инструмент / Топоры и колуны")

    def test_first_menu_entry_wins_for_duplicate_paths(self):
        self.assertEqual(self.names["/catalog/gvozdi_1/"], "Крепёж / Гвозди")

    def test_excluded_sections_are_skipped(self):
        self.assertNotIn("/catalog/semena/", self.names)

    def test_tool_section_is_recognized_as_sold_by_piece(self):
        from app.matching import category_sold_by_piece

        self.assertTrue(category_sold_by_piece(self.names["/catalog/topory/"]))
        self.assertFalse(category_sold_by_piece(self.names["/catalog/gvozdi_1/"]))


def alterra_card(rows_html: str, label: str = "В наличии", cls: str = "availability_in") -> BeautifulSoup:
    return BeautifulSoup(f"""
    <div class="card__wrapper">
      <div class="availability {cls} tip tip_wide"><span class="tip__name">{label}</span>
        <div class="tip__hidecontent"><ul class="tip__vlist">{rows_html}</ul></div>
      </div>
    </div>""", "lxml").select_one("div.card__wrapper")


class TestAlterraAvailability(unittest.TestCase):
    def test_city_cookie_is_set(self):
        # Без cookie сайт показывает Барнаул — и барнаульские цены
        self.assertEqual(AlterraScraper().cookies, CITY_COOKIE)
        self.assertEqual(CITY_COOKIE["BITRIX_SM_torgzal"], "biysk")

    def test_in_stock_sums_biysk_stores(self):
        card = alterra_card("""
          <li><span>Бийск, пер. Шубенский, 75</span><span><strong>10 шт</strong></span></li>
          <li><span>Бийск, ул. Советская, 206</span><span><strong>1 шт</strong></span></li>
          <li><span>Бийск, ул. Ивана Тургенева, 88</span><span><strong>4 шт</strong></span></li>""")
        self.assertEqual(AlterraScraper.parse_availability(card), (True, "в наличии: 15 шт"))

    def test_more_than_hundred(self):
        card = alterra_card("<li><span>Бийск, пер. Шубенский, 75</span><span><strong>&gt;100  уп</strong></span></li>")
        self.assertEqual(AlterraScraper.parse_availability(card), (True, "в наличии: 100+ уп"))

    def test_zero_in_biysk_is_on_order_even_if_barnaul_has_it(self):
        card = alterra_card("""
          <li><span>Бийск, пер. Шубенский, 75</span><span><strong>0 уп</strong></span></li>
          <li><span>Барнаул, Павловский тракт, 206Б</span><span><strong>&gt;100 уп</strong></span></li>""")
        self.assertEqual(AlterraScraper.parse_availability(card), (False, "под заказ"))

    def test_label_only(self):
        card = alterra_card("", label="Под заказ", cls="availability_out")
        self.assertEqual(AlterraScraper.parse_availability(card), (False, "под заказ"))

    def test_no_block(self):
        card = BeautifulSoup('<div class="card__wrapper"><a class="card__main"></a></div>', "lxml").select_one("div.card__wrapper")
        self.assertEqual(AlterraScraper.parse_availability(card), (None, None))


class TestStroymirAvailability(unittest.TestCase):
    def test_on_stock(self):
        card = BeautifulSoup('<div class="product-thumb"><p class="text-available">Доступно:<span>на складе</span></p></div>', "lxml").select_one("div.product-thumb")
        self.assertEqual(StroymirScraper.parse_availability(card), (True, "на складе"))

    def test_other_text_means_not_in_stock(self):
        card = BeautifulSoup('<div class="product-thumb"><p class="text-available">Доступно:<span>под заказ</span></p></div>', "lxml").select_one("div.product-thumb")
        self.assertEqual(StroymirScraper.parse_availability(card), (False, "под заказ"))

    def test_missing(self):
        card = BeautifulSoup('<div class="product-thumb"></div>', "lxml").select_one("div.product-thumb")
        self.assertEqual(StroymirScraper.parse_availability(card), (None, None))


if __name__ == "__main__":
    unittest.main()
