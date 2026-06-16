"""Тесты детерминированного перевода названий запчастей (part_naming).

Запуск:
    python manage.py test assistant.tests_part_naming -v 2
"""
from __future__ import annotations

from django.test import SimpleTestCase

from assistant.part_naming import translate_title


class TranslateTitleTests(SimpleTestCase):
    def test_german_single(self):
        self.assertEqual(translate_title("FAHRANTRIEB"), "Ходовой привод")
        self.assertEqual(translate_title("SCHLAUCH"), "Шланг")

    def test_english_single(self):
        self.assertEqual(translate_title("BRACKET"), "Кронштейн")
        self.assertEqual(translate_title("PLATE"), "Пластина")

    def test_assembly_collapses(self):
        # ASS'Y / KPL / ASSEMBLY → одно «в сборе», без дублей
        self.assertEqual(translate_title("HOSE ASS'Y"), "Шланг в сборе")
        self.assertEqual(translate_title("DRUCKLEITUNG KPL"), "Напорный трубопровод в сборе")
        self.assertEqual(translate_title("ENGINE ASSEMBLY"), "Двигатель в сборе")

    def test_phrase_override(self):
        self.assertEqual(translate_title("WIRING HARNESS"), "Жгут проводов")
        self.assertEqual(translate_title("CONTROL VALVE"), "Клапан управления")
        self.assertEqual(translate_title("OIL SEAL"), "Сальник")

    def test_phrase_word_boundary(self):
        # «O RING» не должен сработать внутри «MICRO RING»
        self.assertEqual(translate_title("MICRO RING"), "MICRO кольцо")
        self.assertEqual(translate_title("O RING"), "Уплотнительное кольцо")

    def test_brand_noise_stripped(self):
        # KOMATSU / PART — мусор, выкидывается; модель-код остаётся
        r = translate_title("Komatsu Track Roller(hitachizx330-3)")
        self.assertIn("каток", r.lower())
        self.assertNotIn("KOMATSU", r.upper())

    def test_model_code_preserved(self):
        r = translate_title("PC200 HYD TANK")
        self.assertIn("PC200", r)
        self.assertIn("бак", r.lower())

    def test_unknown_returns_empty(self):
        # Перевести нечего → '' (фронт покажет оригинал)
        self.assertEqual(translate_title("XYZZY QWERTY"), "")
        self.assertEqual(translate_title("KF"), "")
        self.assertEqual(translate_title("123-456"), "")

    def test_empty_input(self):
        self.assertEqual(translate_title(""), "")
        self.assertEqual(translate_title(None), "")

    def test_deterministic(self):
        # Один вход → один выход (надёжность: код, не AI)
        a = translate_title("HYDRAULIKPUMPE")
        b = translate_title("HYDRAULIKPUMPE")
        self.assertEqual(a, b)
        self.assertEqual(a, "Гидронасос")

    def test_unknown_word_not_lost(self):
        # Незнакомое слово рядом с известным — НЕ теряется
        r = translate_title("FOOBAR BRACKET")
        self.assertIn("кронштейн", r.lower())
        self.assertIn("FOOBAR", r.upper())
