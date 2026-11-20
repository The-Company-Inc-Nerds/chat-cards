"""Tests for session persistence: crash-safe writes and unreadable state files.

    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from chat_cards import state  # noqa: E402
from chat_cards.state import Store, quarantine_path, write_atomic  # noqa: E402


def tmpdir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())


def asides(directory: pathlib.Path) -> list[pathlib.Path]:
    return sorted(directory.glob("session.json.corrupt-*"))


class WriteAtomicTests(unittest.TestCase):
    def test_data_is_on_disk_before_the_rename(self) -> None:
        path = tmpdir() / "session.json"
        path.write_bytes(b"old")
        order: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd: int) -> None:
            order.append("fsync")
            real_fsync(fd)

        def replace(src, dst) -> None:
            # The rename must find the complete new file already written out.
            self.assertEqual(pathlib.Path(src).read_bytes(), b"new")
            order.append("replace")
            real_replace(src, dst)

        with mock.patch.object(state.os, "fsync", fsync), mock.patch.object(state.os, "replace", replace):
            write_atomic(path, b"new")

        self.assertEqual(order, ["fsync", "replace"])
        self.assertEqual(path.read_bytes(), b"new")
        self.assertEqual([p.name for p in path.parent.iterdir()], ["session.json"])

    def test_a_write_that_dies_leaves_the_old_file(self) -> None:
        path = tmpdir() / "session.json"
        path.write_bytes(b"old")
        with mock.patch.object(state.os, "fsync", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                write_atomic(path, b"new")
        self.assertEqual(path.read_bytes(), b"old")

    def test_store_saves_through_it(self) -> None:
        path = tmpdir() / "session.json"
        store = Store(path, 5)
        with mock.patch.object(state.os, "fsync", wraps=os.fsync) as fsync:
            asyncio.run(store.open_pack(5, "Ann", None))
        self.assertEqual(fsync.call_count, 1)
        self.assertEqual(json.loads(path.read_text())["packs"][0]["winner"], "Ann")


class LoadTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        path = tmpdir() / "session.json"

        async def fill() -> None:
            store = Store(path, 5)
            await store.open_pack(5, "Ann", None, ref="turn-1")
            await store.add_card()

        asyncio.run(fill())
        store = Store(path, 5)
        store.load()
        snapshot = store.snapshot()
        self.assertEqual(snapshot["pack"]["ref"], "turn-1")
        self.assertEqual(snapshot["totals"]["count"], 1)
        self.assertEqual(asides(path.parent), [])

    def test_first_run_has_nothing_to_move_aside(self) -> None:
        path = tmpdir() / "session.json"
        Store(path, 5).load()
        self.assertEqual(list(path.parent.iterdir()), [])

    def test_unreadable_files_are_moved_aside_not_overwritten(self) -> None:
        cases = {
            "torn mid-write": b'{"version": 1, "packs": [{"id": "p1", "ind',
            "zero length": b"",
            "not a session": b"[1, 2, 3]",
            "pack missing its fields": b'{"packs": [{"winner": "Ann"}]}',
        }
        for label, content in cases.items():
            with self.subTest(label):
                path = tmpdir() / "session.json"
                path.write_bytes(content)

                store = Store(path, 5)
                with self.assertLogs("chat_cards.state", level="ERROR"):
                    store.load()

                # Up, and empty, rather than refusing to start.
                self.assertEqual(store.snapshot()["totals"]["packs"], 0)
                kept = asides(path.parent)
                self.assertEqual(len(kept), 1)
                self.assertEqual(kept[0].read_bytes(), content)
                self.assertFalse(path.exists())

                # The first change writes a new file and leaves the evidence alone.
                asyncio.run(store.open_pack(5, "Bo", None))
                self.assertEqual(json.loads(path.read_text())["packs"][0]["winner"], "Bo")
                self.assertEqual(kept[0].read_bytes(), content)

    def test_a_file_that_cannot_be_read_is_moved_aside_too(self) -> None:
        path = tmpdir() / "session.json"
        path.write_text('{"packs": []}')
        store = Store(path, 5)
        with mock.patch.object(pathlib.Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertLogs("chat_cards.state", level="ERROR"):
                store.load()
        self.assertEqual(len(asides(path.parent)), 1)
        self.assertFalse(path.exists())

    def test_still_starts_when_the_file_cannot_be_moved(self) -> None:
        path = tmpdir() / "session.json"
        path.write_bytes(b"{")
        store = Store(path, 5)
        with mock.patch.object(state.os, "replace", side_effect=PermissionError("denied")):
            with self.assertLogs("chat_cards.state", level="ERROR") as logs:
                store.load()
        self.assertEqual(store.snapshot()["totals"]["packs"], 0)
        self.assertTrue(any("overwrite" in line for line in logs.output))

    def test_aside_name_is_timestamped_next_to_the_original(self) -> None:
        aside = quarantine_path(pathlib.Path("/var/lib/chat-cards/session.json"), 1_700_000_000)
        self.assertEqual(str(aside), "/var/lib/chat-cards/session.json.corrupt-2023-11-14T22-13-20Z")


class CardTests(unittest.TestCase):
    def test_unfinished_cards_are_the_ones_still_identifying(self) -> None:
        async def run() -> None:
            store = Store(tmpdir() / "session.json", 5)
            done = await store.add_card()
            waiting = await store.add_card()
            await store.update_card(done.uid, status=state.STATUS_PRICED, value=1.0)
            self.assertEqual([c.uid for c in store.unfinished_cards()], [waiting.uid])

        asyncio.run(run())

    def test_removal_names_the_pack_the_card_was_in(self) -> None:
        # The card is gone from the snapshot by then, so a listener that wants
        # to know which pack changed has nothing else to go on.
        async def run() -> dict:
            store = Store(tmpdir() / "session.json", 5)
            pack = await store.open_pack(5, "Ann", None)
            card = await store.add_card()
            queue = store.subscribe()
            await store.remove_card(card.uid, event="card_failed", reason="no match")
            message = queue.get_nowait()
            self.assertEqual(message["pack_id"], pack.id)
            return message

        message = asyncio.run(run())
        self.assertEqual(message["event"], "card_failed")
        self.assertEqual(message["reason"], "no match")


if __name__ == "__main__":
    unittest.main()
