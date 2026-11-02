"""Tests for the price overlay planner and filter builder (no ffmpeg needed).

    PYTHONPATH=src python -m unittest discover -s tests
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from chat_cards.cardsight import Identification  # noqa: E402
from chat_cards.overlay import Pop, drawtext_filter, parse_anchor, parse_size, plan_pops, price_text, space_pops  # noqa: E402
from chat_cards.video import CardRow  # noqa: E402


def row(start: float, price: float | None) -> CardRow:
    return CardRow(0, start, start, start, Identification(name="Card"), previous_price=price)


class PlanTests(unittest.TestCase):
    def test_price_text(self) -> None:
        self.assertEqual(price_text(0.1), "+ $0.10")
        self.assertEqual(price_text(1234.5), "+ $1,234.50")

    def test_skips_unpriced_and_applies_offset(self) -> None:
        pops = plan_pops([row(1.0, 0.1), row(2.0, None), row(3.0, 0.02)], seed=1, offset=-0.2, min_price=0.05)
        self.assertEqual([(p.start, p.text) for p in pops], [(0.8, "+ $0.10")])

    def test_seed_is_repeatable_and_edits_do_not_reshuffle(self) -> None:
        rows = [row(float(i), 1.0) for i in range(10)]
        a = [p.direction for p in plan_pops(rows, seed=7)]
        self.assertEqual(a, [p.direction for p in plan_pops(rows, seed=7)])
        self.assertEqual(set(a), {-1, 1})  # both ways get used
        # Un-pricing one card keeps every other card's hop the same.
        rows[3].previous_price = None
        b = [p.direction for p in plan_pops(rows, seed=7)]
        self.assertEqual(b, a[:3] + a[4:])

    def test_never_starts_before_zero(self) -> None:
        self.assertEqual(plan_pops([row(0.1, 1.0)], offset=-1.0)[0].start, 0.0)


class SpacedTests(unittest.TestCase):
    def test_one_at_a_time_with_gaps(self) -> None:
        # Cards half a second apart in the clip: the timed overlay would overlap.
        pops = plan_pops([row(13.5, 1.26), row(14.0, 0.40), row(14.5, 0.87)], seed=3)
        spaced = space_pops(pops, gap=2.0, hop=1.2)
        self.assertEqual([p.start for p in spaced], [2.0, 5.2, 8.4])
        for a, b in zip(spaced, spaced[1:]):
            self.assertGreaterEqual(b.start - (a.start + 1.2), 2.0)  # never two on screen
        self.assertEqual([p.card_time for p in spaced], [13.5, 14.0, 14.5])
        self.assertEqual([p.direction for p in spaced], [p.direction for p in pops])
        self.assertEqual(spaced[0].label, "Card")


class FilterTests(unittest.TestCase):
    def test_filter_times_and_motion(self) -> None:
        f = drawtext_filter(
            Pop(start=12.5, text="+ $1.00", direction=-1, distance=0.15),
            pathlib.Path("/tmp/pop000.txt"),
            font="/fonts/DejaVu Sans-Bold.ttf", width=1080, height=1920, hop=1.2,
            color="0x2BD94A", anchor=(0.5, 0.45), font_size=125,
        )
        self.assertIn("enable='between(t,12.500,13.700)'", f)
        self.assertIn("(-162.0)*", f)  # hops left: 15% of 1080
        self.assertIn("fontfile='/fonts/DejaVu Sans-Bold.ttf'", f)
        self.assertIn("textfile='/tmp/pop000.txt'", f)
        self.assertIn("alpha='if(lt(", f)

    def test_fontconfig_name(self) -> None:
        f = drawtext_filter(Pop(0, "+ $1.00", 1, 0.1), pathlib.Path("/t.txt"), font="Sans:style=Bold",
                            width=100, height=100, hop=1, color="green", anchor=(0.5, 0.5), font_size=10)
        self.assertIn(r"font='Sans\:style=Bold'", f)

    def test_parsers(self) -> None:
        self.assertEqual(parse_size("1080x1920"), (1080, 1920))
        self.assertEqual(parse_anchor("0.5,0.45"), (0.5, 0.45))
        with self.assertRaises(SystemExit):
            parse_anchor("2,0")


if __name__ == "__main__":
    unittest.main()
