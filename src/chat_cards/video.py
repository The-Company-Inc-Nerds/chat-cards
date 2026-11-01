"""Price a recorded pack opening: video in, per-card timestamps + prices out.

Built for editing shorts. Point it at the clip, say how many cards came out of
the pack, and it writes a CSV with when each card is shown, what it is, what it
is worth and the running total — ready to drop price callouts in at the right
moments. `--srt` also writes a subtitle track an editor (CapCut, Premiere,
Resolve) can import directly.

    chat-cards-video opening.mp4 --cards 10 --api-key-file KEY
    chat-cards-video opening.mp4 --cards 10 --mock          # rehearse, nothing billed
    chat-cards-video opening.mp4 --at 0:03.2,0:05,0:07.5    # you pick the moments

How cards are found. A reveal on camera is a burst of motion (the swipe) followed
by a beat where the card is held still. The video is decoded small and grey at a
few frames a second, and each run of near-still frames becomes a candidate
"hold". Holds that look alike are merged (the same card, hand jitter), the
stillest frame of each is grabbed at full size and sent to CardSight, and
consecutive holds that identify as the same card collapse into one. The pack
wrapper, hands and code cards simply fail to identify and drop out.

Identify is the billed call, so the number of candidates sent is capped
(`--max-identify`, by default a few per expected card) and the longest holds go
first. When the automatic pass gets it wrong, `--at` takes the timestamps from
you and skips detection entirely.

Pricing follows the server exactly: PriceCharting when a token is given, then
the CardSight sold/estimate ladder, then the flat bulk rate.

Needs `ffmpeg` and `ffprobe` on PATH (the dev shell provides both).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .cardsight import CONFIDENCE_RANK, CardSightClient, CardSightError, Identification, Valuation
from .config import (
    DEFAULT_LISTING_TYPE,
    DEFAULT_PERIOD,
    DEFAULT_PRICING_LIMIT,
    PricingConfig,
    _read_secret,
)

log = logging.getLogger("chat_cards.video")

# Motion analysis runs on tiny greyscale frames: enough to see a swipe, cheap
# enough that a ten-minute clip analyses in seconds without numpy.
ANALYSIS_W = 48
ANALYSIS_H = 48
DEFAULT_SAMPLE_FPS = 6.0
DEFAULT_MIN_HOLD = 0.35
# Two holds whose thumbnails differ by less than this (mean abs, 0-255) are the
# same card held across a twitch, not two cards.
SAME_VIEW_DIFF = 10.0
# Frames sent to identify are capped on the long side; the API takes 20MB but a
# phone-sized JPEG identifies just as well and uploads far faster.
IDENTIFY_MAX_SIDE = 1600


# --------------------------------------------------------------------- data


@dataclass
class Hold:
    """A run of near-still frames: a card (or something) held up to the camera."""

    start: float
    end: float
    best_time: float  # the stillest moment, used for the identify frame
    thumb: bytes  # analysis-size greyscale frame at best_time

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Sighting:
    """One identify result at one moment in the video."""

    start: float
    end: float
    frame_time: float
    ident: Identification
    frame_path: Path | None = None

    @property
    def key(self) -> tuple[str, str | None] | None:
        if not self.ident.card_id:
            return None
        return (self.ident.card_id, self.ident.parallel_id)


@dataclass
class CardRow:
    index: int
    start: float
    end: float
    frame_time: float
    ident: Identification
    valuation: Valuation | None = None
    running_total: float = 0.0
    sightings: int = 1
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------ pure helpers


def parse_timestamp(text: str) -> float:
    """'83', '1:23', '1:23.5' or '0:01:23.5' -> seconds."""
    text = text.strip()
    if not text:
        raise ValueError("empty timestamp")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"bad timestamp {text!r}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    if seconds < 0:
        raise ValueError(f"negative timestamp {text!r}")
    return seconds


def format_timestamp(seconds: float) -> str:
    """Editor-friendly M:SS.mmm (H:MM:SS.mmm past an hour)."""
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    if h:
        return f"{h}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{m}:{s:02d}.{ms:03d}"


def srt_timestamp(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def frame_diff(a: bytes, b: bytes) -> float:
    """Mean absolute difference between two equal-size greyscale frames."""
    if not a or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def auto_threshold(diffs: list[float]) -> float:
    """A stillness cut-off that adapts to how shaky the footage is.

    Handheld footage is never truly still, so a fixed number would find no holds
    in one clip and nothing but holds in another. The quiet half of the clip
    sets the bar.
    """
    if not diffs:
        return 2.0
    return max(1.5, statistics.median(diffs) * 1.1)


def find_holds(
    frames: list[bytes],
    fps: float,
    *,
    threshold: float | None = None,
    min_hold: float = DEFAULT_MIN_HOLD,
) -> list[Hold]:
    """Split a frame sequence into still stretches."""
    if len(frames) < 2 or fps <= 0:
        return []
    # diffs[i] is the motion arriving at frame i (frame 0 borrows frame 1's).
    diffs = [frame_diff(frames[i - 1], frames[i]) for i in range(1, len(frames))]
    diffs.insert(0, diffs[0])
    cut = threshold if threshold is not None else auto_threshold(diffs)
    min_frames = max(2, int(round(min_hold * fps)))

    holds: list[Hold] = []
    run_start: int | None = None
    for i, d in enumerate(diffs + [float("inf")]):  # sentinel closes the last run
        if d < cut:
            if run_start is None:
                run_start = i
            continue
        if run_start is not None:
            if i - run_start >= min_frames:
                window = range(run_start, i)
                # Stillest frame, ties broken toward the middle of the hold.
                mid = (run_start + i - 1) / 2
                best = min(window, key=lambda j: (diffs[j], abs(j - mid)))
                holds.append(
                    Hold(start=run_start / fps, end=i / fps, best_time=best / fps, thumb=frames[best])
                )
            run_start = None
    return holds


def merge_similar_holds(holds: list[Hold], *, max_diff: float = SAME_VIEW_DIFF, max_gap: float = 1.0) -> list[Hold]:
    """Fuse neighbouring holds that show the same thing."""
    merged: list[Hold] = []
    for hold in holds:
        prev = merged[-1] if merged else None
        if prev and hold.start - prev.end <= max_gap and frame_diff(prev.thumb, hold.thumb) < max_diff:
            longer = prev if prev.duration >= hold.duration else hold
            merged[-1] = Hold(start=prev.start, end=hold.end, best_time=longer.best_time, thumb=longer.thumb)
        else:
            merged.append(hold)
    return merged


def pick_candidates(holds: list[Hold], budget: int) -> list[Hold]:
    """The `budget` longest holds, back in time order."""
    if budget <= 0:
        return []
    ranked = sorted(holds, key=lambda h: h.duration, reverse=True)[:budget]
    return sorted(ranked, key=lambda h: h.start)


def collapse_sightings(sightings: list[Sighting]) -> list[CardRow]:
    """Turn time-ordered identify results into one row per revealed card.

    Unmatched sightings are dropped (wrapper, hands, energy/code cards). A run of
    sightings of the same card — even with an unmatched blur in between — is one
    reveal. The same card appearing again after a *different* card is kept: two
    copies in one pack is ordinary.
    """
    rows: list[CardRow] = []
    for s in sorted(sightings, key=lambda s: s.start):
        if s.key is None:
            continue
        last = rows[-1] if rows else None
        if last and (last.ident.card_id, last.ident.parallel_id) == s.key:
            last.end = max(last.end, s.end)
            last.sightings += 1
            # Keep the most confident read of the card.
            if CONFIDENCE_RANK.get(s.ident.confidence or "", 0) > CONFIDENCE_RANK.get(last.ident.confidence or "", 0):
                last.ident = s.ident
                last.frame_time = s.frame_time
            continue
        rows.append(CardRow(index=0, start=s.start, end=s.end, frame_time=s.frame_time, ident=s.ident))
    for i, row in enumerate(rows, 1):
        row.index = i
    return rows


def trim_to_count(rows: list[CardRow], count: int | None) -> tuple[list[CardRow], list[CardRow]]:
    """Keep the `count` most convincing reveals, in time order.

    Convincing = identified with higher confidence, then on screen longer, then
    seen in more holds. Returns (kept, dropped).
    """
    if not count or len(rows) <= count:
        return rows, []

    def score(r: CardRow) -> tuple[int, float, int]:
        return (CONFIDENCE_RANK.get(r.ident.confidence or "", 0), r.end - r.start, r.sightings)

    ranked = sorted(rows, key=score, reverse=True)
    kept = sorted(ranked[:count], key=lambda r: r.start)
    dropped = sorted(ranked[count:], key=lambda r: r.start)
    for i, row in enumerate(kept, 1):
        row.index = i
    return kept, dropped


def apply_running_totals(rows: list[CardRow]) -> float:
    total = 0.0
    for row in rows:
        value = row.valuation.value if row.valuation and row.valuation.value is not None else 0.0
        total = round(total + value, 2)
        row.running_total = total
    return total


CSV_FIELDS = [
    "card",
    "timestamp",
    "seconds",
    "end_timestamp",
    "end_seconds",
    "name",
    "number",
    "set",
    "variant",
    "rarity",
    "confidence",
    "price",
    "running_total",
    "price_source",
    "estimated",
    "card_id",
    "note",
]


def csv_rows(rows: list[CardRow], total: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        v = r.valuation
        out.append(
            {
                "card": r.index,
                "timestamp": format_timestamp(r.start),
                "seconds": f"{r.start:.3f}",
                "end_timestamp": format_timestamp(r.end),
                "end_seconds": f"{r.end:.3f}",
                "name": r.ident.overlay_name,
                "number": r.ident.number or "",
                "set": r.ident.release_name or r.ident.set_name or "",
                "variant": r.ident.parallel_name or "",
                "rarity": r.ident.rarity or "",
                "confidence": r.ident.confidence or "",
                "price": f"{v.value:.2f}" if v and v.value is not None else "",
                "running_total": f"{r.running_total:.2f}",
                "price_source": v.basis_label if v else "",
                "estimated": "yes" if v and v.estimated else "no",
                "card_id": r.ident.card_id or "",
                "note": "; ".join([*(r.notes), *([v.note] if v and v.note else [])]),
            }
        )
    out.append({"card": "TOTAL", "name": f"{len(rows)} cards", "price": f"{total:.2f}", "running_total": f"{total:.2f}"})
    return out


def write_csv(path: Path, rows: list[CardRow], total: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens names like "Pokémon" correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(csv_rows(rows, total))


def srt_text(rows: list[CardRow], *, video_end: float | None = None, linger: float = 2.5) -> str:
    """Subtitle cues: each card's price from its reveal until the next card."""
    blocks: list[str] = []
    for i, r in enumerate(rows):
        start = r.start
        nxt = rows[i + 1].start if i + 1 < len(rows) else None
        end = nxt if nxt is not None else r.end + linger
        if video_end is not None:
            end = min(end, video_end)
        end = max(end, start + 0.5)
        price = r.valuation.value if r.valuation and r.valuation.value is not None else 0.0
        blocks.append(
            f"{i + 1}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n"
            f"{r.ident.overlay_name} — ${price:,.2f}\nTotal: ${r.running_total:,.2f}\n"
        )
    return "\n".join(blocks)


