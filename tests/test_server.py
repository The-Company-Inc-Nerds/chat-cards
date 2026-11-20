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
import socket
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
from chat_cards import __main__ as entry  # noqa: E402
from chat_cards import server, state  # noqa: E402
from chat_cards.cardsight import CardSightClient, Valuation  # noqa: E402
from chat_cards.config import Config  # noqa: E402
from chat_cards.state import Card, Session, Store  # noqa: E402

HANG = object()


class FakeCardSight:
    """Stands in for CardSightClient. `auth` scripts health_auth(), one entry per call."""

    configured = True

    def __init__(self, auth: list | None = None) -> None:
        self._auth = list(auth or [True])
        self.auth_calls = 0
        self.identify_calls = 0
        self.pricing_calls = 0
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
        self.pricing_calls += 1
        return Valuation(value=4.2, sample_size=3, basis="sold")

    async def card_image(self, card_id: str) -> tuple[bytes, str]:
        self.image_calls += 1
        return self.image


class FakePriceCharting:
    """Stands in for PriceChartingClient. `health` scripts health(), one entry per call."""

    configured = True

    def __init__(self, health: list, price: Valuation | None = None) -> None:
        self._health = list(health)
        self._price = price
        self.health_calls = 0

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def health(self):
        self.health_calls += 1
        verdict = self._health.pop(0) if len(self._health) > 1 else self._health[0]
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    async def price(self, *_args, **_kwargs):
        return self._price


def config(**overrides) -> Config:
    overrides.setdefault("state_dir", pathlib.Path(tempfile.mkdtemp()))
    overrides.setdefault("api_key", "not-a-real-key")
    return Config(**overrides)


def make_app(cfg: Config, client=None, pricecharting=None) -> web.Application:
    with warnings.catch_warnings():
        # The app keeps its state under plain string keys, by design.
        warnings.simplefilter("ignore", web.NotAppKeyWarning)
        app = server.create_app(cfg)
        if client is not None:
            app["client"] = client
        if pricecharting is not None:
            app["pricecharting"] = pricecharting
    return app


@contextlib.asynccontextmanager
async def serving(cfg: Config, client=None, pricecharting=None):
    """Start the app the way run_app does: startup hooks, then the listening socket."""
    app = make_app(cfg, client, pricecharting)
    runner = web.AppRunner(app)
    # Start-up that waits on anything is the regression these tests exist for;
    # it should fail one, not hang the run.
    await asyncio.wait_for(runner.setup(), timeout=5)
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        base = "http://127.0.0.1:%d" % runner.addresses[0][1]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:

            async def get(path: str):
                async with http.get(base + path) as response:
                    kind = response.headers.get("Content-Type", "")
                    return response.status, await (response.json() if "json" in kind else response.read())

            async def post(path: str, body: dict | aiohttp.FormData):
                sent = {"data": body} if isinstance(body, aiohttp.FormData) else {"json": body}
                async with http.post(base + path, **sent) as response:
                    return response.status, await response.json()

            yield app, get, post
    finally:
        await runner.cleanup()


def fast_retries():
    return mock.patch.object(server, "CHECK_RETRY_DELAYS", (0.02,))


