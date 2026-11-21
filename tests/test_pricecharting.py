"""Tests for PriceCharting query building, product choice and the token check (no network).

    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from aiohttp import web  # noqa: E402

from _support import Upstream  # noqa: E402
from chat_cards import pricecharting  # noqa: E402
from chat_cards.pricecharting import (  # noqa: E402
    PriceChartingClient,
    build_query,
    choose_product,
    language_code,
    language_word,
    normalise_number,
)


TOKEN = "0123456789abcdef0123456789abcdef01234567"


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


class HealthTests(unittest.TestCase):
    """health() against a loopback stand-in: a bad token and no answer are different things."""

    def verdict(self, status: int | None, payload: object = None, respond=None) -> bool | None:
        async def run() -> bool | None:
            async def answer(_request: web.Request) -> web.Response:
                return web.json_response({} if payload is None else payload, status=status)

            upstream = Upstream(respond or answer)
            if status is not None:
                await upstream.up()
            client = PriceChartingClient(TOKEN, rate_limit=0)
            await client.start()
            try:
                with mock.patch.object(pricecharting, "BASE_URL", upstream.url):
                    return await client.health()
            finally:
                await client.close()
                await upstream.down()

        return asyncio.run(run())

    def test_price_data_means_the_token_works(self) -> None:
        self.assertIs(self.verdict(200, {"status": "success", "loose-price": 1732}), True)

    def test_identity_without_a_price_is_a_token_with_no_entitlement(self) -> None:
        self.assertIs(self.verdict(200, {"status": "success", "product-name": "Charizard #4"}), False)

    def test_a_refused_token_is_a_verdict(self) -> None:
        self.assertIs(self.verdict(200, {"status": "error", "error-message": "bad token"}), False)
        self.assertIs(self.verdict(403), False)

    def test_no_answer_is_not_a_verdict(self) -> None:
        self.assertIsNone(self.verdict(None))  # connection refused
        self.assertIsNone(self.verdict(503))
        self.assertIsNone(self.verdict(429))

    def test_json_that_is_not_a_response_is_no_answer_either(self) -> None:
        # Something answering in PriceCharting's place: nothing was learned
        # about the token, and nothing may blow up on the way to saying so.
        for payload in ([1, 2, 3], "maintenance", 0):
            with self.subTest(payload=payload):
                self.assertIsNone(self.verdict(200, payload))

    def test_the_token_stays_out_of_the_log(self) -> None:
        # A redirect loop is reported by the HTTP client with the whole URL,
        # query string included — and the token is in the query string.
        async def loop(request: web.Request) -> web.Response:
            raise web.HTTPFound(request.path_qs)

        with self.assertLogs("chat_cards.pricecharting", level="INFO") as logs:
            self.assertIsNone(self.verdict(200, respond=loop))
        self.assertIn("TooManyRedirects", "\n".join(logs.output))
        self.assertNotIn(TOKEN, "\n".join(logs.output))


class PriceTests(unittest.TestCase):
    def test_a_garbage_answer_is_a_miss_not_an_exception(self) -> None:
        async def run() -> None:
            async def respond(_request: web.Request) -> web.Response:
                return web.json_response([1, 2, 3])

            upstream = Upstream(respond)
            await upstream.up()
            client = PriceChartingClient(TOKEN, rate_limit=0)
            await client.start()
            try:
                with mock.patch.object(pricecharting, "BASE_URL", upstream.url):
                    self.assertIsNone(await client.price("Mew ex", "151", "Scarlet & Violet 151"))
                self.assertEqual(client.last_miss, "PriceCharting did not answer")
            finally:
                await client.close()
                await upstream.down()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