# ------------------------------------------------------------- ffmpeg I/O


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise SystemExit(f"chat-cards-video: {', '.join(missing)} not found on PATH (enter the dev shell)")


def probe_duration(video: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
            check=True, capture_output=True, text=True,
        ).stdout
        return float(json.loads(out)["format"]["duration"])
    except (subprocess.CalledProcessError, KeyError, ValueError, TypeError):
        return None


def analysis_frames(video: Path, fps: float) -> list[bytes]:
    """Decode the whole clip to tiny greyscale frames at `fps`."""
    size = ANALYSIS_W * ANALYSIS_H
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(video), "-an",
            "-vf", f"fps={fps},scale={ANALYSIS_W}:{ANALYSIS_H}:flags=area,format=gray",
            "-f", "rawvideo", "-",
        ],
        check=True, capture_output=True,
    )
    data = proc.stdout
    return [data[i : i + size] for i in range(0, len(data) - size + 1, size)]


def grab_frame(video: Path, at: float) -> bytes:
    """One full-quality JPEG at `at` seconds, long side capped."""
    scale = (
        f"scale='if(gt(iw,ih),min({IDENTIFY_MAX_SIDE},iw),-2)':"
        f"'if(gt(iw,ih),-2,min({IDENTIFY_MAX_SIDE},ih))'"
    )
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{at:.3f}", "-i", str(video),
            "-frames:v", "1", "-vf", scale, "-q:v", "2", "-f", "image2", "-c:v", "mjpeg", "-",
        ],
        check=True, capture_output=True,
    )
    if not proc.stdout:
        raise RuntimeError(f"no frame at {format_timestamp(at)}")
    return proc.stdout


