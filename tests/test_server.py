"""Tests for server start-up, /healthz and what a restart does to state (no network).

The app is started for real on a loopback port. The upstream APIs are fakes, or
a loopback port with nothing behind it.

    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import sys
import tempfile
import unittest
import warnings
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from _support import Upstream, free_port, until  # noqa: E402
from chat_cards import server  # noqa: E402
from chat_cards.cardsight import CardSightClient, Valuation  # noqa: E402
from chat_cards.config import Config  # noqa: E402
from chat_cards.state import Store  # noqa: E402

HANG = object()


class FakeCardSight:
    """Stands in for CardSightClient. `auth` scripts health_auth(), one entry per call."""

    configured = True

    def __init__(self, auth: list | None = None) -> None:
        self._auth = list(auth or [True])
        self.auth_calls = 0
        self.identify_calls = 0
        self.image_calls = 0
        self.image: tuple[bytes, str] = (b"\x89PNG art", "image/png")

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def health_auth(self):
        self.auth_calls += 1
        verdict = self._auth.pop(0) if len(self._auth) > 1 else self._auth[0]
        if verdict is HANG:
            await asyncio.Event().wait()
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    async def identify(self, image: bytes, content_type: str = "image/jpeg"):
        self.identify_calls += 1
        raise AssertionError("a billed identify call was made")

    async def pricing(self, card_id, parallel_id, **_kwargs) -> Valuation:
        return Valuation(value=4.2, sample_size=3, basis="sold")

    async def card_image(self, card_id: str) -> tuple[bytes, str]:
        self.image_calls += 1
        return self.image


class FakePriceCharting:
    """Stands in for PriceChartingClient. `health` scripts health(), one entry per call."""

    configured = True

    def __init__(self, health: list) -> None:
        self._health = list(health)
        self.health_calls = 0

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def health(self):
        self.health_calls += 1
        return self._health.pop(0) if len(self._health) > 1 else self._health[0]

    async def price(self, *_args, **_kwargs):
        return None


def config(**overrides) -> Config:
    overrides.setdefault("state_dir", pathlib.Path(tempfile.mkdtemp()))
    overrides.setdefault("api_key", "not-a-real-key")
    return Config(**overrides)


@contextlib.asynccontextmanager
async def serving(cfg: Config, client=None, pricecharting=None):
    """Start the app the way run_app does: startup hooks, then the listening socket."""
    with warnings.catch_warnings():
        # The app keeps its state under plain string keys, by design.
        warnings.simplefilter("ignore", web.NotAppKeyWarning)
        app = server.create_app(cfg)
        if client is not None:
            app["client"] = client
        if pricecharting is not None:
            app["pricecharting"] = pricecharting
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        base = "http://127.0.0.1:%d" % runner.addresses[0][1]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:

            async def get(path: str):
                async with http.get(base + path) as response:
                    kind = response.headers.get("Content-Type", "")
                    return response.status, await (response.json() if "json" in kind else response.read())

            async def post(path: str, body: dict):
                async with http.post(base + path, json=body) as response:
                    return response.status, await response.json()

            yield app, get, post
    finally:
        await runner.cleanup()


def fast_retries():
    return mock.patch.object(server, "CHECK_RETRY_DELAYS", (0.02,))


class StartupTests(unittest.TestCase):
    def test_serves_before_the_checks_have_an_answer(self) -> None:
        async def run() -> None:
            client = FakeCardSight([HANG])
            started = asyncio.get_running_loop().time()
            async with serving(config(), client) as (_app, get, post):
                self.assertLess(asyncio.get_running_loop().time() - started, 2.0)

                status, health = await get("/healthz")
                self.assertEqual(status, 200)
                self.assertEqual(
                    health,
                    {
                        "ok": True,
                        "mock": False,
                        "cardsight_key": True,
                        "cardsight_auth": None,
                        "pricecharting": None,
                        "checks": "pending",
                    },
                )

                status, page = await get("/overlay")
                self.assertEqual(status, 200)
                self.assertIn(b"reel__track", page)
                status, _ = await get("/scan")
                self.assertEqual(status, 200)

                status, pack = await post("/api/pack", {"size": 5, "winner": "@ann", "ref": "turn-1"})
                self.assertEqual(status, 200)
                self.assertEqual(pack["ref"], "turn-1")
                self.assertEqual(client.auth_calls, 1)

        asyncio.run(run())

    def test_a_check_that_blows_up_cannot_take_the_server_down(self) -> None:
        async def run() -> None:
            client = FakeCardSight([RuntimeError("boom")])
            with self.assertLogs("chat_cards.server", level="ERROR"):
                async with serving(config(), client) as (_app, get, _post):
                    await until(lambda: _app["health"]["checks"] == "done")
                    status, health = await get("/healthz")
                    self.assertEqual(status, 200)
                    self.assertIsNone(health["cardsight_auth"])
                    status, _ = await get("/overlay")
                    self.assertEqual(status, 200)

        asyncio.run(run())

    def test_unreachable_cardsight_is_asked_again_until_it_answers(self) -> None:
        async def run() -> None:
            cardsight = Upstream()  # not listening yet: connections are refused
            cfg = config(api_base=cardsight.url)
            with fast_retries(), self.assertLogs("chat_cards", level="WARNING"):
                async with serving(cfg) as (app, get, post):
                    status, _ = await post("/api/pack", {"size": 5})
                    self.assertEqual(status, 200)

                    await asyncio.sleep(0.15)  # several refused attempts later
                    _, health = await get("/healthz")
                    self.assertEqual(health["cardsight_key"], True)
                    self.assertIsNone(health["cardsight_auth"])
                    self.assertEqual(health["checks"], "pending")

                    await cardsight.up()
                    await until(lambda: app["health"]["checks"] == "done")
                    _, health = await get("/healthz")
                    self.assertIs(health["cardsight_auth"], True)
                    self.assertEqual(cardsight.requests[0][:2], ("GET", "/health/auth"))
                    await cardsight.down()

        asyncio.run(run())

    def test_a_rejected_key_is_a_final_answer(self) -> None:
        async def run() -> None:
            client = FakeCardSight([False])
            with fast_retries(), self.assertLogs("chat_cards.server", level="ERROR"):
                async with serving(config(), client) as (app, get, _post):
                    await until(lambda: app["health"]["checks"] == "done")
                    await asyncio.sleep(0.1)
                    _, health = await get("/healthz")
                    self.assertIs(health["cardsight_auth"], False)
                    self.assertEqual(client.auth_calls, 1)

        asyncio.run(run())

    def test_pricecharting_verdicts(self) -> None:
        async def run() -> None:
            # Unreachable at first, then fine: asked again, and only as often as needed.
            flaky = FakePriceCharting([None, None, True])
            with fast_retries(), self.assertLogs("chat_cards.server", level="WARNING"):
                async with serving(config(), FakeCardSight(), flaky) as (app, get, _post):
                    await until(lambda: app["health"]["checks"] == "done")
                    _, health = await get("/healthz")
                    self.assertIs(health["pricecharting"], True)
                    self.assertEqual(flaky.health_calls, 3)

            # A token with no price data is a verdict, not something to retry.
            useless = FakePriceCharting([False])
            with fast_retries(), self.assertLogs("chat_cards.server", level="ERROR"):
                async with serving(config(), FakeCardSight(), useless) as (app, get, _post):
                    await until(lambda: app["health"]["checks"] == "done")
                    await asyncio.sleep(0.1)
                    _, health = await get("/healthz")
                    self.assertIs(health["pricecharting"], False)
                    self.assertIs(health["cardsight_auth"], True)
                    self.assertEqual(useless.health_calls, 1)

        asyncio.run(run())

    def test_nothing_to_check_without_a_key_or_in_mock_mode(self) -> None:
        async def run() -> None:
            with self.assertLogs("chat_cards.server", level="WARNING"):
                async with serving(config(api_key=None)) as (app, get, _post):
                    await until(lambda: app["health"]["checks"] == "done")
                    _, health = await get("/healthz")
                    self.assertEqual(health["cardsight_key"], False)
                    self.assertIsNone(health["cardsight_auth"])

            async with serving(config(api_key=None, mock=True)) as (app, get, _post):
                await until(lambda: app["health"]["checks"] == "done")
                _, health = await get("/healthz")
                self.assertEqual(health["mock"], True)
                self.assertIs(health["cardsight_auth"], True)
                self.assertIsNone(health["pricecharting"])

        asyncio.run(run())


class ShutdownTests(unittest.TestCase):
    def test_an_open_websocket_does_not_hold_up_a_stop(self) -> None:
        # The overlay in OBS is connected for as long as the box is up. Left to
        # itself aiohttp waits a minute for such a client before letting go.
        async def run() -> None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", web.NotAppKeyWarning)
                app = server.create_app(config(api_key=None, mock=True))
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = "http://127.0.0.1:%d" % runner.addresses[0][1]

            async with aiohttp.ClientSession() as http:
                ws = await http.ws_connect(base + "/ws")
                self.assertEqual((await ws.receive_json())["event"], "hello")

                loop = asyncio.get_running_loop()
                started = loop.time()
                await asyncio.wait_for(runner.cleanup(), timeout=20)
                self.assertLess(loop.time() - started, 3.0)

                # Told to go away, so the page reconnects instead of sitting on a dead socket.
                message = await ws.receive()
                self.assertEqual(message.type, aiohttp.WSMsgType.CLOSE)
                self.assertEqual(message.data, aiohttp.WSCloseCode.GOING_AWAY)

        asyncio.run(run())


class CardSightHealthTests(unittest.TestCase):
    """health_auth() against a socket: the three answers, and never an exception."""

    def verdict(self, status: int | None) -> bool | None:
        async def run() -> bool | None:
            async def respond(_request: web.Request) -> web.Response:
                return web.json_response({"status": "x"}, status=status)

            upstream = Upstream(respond)
            if status is not None:
                await upstream.up()
            client = CardSightClient("key", upstream.url, "pokemon", rate_limit=0)
            await client.start()
            try:
                return await client.health_auth()
            finally:
                await client.close()
                await upstream.down()

        return asyncio.run(run())

    def test_accepted(self) -> None:
        self.assertIs(self.verdict(200), True)

    def test_rejected(self) -> None:
        with self.assertLogs("chat_cards.cardsight", level="ERROR"):
            self.assertIs(self.verdict(401), False)

    def test_server_error_is_not_a_verdict(self) -> None:
        with self.assertLogs("chat_cards.cardsight", level="WARNING"):
            self.assertIsNone(self.verdict(503))

    def test_connection_refused_is_not_a_verdict_and_does_not_raise(self) -> None:
        with self.assertLogs("chat_cards.cardsight", level="WARNING"):
            self.assertIsNone(self.verdict(None))


def saved_session(state_dir: pathlib.Path) -> dict[str, str]:
    """A session.json as a process killed mid-pack leaves it. Returns card uids by role."""

    async def fill() -> dict[str, str]:
        store = Store(state_dir / "session.json", 5)
        await store.open_pack(5, "Ann", None)
        priced = await store.add_card()
        await store.update_card(priced.uid, status="priced", card_id="c-1", name="Pikachu", value=1.25)
        # Photo uploaded, identification never came back.
        uploaded = await store.add_card()
        # Identified — name and art already on stream — but not yet priced.
        identified = await store.add_card()
        await store.update_card(identified.uid, card_id="c-2", name="Mew ex", image_source="catalog")
        return {"priced": priced.uid, "uploaded": uploaded.uid, "identified": identified.uid}

    return asyncio.run(fill())


class RestartTests(unittest.TestCase):
    def cards_after_restart(self, **overrides) -> tuple[dict[str, str], dict[str, dict], FakeCardSight]:
        state_dir = pathlib.Path(tempfile.mkdtemp())
        uids = saved_session(state_dir)
        client = FakeCardSight()

        async def run() -> dict[str, dict]:
            cfg = config(state_dir=state_dir, **overrides)
            with self.assertLogs("chat_cards.server", level="WARNING"):
                async with serving(cfg, client) as (app, get, _post):
                    await until(lambda: not app["store"].unfinished_cards())
                    _, state = await get("/api/state")
                    return {card["uid"]: card for card in state["cards"]}

        return uids, asyncio.run(run()), client

    def test_a_scan_that_was_never_identified_is_dropped_not_sent_again(self) -> None:
        uids, cards, client = self.cards_after_restart()
        self.assertNotIn(uids["uploaded"], cards)
        # Identification is the billed call: a restart must not make one.
        self.assertEqual(client.identify_calls, 0)
        self.assertEqual(cards[uids["priced"]]["value"], 1.25)

    def test_kept_and_flagged_when_failed_scans_are_kept(self) -> None:
        uids, cards, client = self.cards_after_restart(auto_delete_unmatched=False)
        stranded = cards[uids["uploaded"]]
        self.assertEqual(stranded["status"], "error")
        self.assertIn("restart", stranded["error"])
        self.assertEqual(client.identify_calls, 0)

    def test_an_identified_card_keeps_its_identity_and_gets_its_price(self) -> None:
        uids, cards, client = self.cards_after_restart()
        resumed = cards[uids["identified"]]
        self.assertEqual(resumed["status"], "priced")
        self.assertEqual(resumed["name"], "Mew ex")
        self.assertEqual(resumed["value"], 4.2)
        self.assertEqual(client.identify_calls, 0)

    def test_the_settled_state_is_what_is_on_disk(self) -> None:
        state_dir = pathlib.Path(tempfile.mkdtemp())
        uids = saved_session(state_dir)

        async def run() -> None:
            with self.assertLogs("chat_cards.server", level="WARNING"):
                async with serving(config(state_dir=state_dir), FakeCardSight()) as (app, _get, _post):
                    await until(lambda: not app["store"].unfinished_cards())

        asyncio.run(run())
        on_disk = {c["uid"]: c["status"] for c in json.loads((state_dir / "session.json").read_text())["cards"]}
        self.assertEqual(on_disk, {uids["priced"]: "priced", uids["identified"]: "priced"})


class ImageCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config()
        self.client = FakeCardSight()
        self.app = {"config": self.cfg, "client": self.client}
        self.directory = self.cfg.image_cache_dir

    def cache(self, card_id: str = "card-1") -> bool:
        return asyncio.run(server._cache_catalog_image(self.app, card_id))

    def test_written_whole_and_fetched_once(self) -> None:
        self.assertTrue(self.cache())
        self.assertTrue(self.cache())
        self.assertEqual(self.client.image_calls, 1)
        # Nothing but the finished file: the write went through a rename.
        self.assertEqual([p.name for p in self.directory.iterdir()], ["card-1.png"])
        self.assertEqual((self.directory / "card-1.png").read_bytes(), b"\x89PNG art")

    def test_goes_through_the_atomic_writer(self) -> None:
        with mock.patch.object(server, "write_atomic") as write:
            self.assertTrue(self.cache())
        write.assert_called_once_with(self.directory / "card-1.png", b"\x89PNG art")

    def test_a_zero_length_file_is_a_miss(self) -> None:
        self.directory.mkdir(parents=True)
        (self.directory / "card-1.png").write_bytes(b"")
        self.assertIsNone(server._cached_image(self.directory, "card-1"))

        self.assertTrue(self.cache())
        self.assertEqual(self.client.image_calls, 1)
        self.assertEqual(server._cached_image(self.directory, "card-1").read_bytes(), b"\x89PNG art")

    def test_a_leftover_temp_file_is_not_the_image(self) -> None:
        self.directory.mkdir(parents=True)
        (self.directory / "card-1.png.tmp").write_bytes(b"half a pic")
        self.assertIsNone(server._cached_image(self.directory, "card-1"))

    def test_an_empty_download_is_not_cached(self) -> None:
        self.client.image = (b"", "image/png")
        self.assertFalse(self.cache())
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