def short_grace():
    return mock.patch.object(server, "INTERRUPTED_SCAN_GRACE", 0.05)


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
            with fast_retries(), self.assertLogs("chat_cards.server", level="ERROR"):
                async with serving(config(), client) as (_app, get, _post):
                    # No verdict came of it, so it is asked again like any
                    # other check that could not be made.
                    await until(lambda: client.auth_calls >= 3)
                    status, health = await get("/healthz")
                    self.assertEqual(status, 200)
                    self.assertIsNone(health["cardsight_auth"])
                    self.assertEqual(health["checks"], "pending")
                    status, _ = await get("/overlay")
                    self.assertEqual(status, 200)

        asyncio.run(run())

    def test_one_check_blowing_up_does_not_cost_the_other_its_answer(self) -> None:
        async def run() -> None:
            # PriceCharting is asked first; whatever it throws, CardSight is still asked.
            broken = FakePriceCharting([RuntimeError("boom"), True])
            with fast_retries(), self.assertLogs("chat_cards.server", level="ERROR"):
                async with serving(config(), FakeCardSight(), broken) as (app, get, _post):
                    await until(lambda: app["health"]["checks"] == "done")
                    _, health = await get("/healthz")
                    self.assertIs(health["cardsight_auth"], True)
                    self.assertIs(health["pricecharting"], True)
                    self.assertEqual(broken.health_calls, 2)

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

    def test_a_verdict_is_not_asked_for_again_while_the_other_is_awaited(self) -> None:
        async def run() -> None:
            # CardSight has refused the key; PriceCharting takes three tries to reach.
            refused, flaky = FakeCardSight([False]), FakePriceCharting([None, None, True])
            with fast_retries(), self.assertLogs("chat_cards.server", level="ERROR"):
                async with serving(config(), refused, flaky) as (app, _get, _post):
                    await until(lambda: app["health"]["checks"] == "done")
            self.assertEqual((flaky.health_calls, refused.auth_calls), (3, 1))

            # The other way round: the token is useless, CardSight takes three tries.
            useless, slow = FakePriceCharting([False]), FakeCardSight([None, None, True])
            with fast_retries(), self.assertLogs("chat_cards.server", level="ERROR"):
                async with serving(config(), slow, useless) as (app, _get, _post):
                    await until(lambda: app["health"]["checks"] == "done")
            self.assertEqual((slow.auth_calls, useless.health_calls), (3, 1))

        asyncio.run(run())

    def test_an_unreachable_upstream_is_asked_less_and_less_often(self) -> None:
        # The pace is the whole protection for an upstream that suspends
        # accounts for hammering it, so it is pinned: each wait the loop asks
        # for is recorded and skipped.
        waits: list[float] = []
        real_sleep = asyncio.sleep

        async def sleep(seconds: float) -> None:
            waits.append(seconds)
            await real_sleep(0)

        client = FakeCardSight([None] * 6 + [True])
        app = make_app(config(), client)
        with mock.patch.object(server.asyncio, "sleep", sleep):
            with self.assertNoLogs("chat_cards.server", level="ERROR"):
                asyncio.run(server._check_upstreams(app))

        self.assertEqual(waits, [5.0, 15.0, 60.0, 300.0, 300.0, 300.0])
        self.assertEqual(client.auth_calls, 7)
        self.assertEqual(app["health"]["checks"], "done")

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

    def test_a_client_that_stopped_reading_does_not_hold_up_a_stop_either(self) -> None:
        # A frozen browser source, a phone out of Wi-Fi range: still connected,
        # taking nothing in. Its close frame has nowhere to go.
        async def run() -> None:
            cfg = config(api_key=None, mock=True)
            # One snapshot of this session is more than the socket buffers hold.
            cards = [Card(uid=f"{n:012d}", seq=n, status="priced", name="x" * 500) for n in range(400)]
            cfg.session_file.write_text(json.dumps(Session(seq=400, cards=cards).to_json()))

            app = make_app(cfg)
            runner = web.AppRunner(app, shutdown_timeout=0.2)
            await runner.setup()
            # Small buffers at both ends (accepted connections inherit the
            # listener's), so the server is left holding what it tried to send.
            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            listener.bind(("127.0.0.1", 0))
            await web.SockSite(runner, listener).start()

            loop = asyncio.get_running_loop()
            peer = socket.socket()
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            peer.setblocking(False)
            try:
                await loop.sock_connect(peer, listener.getsockname())
                await loop.sock_sendall(
                    peer,
                    b"GET /ws HTTP/1.1\r\nHost: overlay\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n",
                )
                await until(lambda: app["sockets"])
                await asyncio.sleep(0.1)  # the hello snapshot is now stuck behind the full socket

                started = loop.time()
                with self.assertLogs("chat_cards.server", level="INFO"):
                    await asyncio.wait_for(runner.cleanup(), timeout=10)
                self.assertLess(loop.time() - started, 5.0)

                # Read out what the server was left holding, so its end closes
                # in good order rather than on a reset.
                async def drain() -> None:
                    while await loop.sock_recv(peer, 1 << 16):
                        pass

                await asyncio.wait_for(drain(), timeout=5)
            finally:
                peer.close()

        with mock.patch.object(server, "SOCKET_CLOSE_TIMEOUT", 0.2):
            asyncio.run(run())

    def test_a_request_in_flight_gets_seconds_not_a_minute(self) -> None:
        # aiohttp's own default would let one slow upload keep the overlay
        # away for a minute on every restart.
        with mock.patch.object(entry, "config_from_args", return_value=config(api_key=None, mock=True)), \
                mock.patch.object(entry.logging, "basicConfig"), \
                mock.patch.object(entry.web, "run_app") as run_app, \
                warnings.catch_warnings():
            warnings.simplefilter("ignore", web.NotAppKeyWarning)
            with self.assertLogs("chat_cards", level="INFO"):
                entry.main()
        self.assertLessEqual(run_app.call_args.kwargs["shutdown_timeout"], 5.0)


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
    def setUp(self) -> None:
        grace = short_grace()
        grace.start()
        self.addCleanup(grace.stop)

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

    def test_the_drop_is_announced_once_the_pages_have_reconnected(self) -> None:
        # The phone says "scan that card again" when it sees a card fail. Dropped
        # before the port is even open, the scan would vanish without a word.
        state_dir = pathlib.Path(tempfile.mkdtemp())
        uids = saved_session(state_dir)

        async def run() -> dict:
            with self.assertLogs("chat_cards.server", level="WARNING"):
                async with serving(config(state_dir=state_dir), FakeCardSight()) as (app, _get, _post):
                    phone = app["store"].subscribe()  # the page is back
                    while True:
                        message = await asyncio.wait_for(phone.get(), timeout=5)
                        if message["event"] == "card_failed":
                            return message

        # Long enough for the test to be listening, as a page would be.
        with mock.patch.object(server, "INTERRUPTED_SCAN_GRACE", 0.5):
            message = asyncio.run(run())
        self.assertEqual(message["card_uid"], uids["uploaded"])
        self.assertIn("scan this card again", message["reason"])
        self.assertNotIn(uids["uploaded"], [card["uid"] for card in message["state"]["cards"]])

    def test_a_scan_corrected_by_hand_meanwhile_is_not_dropped(self) -> None:
        state_dir = pathlib.Path(tempfile.mkdtemp())
        uids = saved_session(state_dir)

        async def run() -> dict[str, dict]:
            with self.assertLogs("chat_cards.server", level="WARNING"):
                async with serving(config(state_dir=state_dir), FakeCardSight()) as (app, get, _post):
                    await app["store"].update_card(
                        uids["uploaded"], status="priced", name="Typed in", value=2.0, value_source="manual"
                    )
                    await asyncio.sleep(0.7)  # well past the moment it would have been dropped
                    _, state = await get("/api/state")
                    return {card["uid"]: card for card in state["cards"]}

        with mock.patch.object(server, "INTERRUPTED_SCAN_GRACE", 0.5):
            cards = asyncio.run(run())
        self.assertEqual(cards[uids["uploaded"]]["name"], "Typed in")

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

    def test_an_interrupted_price_waits_for_an_upstream_to_be_reachable(self) -> None:
        # The box is back before its uplink. Looked up straight away, the price
        # would come back "unavailable" and stay that way.
        state_dir = pathlib.Path(tempfile.mkdtemp())
        uids = saved_session(state_dir)
        client = FakeCardSight([None])  # not reachable, however often it is asked

        async def run() -> None:
            with fast_retries(), self.assertLogs("chat_cards.server", level="WARNING"):
                async with serving(config(state_dir=state_dir), client) as (app, get, _post):
                    await until(lambda: client.auth_calls >= 3)
                    _, state = await get("/api/state")
                    waiting = {card["uid"]: card for card in state["cards"]}[uids["identified"]]
                    self.assertEqual(
                        (waiting["status"], waiting["name"], waiting["value"]), ("identifying", "Mew ex", None)
                    )
                    self.assertEqual(client.pricing_calls, 0)

                    client._auth = [True]  # the uplink is there
                    await until(lambda: not app["store"].unfinished_cards())
                    _, state = await get("/api/state")
                    priced = {card["uid"]: card for card in state["cards"]}[uids["identified"]]
                    self.assertEqual(
                        (priced["status"], priced["value"], priced["error"]), ("priced", 4.2, None)
                    )
                    self.assertEqual(client.pricing_calls, 1)

        asyncio.run(run())

    def test_one_upstream_answering_is_enough_for_it(self) -> None:
        # Either answer shows the uplink is there. The price is then looked up
        # as it would be for a scan made now, with whichever API is away missing.
        cases = {
            "PriceCharting away": (FakeCardSight(), FakePriceCharting([None]), 4.2),
            "CardSight away": (
                FakeCardSight([None]),
                FakePriceCharting([True], price=Valuation(value=7.5, basis="pricecharting")),
                7.5,
            ),
        }
        for label, (cardsight, pricecharting, expected) in cases.items():
            with self.subTest(label):
                state_dir = pathlib.Path(tempfile.mkdtemp())
                uids = saved_session(state_dir)

                async def run() -> None:
                    cfg = config(state_dir=state_dir)
                    with fast_retries(), self.assertLogs("chat_cards", level="WARNING"):
                        async with serving(cfg, cardsight, pricecharting) as (app, get, _post):
                            await until(lambda: not app["store"].unfinished_cards())
                            self.assertEqual(app["health"]["checks"], "pending")
                            _, state = await get("/api/state")
                            priced = {card["uid"]: card for card in state["cards"]}[uids["identified"]]
                            self.assertEqual(priced["value"], expected)

                asyncio.run(run())

    def test_nor_for_checks_that_have_given_up(self) -> None:
        with mock.patch.object(server, "_check_upstreams_once", side_effect=RuntimeError("boom")):
            uids, cards, _client = self.cards_after_restart()
        self.assertEqual(cards[uids["identified"]]["value"], 4.2)

    def test_a_price_typed_in_while_it_waited_is_left_alone(self) -> None:
        state_dir = pathlib.Path(tempfile.mkdtemp())
        uids = saved_session(state_dir)
        client = FakeCardSight([None])

        async def run() -> None:
            with fast_retries(), self.assertLogs("chat_cards.server", level="WARNING"):
                async with serving(config(state_dir=state_dir), client) as (app, get, _post):
                    await app["store"].update_card(
                        uids["identified"], status="priced", value=9.0, value_source="manual"
                    )
                    client._auth = [True]
                    await until(lambda: app["health"]["checks"] == "done")
                    await asyncio.sleep(0.05)
                    _, state = await get("/api/state")
                    card = {card["uid"]: card for card in state["cards"]}[uids["identified"]]
                    self.assertEqual((card["value"], card["value_source"]), (9.0, "manual"))
                    self.assertEqual(client.pricing_calls, 0)

        asyncio.run(run())

    def test_a_state_file_that_cannot_be_written_does_not_stop_the_start(self) -> None:
        # A full disk, say. Settling the interrupted scan writes the session; a
        # start that waited on that write would end there, and so would the next.
        state_dir = pathlib.Path(tempfile.mkdtemp())

        async def fill() -> str:
            store = Store(state_dir / "session.json", 5)
            await store.open_pack(5, "Ann", None)
            return (await store.add_card()).uid

        uid = asyncio.run(fill())

        async def run() -> None:
            disk_full = mock.patch.object(
                state.os, "fsync", side_effect=OSError(28, "No space left on device")
            )
            with disk_full, self.assertLogs("chat_cards.server", level="ERROR") as logs:
                async with serving(config(state_dir=state_dir), FakeCardSight()) as (app, get, _post):
                    status, page = await get("/overlay")
                    self.assertEqual(status, 200)
                    self.assertIn(b"reel__track", page)
                    # Off the reel all the same, rather than a placeholder for the night.
                    await until(lambda: not app["store"].unfinished_cards())
                    _, snapshot = await get("/api/state")
                    self.assertEqual(snapshot["cards"], [])
                    self.assertEqual(snapshot["pack"]["winner"], "Ann")
            self.assertTrue(any(uid in line for line in logs.output))

        asyncio.run(run())

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