# ----------------------------------------------------------------- pricing


async def value_card(
    ident: Identification,
    client: Any,
    pricecharting: Any,
    pricing: PricingConfig,
) -> Valuation:
    """Same order as the live server: PriceCharting, CardSight ladder, bulk rate."""
    if pricecharting is not None and pricecharting.configured and ident.name:
        found = await pricecharting.price(
            ident.name, ident.number, ident.release_name, ident.language, ident.parallel_name
        )
        if found is not None:
            return found

    try:
        valuation = await client.pricing(
            ident.card_id,
            ident.parallel_id,
            period=pricing.period,
            listing_type=pricing.listing_type,
            limit=pricing.limit,
            statistic=pricing.statistic,
            fallbacks=pricing.fallbacks,
        )
    except CardSightError as exc:
        return Valuation(value=0.0, basis="none", note=f"Pricing unavailable: {exc}")

    if valuation.basis == "none" and pricing.bulk_value > 0:
        return Valuation(
            value=round(pricing.bulk_value, 2),
            statistic=valuation.statistic,
            period=valuation.period,
            listing_type=valuation.listing_type,
            basis="bulk",
            estimated=True,
            note="No price found anywhere — showing the bulk rate",
        )
    return valuation


# --------------------------------------------------------------------- run


def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def _identify_at(client: Any, video: Path, at: float, frames_dir: Path | None, label: str) -> tuple[Identification, Path | None]:
    image = await asyncio.to_thread(grab_frame, video, at)
    saved: Path | None = None
    if frames_dir is not None:
        frames_dir.mkdir(parents=True, exist_ok=True)
        saved = frames_dir / f"{label}_{at:08.3f}s.jpg"
        saved.write_bytes(image)
    try:
        ident = await client.identify(image, "image/jpeg")
    except CardSightError as exc:
        if exc.status in (401, 403):
            raise
        ident = Identification(messages=[f"identify failed: {exc}"])
    return ident, saved


