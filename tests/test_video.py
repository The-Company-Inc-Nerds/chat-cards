"""Tests for the pure parts of the video pricer (no ffmpeg, no network).

    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
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
    drop_isolated,
    find_moments,
    format_timestamp,
    merge_similar_holds,
    motion_profile,
    parse_region,
    parse_timestamp,
    read_csv_rows,
    reprice_rows,
    write_csv,
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


class MomentTests(unittest.TestCase):
    def test_still_runs_become_one_moment_each(self) -> None:
        # 4 fps: motion, 1s still on A, motion, 1s still on B, motion
        frames = [noise(i) for i in range(4)] + [flat(50)] * 4 + [noise(i) for i in range(10, 14)] + [flat(200)] * 4 + [noise(99)]
        moments = find_moments(frames, 4.0, spacing=0.5)
        still = [m for m in moments if m.motion == 0]
        self.assertEqual([m.thumb for m in still], [flat(50), flat(200)])
        self.assertAlmostEqual(still[0].best_time, 5 / 4)

    def test_brief_dip_in_constant_motion_is_found(self) -> None:
        # A card shown mid-swipe: never still, just steadier for one frame.
        frames = [noise(i) for i in range(5)] + [flat(80), bytes([82]) * SIZE] + [noise(i) for i in range(20, 25)]
        moments = find_moments(frames, 10.0, spacing=0.3)
        self.assertIn(5, [round(m.best_time * 10) for m in moments])

    def test_threshold_drops_blurry_dips(self) -> None:
        frames = [noise(i) for i in range(6)]
        self.assertEqual(find_moments(frames, 10.0, threshold=1.0), [])

    def test_motion_profile_takes_worse_side(self) -> None:
        self.assertEqual(motion_profile([flat(0), flat(0), flat(10)]), [0.0, 10.0, 10.0])

    def test_region(self) -> None:
        self.assertIsNone(parse_region("full"))
        self.assertEqual(parse_region("0,0.2,1,0.9"), (0.0, 0.2, 1.0, 0.9))
        with self.assertRaises(ValueError):
            parse_region("0.5,0,0.2,1")

    def test_merge_same_view(self) -> None:
        a = Hold(1.0, 2.0, 1.5, flat(50), motion=5)
        b = Hold(2.3, 3.5, 3.0, flat(52), motion=2)
        c = Hold(4.0, 5.0, 4.5, flat(200))
        merged = merge_similar_holds([a, b, c])
        self.assertEqual(len(merged), 2)
        self.assertEqual((merged[0].start, merged[0].end, merged[0].best_time), (1.0, 3.5, 3.0))

    def test_candidates_steadiest_then_time_order(self) -> None:
        holds = [Hold(0, 0, 0, b"", motion=30), Hold(1, 1, 1, b"", motion=5), Hold(4, 4, 4, b"", motion=9)]
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


    def test_trim_drops_later_repeat_first(self) -> None:
        rows = [
            CardRow(1, 1, 2, 1.5, ident("a", "Pikachu", "Medium")),
            CardRow(2, 3, 3, 3, ident("b", "Mew", "Medium")),
            CardRow(3, 5, 5, 5, ident("a", "Pikachu", "High")),  # the pile, matched again
        ]
        kept, dropped = trim_to_count(rows, 2)
        self.assertEqual([r.start for r in kept], [1, 3])
        self.assertEqual([r.start for r in dropped], [5])


    def test_isolated_low_confidence_dropped(self) -> None:
        rows = [
            CardRow(1, 0.0, 0.0, 0.0, ident("art", "Pack art", "Low")),  # intro shot of the packs
            CardRow(2, 10.0, 10.0, 10.0, ident("a", "A", "Low")),  # real, in a run
            CardRow(3, 10.6, 10.6, 10.6, ident("b", "B")),
            CardRow(4, 11.2, 11.2, 11.2, ident("c", "C")),
            CardRow(5, 20.0, 20.0, 20.0, ident("d", "D", "High")),  # alone but confident
        ]
        kept, lonely = drop_isolated(rows)
        self.assertEqual([r.ident.card_id for r in kept], ["a", "b", "c", "d"])
        self.assertEqual([r.ident.card_id for r in lonely], ["art"])


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


class FakePriceCharting:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices
        self.calls: list[tuple] = []
        self.last_miss: str | None = None

    async def price(self, name, number, release, language=None, parallel_name=None):
        self.calls.append((name, number, release, language))
        if name in self.prices:
            return Valuation(value=self.prices[name], basis="pricecharting")
        self.last_miss = "no single PriceCharting product matches"
        return None


class RepriceTests(unittest.TestCase):
    def _csv(self, body: str) -> pathlib.Path:
        tmp = pathlib.Path(tempfile.mkdtemp()) / "edit.csv"
        tmp.write_text(body, encoding="utf-8-sig")
        return tmp

    def test_edited_csv_round_trip(self) -> None:
        path = self._csv(
            "card,timestamp,seconds,name,number,set,language,price,price_source\n"
            "1,0:52.900,52.9,Pikachu,034/103,30th Celebration,japanese,3.99,PriceCharting\n"
            ",0:51.4,,Morpeko,052/103,30th Celebration,Japanese,,\n"  # added by hand, no seconds
            "3,0:48.5,48.5,Basic Energy,,,japanese,0.05,manual\n"
            "4,0:10,10,Mystery,,,,1.50,sold\n"
            "TOTAL,,,28 cards,,,,9.99,\n"
        )
        rows = read_csv_rows(path)
        self.assertEqual([r.ident.name for r in rows], ["Mystery", "Basic Energy", "Morpeko", "Pikachu"])
        self.assertEqual(rows[2].ident.language, "ja")

        fake = FakePriceCharting({"Pikachu": 4.79, "Morpeko": 1.50})
        asyncio.run(reprice_rows(rows, fake))
        self.assertEqual([r.valuation.value for r in rows], [1.50, 0.05, 1.50, 4.79])
        self.assertTrue(rows[0].valuation.note.startswith("Kept the previous price"))
        self.assertEqual(rows[1].valuation.basis, "manual")
        self.assertNotIn("Basic Energy", [c[0] for c in fake.calls])  # manual is never looked up

        total = apply_running_totals(rows)
        self.assertEqual(total, 7.84)
        out = path.with_name("out.csv")
        write_csv(out, rows, total)
        again = read_csv_rows(out)
        self.assertEqual([r.previous_price for r in again], [1.50, 0.05, 1.50, 4.79])
        self.assertEqual(again[1].previous_source, "manual")


if __name__ == "__main__":
    unittest.main()
