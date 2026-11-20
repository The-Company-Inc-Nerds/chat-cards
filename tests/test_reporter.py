"""Tests for pack reporting (chat-cards → multichat ledger).

The summary builder is pure. The Reporter is driven against a stand-in for
multichat's /api/turn-report on loopback, which is taken down and brought back
to exercise the resend. Dependency-light: stdlib unittest, plus the aiohttp the
app itself needs. Run from the repo root with:

    PYTHONPATH=src python -m unittest discover -s tests

(the sys.path shim below also lets `python -m unittest tests.test_reporter` work
without setting PYTHONPATH).
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from aiohttp import web  # noqa: E402

from _support import Upstream, until  # noqa: E402
from chat_cards.reporter import Reporter, pack_summary  # noqa: E402
from chat_cards.state import Store  # noqa: E402


def _snapshot() -> dict:
    return {
        "packs": [
            {
                "id": "p1",
                "index": 3,
                "size": 5,
                "winner": "Ann",
                "label": "seat 3",
                "ref": "42",
                "opened_at": 1_700_000_000.0,
                "closed_at": None,
            },
            {"id": "p0", "index": 2, "size": 5, "winner": "Bo", "ref": None, "opened_at": 1.0},
        ],
        "cards": [
            {
                "uid": "c1",
                "pack_id": "p1",
                "name": "Charizard",
                "number": "4/102",
                "set_name": "Base Set",
                "rarity": "Rare Holo",
                "value": 250.0,
                "image_url": "/api/card/c1/image?v=catalog-0",
            },
            {"uid": "c2", "pack_id": "p1", "name": "Energy", "value": 0.1, "image_url": None},
            {"uid": "c3", "pack_id": "p1", "name": "Pending", "value": None},
            {"uid": "cX", "pack_id": "p0", "name": "Other", "value": 5.0},
        ],
    }


class PackSummaryTests(unittest.TestCase):
    def test_builds_summary_for_pack(self) -> None:
        s = pack_summary(_snapshot(), "p1")
        assert s is not None
        self.assertEqual(s["packId"], "p1")
        self.assertEqual(s["ref"], "42")
        self.assertEqual(s["winner"], "Ann")
        self.assertEqual(s["label"], "seat 3")
        self.assertEqual(s["index"], 3)
        self.assertEqual(s["size"], 5)
        # Seconds → epoch ms; open pack has closedAt None.
        self.assertEqual(s["openedAt"], 1_700_000_000_000)
        self.assertIsNone(s["closedAt"])
        # Only this pack's 3 cards; unpriced card counts as 0 toward the total.
        self.assertEqual(s["cardCount"], 3)
        self.assertEqual(s["totalValue"], 250.10)

    def test_card_fields_mapped_to_wire_shape(self) -> None:
        s = pack_summary(_snapshot(), "p1")
        assert s is not None
        first = s["cards"][0]
        self.assertEqual(first["name"], "Charizard")
        self.assertEqual(first["number"], "4/102")
        self.assertEqual(first["set"], "Base Set")
        self.assertEqual(first["rarity"], "Rare Holo")
        self.assertEqual(first["value"], 250.0)
        self.assertEqual(first["image"], "/api/card/c1/image?v=catalog-0")
        # An unpriced card is reported with value 0.0 (not None).
        self.assertEqual(s["cards"][2]["value"], 0.0)

    def test_only_this_packs_cards(self) -> None:
        s = pack_summary(_snapshot(), "p0")
        assert s is not None
        self.assertEqual(s["cardCount"], 1)
        self.assertEqual(s["totalValue"], 5.0)
        self.assertIsNone(s["ref"])

    def test_unknown_pack_returns_none(self) -> None:
        self.assertIsNone(pack_summary(_snapshot(), "nope"))


# Long enough that no test waits for it: a resend that happens anyway was
# triggered by something else, which is what those tests are about.
NEVER = (3600.0,)


class Sink(Upstream):
    """multichat's /api/turn-report: keeps the last report per pack, like its upsert."""

    @property
    def target(self) -> str:
        return f"{self.url}/api/turn-report"

    @property
    def posted(self) -> list[str]:
        """Pack ids in the order their reports arrived, repeats included."""
        return [body["packId"] for _, _, body in self.requests]

    @property
    def ledger(self) -> dict[str, dict]:
        return {body["packId"]: body for _, _, body in self.requests}