async def run(args: argparse.Namespace) -> int:
    require_tools()
    video = Path(args.video).expanduser()
    if not video.is_file():
        _say(f"chat-cards-video: no such file: {video}")
        return 2

    duration = probe_duration(video)
    out_csv = Path(args.out).expanduser() if args.out else video.with_suffix(".prices.csv")
    frames_dir = Path(args.save_frames).expanduser() if args.save_frames else None

    pricing = PricingConfig(
        period=args.pricing_period,
        listing_type=args.pricing_listing_type,
        limit=args.pricing_limit,
        statistic=args.pricing_statistic,
        fallbacks=not args.no_pricing_fallbacks,
        bulk_value=max(0.0, args.bulk_value),
    )
    segment = None if args.segment in (None, "", "auto") else args.segment

    pricecharting = None
    if args.mock:
        from .mock import MockCardSightClient

        client: Any = MockCardSightClient()
    else:
        api_key = _read_secret(args.api_key_file) or args.api_key
        if not api_key:
            _say("chat-cards-video: no CardSight key (use --api-key-file, $CARDSIGHTAI_API_KEY, or --mock)")
            return 2
        client = CardSightClient(api_key, args.api_base, segment, rate_limit=args.rate_limit)
        token = _read_secret(args.pricecharting_token_file)
        if token:
            from .pricecharting import PriceChartingClient

            pricecharting = PriceChartingClient(token)

    await client.start()
    if pricecharting is not None:
        await pricecharting.start()
    try:
        if not args.mock and await client.health_auth() is False:
            _say("chat-cards-video: CardSight rejected the API key")
            return 1

        sightings: list[Sighting] = []
        if args.at:
            times = [parse_timestamp(t) for t in args.at.split(",") if t.strip()]
            _say(f"Identifying {len(times)} card(s) at the given timestamps…")
            for n, t in enumerate(times, 1):
                ident, saved = await _identify_at(client, video, t, frames_dir, f"card{n:02d}")
                _say(f"  {format_timestamp(t)}  {ident.overlay_name if ident.card_id else 'no match'}")
                sightings.append(Sighting(start=t, end=t, frame_time=t, ident=ident, frame_path=saved))
            # One row per timestamp, matched or not: the user said there is a
            # card there, so the CSV must line up with their edit.
            rows = []
            for s in sightings:
                row = CardRow(index=len(rows) + 1, start=s.start, end=s.end, frame_time=s.frame_time, ident=s.ident)
                if not s.ident.card_id:
                    row.notes.append("Not identified — " + "; ".join(s.ident.messages or ["no match"]))
                rows.append(row)
            dropped: list[CardRow] = []
        else:
            _say(f"Analysing motion in {video.name}" + (f" ({format_timestamp(duration)})" if duration else "") + "…")
            frames = await asyncio.to_thread(analysis_frames, video, args.sample_fps)
            holds = find_holds(frames, args.sample_fps, threshold=args.still_threshold, min_hold=args.min_hold)
            holds = merge_similar_holds(holds)
            _say(f"  {len(holds)} still moment(s) found")
            if not holds:
                _say("No still moments found. Try --min-hold 0.2, --still-threshold 4, or give --at timestamps.")
                return 1

            expected = args.cards
            cap = args.max_identify if args.max_identify is not None else (max(expected * 3, expected + 4) if expected else 40)
            first_pass = min(cap, len(holds), (expected * 2 + 2) if expected else cap)
            ordered = sorted(holds, key=lambda h: h.duration, reverse=True)
            tried: set[int] = set()

            async def try_holds(batch: Iterable[Hold]) -> None:
                for hold in batch:
                    tried.add(id(hold))
                    ident, saved = await _identify_at(client, video, hold.best_time, frames_dir, "hold")
                    name = ident.overlay_name if ident.card_id else "—"
                    _say(f"  {format_timestamp(hold.start)}–{format_timestamp(hold.end)}  {name}")
                    sightings.append(
                        Sighting(start=hold.start, end=hold.end, frame_time=hold.best_time, ident=ident, frame_path=saved)
                    )

            _say(f"Identifying {first_pass} candidate frame(s)…")
            await try_holds(pick_candidates(holds, first_pass))
            rows = collapse_sightings(sightings)

            # Short of the expected count: spend the rest of the budget on the
            # holds not yet tried, longest first, until the pack is accounted for.
            while expected and len(rows) < expected and len(tried) < min(cap, len(holds)):
                more = [h for h in ordered if id(h) not in tried][: max(1, expected - len(rows))]
                more = more[: cap - len(tried)]
                if not more:
                    break
                _say(f"Found {len(rows)}/{expected}; checking {len(more)} more…")
                await try_holds(sorted(more, key=lambda h: h.start))
                rows = collapse_sightings(sightings)

            rows, dropped = trim_to_count(rows, expected)

        # Price each distinct card once.
        _say("Pricing…")
        cache: dict[tuple[str, str | None], Valuation] = {}
        for row in rows:
            if not row.ident.card_id:
                row.valuation = Valuation(value=0.0, basis="none")
                continue
            key = (row.ident.card_id, row.ident.parallel_id)
            if key not in cache:
                cache[key] = await value_card(row.ident, client, pricecharting, pricing)
            row.valuation = cache[key]
            if row.ident.confidence == "Low":
                row.notes.append("Low-confidence match — check it")

        total = apply_running_totals(rows)
        write_csv(out_csv, rows, total)
        if args.srt:
            srt_path = Path(args.srt).expanduser()
            srt_path.write_text(srt_text(rows, video_end=duration), encoding="utf-8")

        # ---- report
        print()
        print(f"{'#':>3}  {'time':>9}  {'card':<34} {'price':>9}  {'total':>9}")
        for r in rows:
            v = r.valuation.value if r.valuation and r.valuation.value is not None else 0.0
            flag = "*" if r.valuation and r.valuation.estimated else " "
            print(f"{r.index:>3}  {format_timestamp(r.start):>9}  {r.ident.overlay_name[:34]:<34} ${v:>8,.2f}{flag} ${r.running_total:>8,.2f}")
        print(f"\n     {len(rows)} card(s)  TOTAL ${total:,.2f}   (* = estimate)")
        print(f"     CSV: {out_csv}")
        if args.srt:
            print(f"     SRT: {args.srt}")
        if dropped:
            print(f"     Dropped {len(dropped)} extra match(es) beyond --cards {args.cards}: "
                  + ", ".join(f"{d.ident.overlay_name} @ {format_timestamp(d.start)}" for d in dropped))
        if args.cards and len(rows) < args.cards:
            print(f"     Only found {len(rows)} of {args.cards} cards — check the CSV, or rerun with "
                  "--max-identify higher, --min-hold lower, or explicit --at timestamps.")
        return 0
    finally:
        await client.close()
        if pricecharting is not None:
            await pricecharting.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chat-cards-video",
        description="Find, identify and price every card revealed in a pack-opening video; export CSV.",
    )
    p.add_argument("video", help="The pack-opening clip (anything ffmpeg can read).")
    p.add_argument("-n", "--cards", type=int, default=None, help="How many cards were opened. Strongly recommended.")
    p.add_argument("-o", "--out", default=None, help="CSV path (default: <video>.prices.csv next to the video).")
    p.add_argument("--srt", default=None, help="Also write an SRT subtitle track of the price callouts.")
    p.add_argument(
        "--at", default=None,
        help="Comma-separated timestamps to identify instead of auto-detecting, e.g. 0:03.2,0:05,7.5",
    )
    p.add_argument("--save-frames", default=None, help="Directory to save every frame sent to identify, for checking.")
    p.add_argument("--mock", action="store_true", help="Canned identify/pricing. No key, nothing billed.")

    g = p.add_argument_group("detection")
    g.add_argument("--sample-fps", type=float, default=DEFAULT_SAMPLE_FPS, help="Analysis frame rate (default 6).")
    g.add_argument("--min-hold", type=float, default=DEFAULT_MIN_HOLD, help="Seconds a card must be held still (default 0.35).")
    g.add_argument("--still-threshold", type=float, default=None, help="Motion cut-off 0-255 (default: adaptive).")
    g.add_argument(
        "--max-identify", type=int, default=None,
        help="Hard cap on billed identify calls (default: 3x --cards, or 40).",
    )

    k = p.add_argument_group("API")
    k.add_argument("--api-key-file", default=os.environ.get("CHAT_CARDS_API_KEY_FILE"))
    k.add_argument("--api-key", default=os.environ.get("CARDSIGHTAI_API_KEY") or os.environ.get("CHAT_CARDS_API_KEY"))
    k.add_argument("--api-base", default=os.environ.get("CHAT_CARDS_API_BASE", "https://api.cardsight.ai"))
    k.add_argument("--segment", default=os.environ.get("CHAT_CARDS_SEGMENT", "pokemon"), help='"auto" to let the API detect.')
    k.add_argument("--rate-limit", type=float, default=3.0)
    k.add_argument("--pricecharting-token-file", default=os.environ.get("CHAT_CARDS_PRICECHARTING_TOKEN_FILE"))

    q = p.add_argument_group("pricing")
    q.add_argument("--pricing-period", default=DEFAULT_PERIOD)
    q.add_argument("--pricing-listing-type", choices=["auction", "fixed", "both"], default=DEFAULT_LISTING_TYPE)
    q.add_argument("--pricing-limit", type=int, default=DEFAULT_PRICING_LIMIT)
    q.add_argument("--pricing-statistic", choices=["median", "mean", "latest"], default="median")
    q.add_argument("--bulk-value", type=float, default=0.10)
    q.add_argument("--no-pricing-fallbacks", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.cards is not None and args.cards <= 0:
        raise SystemExit("--cards must be positive")
    try:
        code = asyncio.run(run(args))
    except subprocess.CalledProcessError as exc:
        _say(f"ffmpeg failed: {(exc.stderr or b'').decode(errors='replace')[-500:]}")
        code = 1
    except CardSightError as exc:
        _say(f"CardSight error: {exc}")
        code = 1
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
