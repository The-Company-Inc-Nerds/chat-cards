"""Tests for the pack-report summary builder (chat-cards → multichat ledger).

Dependency-light: stdlib unittest only. Run from the repo root with:

    PYTHONPATH=src python -m unittest discover -s tests

(the sys.path shim below also lets `python -m unittest tests.test_reporter` work
without setting PYTHONPATH).
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from chat_cards.reporter import pack_summary  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