def new_store() -> Store:
    return Store(pathlib.Path(tempfile.mkdtemp()) / "session.json", 5)


async def unavailable(_request: web.Request) -> web.Response:
    return web.Response(status=503, text="starting up")


class ReporterTests(unittest.TestCase):
    def test_start_resends_every_pack_once(self) -> None:
        async def run() -> None:
            store, sink = new_store(), Sink()
            first = await store.open_pack(5, "Ann", None, ref="turn-1")
            second = await store.open_pack(5, "Bo", None, ref="turn-2")  # closes the first
            await sink.up()

            reporter = Reporter(store, sink.target, None, retry_delays=NEVER)
            await reporter.start()
            try:
                await until(lambda: len(sink.posted) == 2)
                await asyncio.sleep(0.1)  # and then it is quiet
                self.assertEqual(sink.posted, [first.id, second.id])
                self.assertIsNotNone(sink.ledger[first.id]["closedAt"])
                self.assertEqual(sink.ledger[second.id]["ref"], "turn-2")
            finally:
                await reporter.close()
                await sink.down()

        asyncio.run(run())

    def test_an_empty_session_makes_no_request(self) -> None:
        async def run() -> None:
            store, sink = new_store(), Sink()
            await sink.up()
            reporter = Reporter(store, sink.target, None, retry_delays=(0.01,))
            await reporter.start()
            try:
                await asyncio.sleep(0.1)
                self.assertEqual(sink.requests, [])
            finally:
                await reporter.close()
                await sink.down()

        asyncio.run(run())

    def test_everything_is_resent_on_the_first_success_after_a_failure(self) -> None:
        async def run() -> None:
            store, sink = new_store(), Sink()
            await sink.up()
            reporter = Reporter(store, sink.target, None, retry_delays=NEVER)
            await reporter.start()
            try:
                first = await store.open_pack(5, "Ann", None)
                await until(lambda: sink.posted == [first.id])

                # multichat restarts; a whole pack comes and goes meanwhile.
                await sink.down()
                with self.assertLogs("chat_cards.reporter", level="WARNING") as logs:
                    second = await store.open_pack(5, "Bo", None)
                    card = await store.add_card()
                    await store.update_card(card.uid, event="card_priced", name="Mew", value=62.0)
                    await store.close_pack()
                    await until(lambda: len(logs.records) == 4)  # one failed POST per event
                self.assertEqual(sink.posted, [first.id])

                # Back up: nothing happens by itself here (NEVER), but the next
                # report that lands brings the rest with it.
                await sink.up()
                await asyncio.sleep(0.1)
                self.assertEqual(sink.posted, [first.id])
                third = await store.open_pack(5, "Cy", None)
                await until(lambda: len(sink.posted) == 4)
                await asyncio.sleep(0.1)

                # The live pack went first and was not sent twice.
                self.assertEqual(sink.posted[1], third.id)
                self.assertEqual(sorted(sink.posted[2:]), sorted([first.id, second.id]))
                missed = sink.ledger[second.id]
                self.assertEqual(missed["totalValue"], 62.0)
                self.assertEqual(missed["cardCount"], 1)
                self.assertIsNotNone(missed["closedAt"])
                self.assertIsNotNone(sink.ledger[first.id]["closedAt"])
            finally:
                await reporter.close()
                await sink.down()

        asyncio.run(run())

    def test_keeps_trying_until_multichat_is_up(self) -> None:
        async def run() -> None:
            store, sink = new_store(), Sink()
            first = await store.open_pack(5, "Ann", None)
            second = await store.open_pack(5, "Bo", None)

            # Both services start together at boot; multichat is not listening yet.
            reporter = Reporter(store, sink.target, None, retry_delays=(0.02,))
            with self.assertLogs("chat_cards.reporter", level="DEBUG") as logs:
                await reporter.start()
                await until(lambda: len(logs.records) >= 6)
            try:
                # Several attempts were refused, but only the first is worth a warning.
                warnings = [r.getMessage() for r in logs.records if r.levelname == "WARNING"]
                self.assertEqual(len(warnings), 2)
                self.assertEqual(len([w for w in warnings if "retrying" in w]), 1)

                await sink.up()
                await until(lambda: sorted(sink.posted) == sorted([first.id, second.id]))
            finally:
                await reporter.close()
                await sink.down()

        asyncio.run(run())

    def test_a_sink_that_is_not_taking_reports_costs_one_post_per_attempt(self) -> None:
        async def run() -> None:
            store, sink = new_store(), Sink(unavailable)
            packs = [await store.open_pack(5, f"winner-{n}", None) for n in range(6)]
            await sink.up()

            # Second attempt 0.05s after the first, then nothing for an hour.
            reporter = Reporter(store, sink.target, None, retry_delays=(0.05, 3600.0))
            with self.assertLogs("chat_cards.reporter", level="WARNING"):
                await reporter.start()
                await until(lambda: len(sink.posted) == 2)
                await asyncio.sleep(0.2)
            try:
                # Six packs are owed; each attempt gave up at the first refusal.
                self.assertEqual(sink.posted, [packs[0].id, packs[0].id])
            finally:
                await reporter.close()
                await sink.down()

        asyncio.run(run())

    def test_a_live_event_goes_ahead_of_the_backlog(self) -> None:
        async def run() -> None:
            # The sink sits on the first report until the test lets it go, so the
            # live event is certain to arrive while the resend is under way.
            gate = asyncio.Event()

            async def held(_request: web.Request) -> web.Response:
                await gate.wait()
                return web.json_response({"ok": True})

            store, sink = new_store(), Sink(held)
            old = [await store.open_pack(5, f"winner-{n}", None) for n in range(10)]
            await sink.up()

            reporter = Reporter(store, sink.target, None, retry_delays=NEVER)
            await reporter.start()
            try:
                await until(lambda: len(sink.posted) == 1)
                live = await store.open_pack(5, "on screen now", None)
                gate.set()
                await until(lambda: len(sink.posted) == 11)
                await asyncio.sleep(0.1)

                # Not queued behind the nine packs still owed, and sent exactly once.
                self.assertEqual(sink.posted[:2], [old[0].id, live.id])
                self.assertEqual(sorted(sink.posted), sorted(p.id for p in [*old, live]))
                # The pack it closed was still owed, and went out as it stands now.
                self.assertIsNotNone(sink.ledger[old[-1].id]["closedAt"])
            finally:
                await reporter.close()
                await sink.down()

        asyncio.run(run())

    def test_a_removed_card_is_reported(self) -> None:
        async def run() -> None:
            store, sink = new_store(), Sink()
            await sink.up()
            reporter = Reporter(store, sink.target, None, retry_delays=NEVER)
            await reporter.start()
            try:
                pack = await store.open_pack(5, "Ann", None)
                card = await store.add_card()
                await until(lambda: sink.ledger.get(pack.id, {}).get("cardCount") == 1)
                await store.remove_card(card.uid)
                await until(lambda: sink.ledger[pack.id]["cardCount"] == 0)
            finally:
                await reporter.close()
                await sink.down()

        asyncio.run(run())

    def test_a_hung_sink_does_not_hold_up_the_reveal(self) -> None:
        async def hang(_request: web.Request) -> web.Response:
            await asyncio.sleep(3600)
            return web.Response()

        async def run() -> None:
            store, sink = new_store(), Sink(hang)
            await sink.up()
            reporter = Reporter(store, sink.target, None, retry_delays=NEVER)
            await reporter.start()
            try:
                started = time.monotonic()
                await store.open_pack(5, "Ann", None)
                for _ in range(5):
                    await store.add_card()
                # Each POST would take its full five-second timeout to give up.
                self.assertLess(time.monotonic() - started, 3.0)
                self.assertEqual(store.snapshot()["pack"]["revealed"], 5)
            finally:
                await reporter.close()
                await sink.down()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
