# -*- coding: utf-8 -*-
"""Разбор разметки магазинов на сохранённых фрагментах — без обращения к сайтам."""

import unittest

from bs4 import BeautifulSoup

from app.scrapers.alterra import AlterraScraper

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


if __name__ == "__main__":
    unittest.main()