class CaptureTests(unittest.TestCase):
    """The producer's photo of a scan: written like the rest of the state, never served empty."""

    PHOTO = b"\xff\xd8 a photo"

    def scan(self, then) -> None:
        class StillIdentifying(FakeCardSight):
            async def identify(self, image: bytes, content_type: str = "image/jpeg"):
                await asyncio.Event().wait()  # the photo is all there is of this card

        async def run() -> None:
            cfg = config()
            async with serving(cfg, StillIdentifying()) as (_app, get, post):
                form = aiohttp.FormData()
                form.add_field("image", self.PHOTO, filename="card.jpg", content_type="image/jpeg")
                with mock.patch.object(server, "write_atomic", wraps=server.write_atomic) as write:
                    status, scanned = await post("/api/scan", form)
                self.assertEqual(status, 200)
                await then(cfg, get, scanned["uid"], write)

        asyncio.run(run())

    def test_the_photo_goes_through_the_atomic_writer(self) -> None:
        async def then(cfg, get, uid, write) -> None:
            write.assert_called_once_with(cfg.capture_dir / f"{uid}.bin", self.PHOTO)
            self.assertEqual(await get(f"/api/card/{uid}/image"), (200, self.PHOTO))

        self.scan(then)

    def test_an_empty_photo_is_not_served(self) -> None:
        async def then(cfg, get, uid, write) -> None:
            # What a power cut can leave of a file that was never forced to disk.
            (cfg.capture_dir / f"{uid}.bin").write_bytes(b"")
            status, _ = await get(f"/api/card/{uid}/image")
            self.assertEqual(status, 404)

        self.scan(then)


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
