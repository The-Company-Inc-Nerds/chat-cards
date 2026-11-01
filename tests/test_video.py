"""Tests for the pure parts of the video pricer (no ffmpeg, no network).

    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from chat_cards.cardsight import Identification, Valuation  # noqa: E402
from chat_cards.video import (  # noqa: E402
    CardRow,
    Hold,
    Sighting,
    apply_running_totals,
    collapse_sightings,
    csv_rows,
    find_holds,
    format_timestamp,
    merge_similar_holds,
    parse_timestamp,
    pick_candidates,
    srt_text,
    trim_to_count,
)

SIZE = 16


def flat(level: int) -> bytes:
    return bytes([level]) * SIZE


def noise(seed: int) -> bytes:
    return bytes(((seed * 97 + i * 53) % 256) for i in range(SIZE))


def ident(card_id: str | None, name: str = "Card", confidence: str = "High") -> Identification:
    return Identification(card_id=card_id, name=name, confidence=confidence)


class TimestampTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(parse_timestamp("7.5"), 7.5)
        self.assertEqual(parse_timestamp("1:23.5"), 83.5)
        self.assertEqual(parse_timestamp("1:00:02"), 3602.0)
        with self.assertRaises(ValueError):
            parse_timestamp("")

    def test_format(self) -> None:
        self.assertEqual(format_timestamp(83.5), "1:23.500")
        self.assertEqual(format_timestamp(3602.25), "1:00:02.250")


class HoldTests(unittest.TestCase):
    def test_still_runs_between_motion_become_holds(self) -> None:
        # 4 fps: motion, 1s still on A, motion, 1s still on B, motion
        frames = [noise(i) for i in range(4)] + [flat(50)] * 4 + [noise(i) for i in range(10, 14)] + [flat(200)] * 4 + [noise(99)]
        holds = find_holds(frames, 4.0, threshold=5.0, min_hold=0.5)
        self.assertEqual(len(holds), 2)
        self.assertAlmostEqual(holds[0].start, 5 / 4)  # first frame after the jump into A
        self.assertEqual(holds[0].thumb, flat(50))
        self.assertEqual(holds[1].thumb, flat(200))

    def test_short_still_ignored(self) -> None:
        frames = [noise(1), flat(10), flat(10), noise(2)]
        self.assertEqual(find_holds(frames, 4.0, threshold=5.0, min_hold=1.0), [])

    def test_merge_same_view(self) -> None:
        a = Hold(1.0, 2.0, 1.5, flat(50))
        b = Hold(2.3, 3.5, 3.0, flat(52))
        c = Hold(4.0, 5.0, 4.5, flat(200))
        merged = merge_similar_holds([a, b, c])
        self.assertEqual(len(merged), 2)
        self.assertEqual((merged[0].start, merged[0].end, merged[0].best_time), (1.0, 3.5, 3.0))

    def test_candidates_longest_then_time_order(self) -> None:
        holds = [Hold(0, 0.5, 0, b""), Hold(1, 3, 2, b""), Hold(4, 5, 4.5, b"")]
        self.assertEqual([h.start for h in pick_candidates(holds, 2)], [1, 4])


class CollapseTests(unittest.TestCase):
    def test_runs_merge_and_unmatched_drop(self) -> None:
        sightings = [
            Sighting(1, 2, 1.5, ident("a", "Pikachu", "Medium")),
            Sighting(2.5, 3, 2.7, ident(None)),  # blur between two reads of Pikachu
            Sighting(3.2, 4, 3.5, ident("a", "Pikachu", "High")),
            Sighting(5, 6, 5.5, ident("b", "Mew")),
            Sighting(7, 8, 7.5, ident("a", "Pikachu")),  # a second copy later
        ]
        rows = collapse_sightings(sightings)
        self.assertEqual([r.ident.name for r in rows], ["Pikachu", "Mew", "Pikachu"])
        self.assertEqual((rows[0].start, rows[0].end, rows[0].sightings), (1, 4, 2))
        self.assertEqual(rows[0].ident.confidence, "High")
        self.assertEqual([r.index for r in rows], [1, 2, 3])

    def test_trim_keeps_most_convincing_in_time_order(self) -> None:
        rows = [
            CardRow(1, 0, 0.4, 0, ident("x", "Hand", "Low")),
            CardRow(2, 1, 2, 1.5, ident("a")),
            CardRow(3, 3, 4, 3.5, ident("b")),
        ]
        kept, dropped = trim_to_count(rows, 2)
        self.assertEqual([r.ident.card_id for r in kept], ["a", "b"])
        self.assertEqual([r.index for r in kept], [1, 2])
        self.assertEqual([r.ident.card_id for r in dropped], ["x"])


class OutputTests(unittest.TestCase):
    def _rows(self) -> list[CardRow]:
        rows = [CardRow(1, 1.0, 2.0, 1.5, ident("a", "Pikachu")), CardRow(2, 3.0, 4.0, 3.5, ident("b", "Mew"))]
        rows[0].valuation = Valuation(value=1.25, basis="sold")
        rows[1].valuation = Valuation(value=0.10, basis="bulk", estimated=True)
        return rows

    def test_running_total_and_csv(self) -> None:
        rows = self._rows()
        total = apply_running_totals(rows)
        self.assertEqual(total, 1.35)
        out = csv_rows(rows, total)
        self.assertEqual([r["running_total"] for r in out], ["1.25", "1.35", "1.35"])
        self.assertEqual(out[1]["estimated"], "yes")
        self.assertEqual(out[-1]["card"], "TOTAL")

    def test_srt_cues_run_to_next_card(self) -> None:
        rows = self._rows()
        apply_running_totals(rows)
        text = srt_text(rows, video_end=5.0)
        self.assertIn("00:00:01,000 --> 00:00:03,000", text)
        self.assertIn("00:00:03,000 --> 00:00:05,000", text)  # clamped to video end
        self.assertIn("Total: $1.35", text)


if __name__ == "__main__":
    unittest.main()
