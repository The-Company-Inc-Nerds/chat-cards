"""Tests for PriceCharting query building and product choice (no network).

    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from chat_cards.pricecharting import (  # noqa: E402
    build_query,
    choose_product,
    language_code,
    language_word,
    normalise_number,
)


def product(name: str, console: str) -> dict:
    return {"product-name": name, "console-name": console}


class NormaliseTests(unittest.TestCase):
    def test_number(self) -> None:
        self.assertEqual(normalise_number("052/103"), "52")
        self.assertEqual(normalise_number("007"), "7")
        self.assertEqual(normalise_number("#155"), "155")
        self.assertEqual(normalise_number("SM230"), "SM230")
        self.assertIsNone(normalise_number(""))

    def test_language(self) -> None:
        self.assertEqual(language_code("Japanese"), "ja")
        self.assertEqual(language_code("ko"), "ko")
        self.assertEqual(language_code("KR"), "ko")
        self.assertEqual(language_code("ja-JP"), "ja")
        self.assertIsNone(language_word("english"))
        self.assertIsNone(language_word(None))
        self.assertEqual(language_word("korean"), "korean")

    def test_query(self) -> None:
        self.assertEqual(
            build_query("Morpeko", "052/103", "30th Celebration", "japanese"),
            "Morpeko #52 pokemon japanese 30th Celebration",
        )
        self.assertEqual(build_query("Nuzleaf", "004/071", None, "ko"), "Nuzleaf #4 pokemon korean")


class ChooseTests(unittest.TestCase):
    def test_number_is_exact(self) -> None:
        products = [product("Pikachu #55", "Pokemon Ascended Heroes"), product("Pikachu #5", "Pokemon Base Set")]
        self.assertEqual(choose_product(products, "5", None)["product-name"], "Pikachu #5")
        self.assertEqual(choose_product(products, "055", None)["product-name"], "Pikachu #55")

    def test_language_must_match_when_given(self) -> None:
        products = [product("Torracat #33", "Pokemon Temporal Forces")]
        self.assertIsNone(choose_product(products, "33", None, None, "korean"))

    def test_language_and_set_break_ties(self) -> None:
        products = [
            product("Zamazenta #86", "Pokemon Japanese 30th Celebration"),
            product("Zamazenta #86", "Pokemon Korean 30th Celebration"),
            product("Zamazenta #86", "Pokemon Chinese 30th Celebration"),
        ]
        self.assertEqual(choose_product(products, "86", None, "30th Celebration", "ko")["console-name"],
                         "Pokemon Korean 30th Celebration")

    def test_english_prefers_unlabelled_set(self) -> None:
        products = [
            product("Zamazenta #86", "Pokemon Japanese 30th Celebration"),
            product("Zamazenta #86", "Pokemon 30th Celebration"),
        ]
        self.assertEqual(choose_product(products, "86", None)["console-name"], "Pokemon 30th Celebration")

    def test_still_refuses_to_guess(self) -> None:
        products = [product("Morpeko #52", "Pokemon Japanese 30th Celebration"),
                    product("Morpeko #110", "Pokemon Japanese 30th Celebration")]
        self.assertIsNone(choose_product(products, None, None, "30th Celebration", "ja"))


if __name__ == "__main__":
    unittest.main()
