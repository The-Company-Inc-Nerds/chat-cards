"""Render a transparent "+ $0.10" price overlay for a pack-opening video.

Reads the CSV from `price-video` (or a hand-corrected one; only `timestamp` /
`seconds` and `price` are needed). It writes one transparent video, the same
length as the clip. At each card's timestamp, its price pops up in green, hops
left or right (randomly, per card) and fades out during the hop. Put it on a
track above the clip, lined up at 0:00, and every callout lands on its card.

    chat-cards-overlay opening.prices.csv --like opening.mp4            # -> opening.prices.overlay.mov
    chat-cards-overlay opening.prices.csv --like opening.mp4 -o pops.webm
    chat-cards-overlay opening.prices.csv --size 1080x1920 --fps 30

Formats, by output extension:
  .mov   ProRes 4444 with alpha. Premiere, Resolve, Final Cut and CapCut
         desktop all take it. This is the default.
  .webm  VP9 with alpha. Smaller; for browsers and OBS.

Rendering is pure ffmpeg (one drawtext per card), so it needs nothing beyond the
ffmpeg already used by price-video, plus a bold font (see `--font`).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .video import format_timestamp, read_csv_rows, require_tools

DEFAULT_SIZE = (1080, 1920)  # vertical short
DEFAULT_FPS = 30.0
DEFAULT_COLOR = "0x2BD94A"  # a bright, readable money green
DEFAULT_HOP = 1.2  # seconds a callout is on screen

# Searched in order when --font and $CHAT_CARDS_FONT are not set.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


@dataclass
class Pop:
    """One price callout: when it starts and which way it hops."""

    start: float
    text: str
    direction: int  # -1 left, +1 right
    distance: float  # fraction of frame width travelled sideways
    label: str = ""  # the card's name, for the cue list
    card_time: float = 0.0  # when the card appears in the clip


def price_text(value: float) -> str:
    """0.1 -> '+ $0.10', 1234.5 -> '+ $1,234.50'."""
    return f"+ ${value:,.2f}"


def plan_pops(
    rows: list,
    *,
    seed: int | None = None,
    offset: float = 0.0,
    min_price: float = 0.0,
) -> list[Pop]:
    """One Pop per priced row, with a random hop direction and distance.

    The seed makes a render repeatable: the same CSV and seed always hop the
    same way, so a re-render after a price fix doesn't shuffle every card.
    """
    rng = random.Random(seed)
    pops: list[Pop] = []
    for row in rows:
        value = row.previous_price
        # Draw for every row, priced or not, so one row's edit can't reshuffle
        # the hops of the cards after it.
        direction = rng.choice((-1, 1))
        distance = rng.uniform(0.10, 0.18)
        if value is None or value < min_price:
            continue
        start = max(0.0, row.start + offset)
        pops.append(
            Pop(
                start=start, text=price_text(value), direction=direction, distance=distance,
                label=row.ident.overlay_name if row.ident.name else "", card_time=row.start,
            )
        )
    return pops


def space_pops(pops: list[Pop], *, gap: float, hop: float) -> list[Pop]:
    """Re-time the callouts one after another, `gap` seconds of nothing between.

    For cutting by hand: exactly one number is on screen at a time, each clip
    is easy to find on the timeline, and order follows the CSV. The card's own
    time in the clip is kept in `card_time` for the cue list.
    """
    spaced: list[Pop] = []
    for i, pop in enumerate(pops):
        start = gap + i * (hop + gap)
        spaced.append(
            Pop(start=start, text=pop.text, direction=pop.direction, distance=pop.distance,
                label=pop.label, card_time=pop.card_time)
        )
    return spaced


def write_cues(path: Path, pops: list[Pop], hop: float) -> None:
    """Where each callout sits in the overlay, and where its card is in the clip."""
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["card", "name", "price", "card_in_clip", "overlay_start", "overlay_end"])
        for i, pop in enumerate(pops, 1):
            writer.writerow([
                i, pop.label, pop.text.replace("+ ", ""), format_timestamp(pop.card_time),
                format_timestamp(pop.start), format_timestamp(pop.start + hop),
            ])


def drawtext_filter(
    pop: Pop,
    textfile: Path,
    *,
    font: str,
    width: int,
    height: int,
    hop: float,
    color: str,
    anchor: tuple[float, float],
    font_size: int,
) -> str:
    """The drawtext filter for one callout.

    With p running 0 to 1 over the hop: x slides sideways, y follows an arc
    (up, then a little past the starting line), and opacity holds for the first
    third before fading to nothing as the hop lands.
    """
    t0, t1 = pop.start, pop.start + hop
    p = f"((t-{t0:.3f})/{hop:.3f})"
    dx = pop.direction * pop.distance * width
    rise = 0.09 * height
    drop = 0.03 * height
    x = f"{anchor[0] * width:.1f}-text_w/2+({dx:.1f})*{p}"
    y = f"{anchor[1] * height:.1f}-text_h/2-{rise:.1f}*sin(PI*{p})+{drop:.1f}*{p}"
    alpha = f"if(lt({p},0.35),1,max(0,1-({p}-0.35)/0.65))"
    font_opt = f"fontfile='{_esc(font)}'" if os.path.sep in font or font.lower().endswith((".ttf", ".otf")) else f"font='{_esc(font)}'"
    return (
        f"drawtext={font_opt}:textfile='{_esc(str(textfile))}':expansion=none:"
        f"fontsize={font_size}:fontcolor={color}:"
        f"borderw={max(2, font_size // 14)}:bordercolor=black@0.85:"
        f"shadowx=0:shadowy={max(2, font_size // 20)}:shadowcolor=black@0.5:"
        f"x='{x}':y='{y}':alpha='{alpha}':enable='between(t,{t0:.3f},{t1:.3f})'"
    )


def _esc(text: str) -> str:
    """Escape a value for use inside single quotes in an ffmpeg filtergraph."""
    return text.replace("\\", "/").replace("'", r"'\''").replace(":", r"\:")


def find_font(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("CHAT_CARDS_FONT")
    if env:
        return env
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    return "Sans:style=Bold"  # fontconfig name; works where fontconfig knows a sans font


def probe_video(video: Path) -> tuple[int, int, float, float | None]:
    """(width, height, fps, duration) of a clip, for --like."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate:stream_side_data=rotation:format=duration",
            "-of", "json", str(video),
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    width, height = int(stream["width"]), int(stream["height"])
    # A phone clip stored landscape with a 90-degree rotation flag plays upright.
    rotation = 0
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            rotation = abs(int(side["rotation"])) % 180
    if rotation == 90:
        width, height = height, width
    num, _, den = str(stream.get("r_frame_rate", "30/1")).partition("/")
    fps = float(num) / float(den or 1) if float(num or 0) else DEFAULT_FPS
    duration = data.get("format", {}).get("duration")
    return width, height, fps, float(duration) if duration else None


def encoder_args(out: Path) -> list[str]:
    ext = out.suffix.lower()
    if ext == ".webm":
        return ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "30", "-auto-alt-ref", "0"]
    if ext == ".mov":
        return ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-vendor", "apl0"]
    raise SystemExit(f"chat-cards-overlay: unsupported output {out.name!r} (use .mov or .webm)")


def parse_size(text: str) -> tuple[int, int]:
    w, _, h = text.lower().partition("x")
    try:
        size = int(w), int(h)
    except ValueError:
        raise SystemExit(f"--size wants WIDTHxHEIGHT, e.g. 1080x1920 (got {text!r})") from None
    if size[0] <= 0 or size[1] <= 0:
        raise SystemExit(f"--size must be positive (got {text!r})")
    return size


def parse_anchor(text: str) -> tuple[float, float]:
    try:
        x, y = (float(v) for v in text.split(","))
    except ValueError:
        raise SystemExit(f"--position wants x,y fractions, e.g. 0.5,0.45 (got {text!r})") from None
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise SystemExit(f"--position fractions must be between 0 and 1 (got {text!r})")
    return x, y


def render(
    pops: list[Pop],
    out: Path,
    *,
    width: int,
    height: int,
    fps: float,
    duration: float,
    hop: float,
    color: str,
    anchor: tuple[float, float],
    font: str,
    font_scale: float,
) -> None:
    font_size = max(12, int(round(height * font_scale)))
    # ProRes wants even dimensions.
    width, height = width + width % 2, height + height % 2
    with tempfile.TemporaryDirectory(prefix="chat-cards-overlay-") as tmp:
        tmpdir = Path(tmp)
        filters = ["format=rgba"]  # keep drawtext working in RGBA
        for n, pop in enumerate(pops):
            textfile = tmpdir / f"pop{n:03d}.txt"
            # Text goes through a file: "1,234.50" would otherwise split the filtergraph.
            textfile.write_text(pop.text, encoding="utf-8")
            filters.append(
                drawtext_filter(
                    pop, textfile, font=font, width=width, height=height, hop=hop,
                    color=color, anchor=anchor, font_size=font_size,
                )
            )
        script = tmpdir / "filters.txt"
        script.write_text(",\n".join(filters), encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)

        def cmd(script_opt: list[str]) -> list[str]:
            return [
                "ffmpeg", "-v", "error", "-y",
                # format=rgba must sit on the source itself: left to negotiation,
                # the colour source settles on an opaque format and the alpha is lost.
                "-f", "lavfi", "-i", f"color=c=black@0.0:s={width}x{height}:r={fps:g}:d={duration:.3f},format=rgba",
                *script_opt,
                *encoder_args(out),
                "-an", str(out),
            ]

        # The filtergraph is read from a file (it grows with every card). ffmpeg
        # 7.1+ spells that "-/vf FILE"; 8.0 dropped the older "-filter_script".
        try:
            subprocess.run(cmd(["-/vf", str(script)]), check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode(errors="replace")
            if "Unrecognized option" not in stderr and "Option not found" not in stderr:
                raise
            subprocess.run(cmd(["-filter_script:v", str(script)]), check=True, capture_output=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chat-cards-overlay",
        description="Render a transparent '+ $price' hop-and-fade overlay video from a price-video CSV.",
    )
    p.add_argument("csv", help="CSV from price-video (needs timestamp/seconds and price columns).")
    p.add_argument("-o", "--out", default=None, help="Output .mov (ProRes 4444, default) or .webm (VP9 alpha).")
    p.add_argument("--like", default=None, help="The source clip: copy its size, frame rate and length.")
    p.add_argument("--size", default=None, help="WIDTHxHEIGHT when not using --like (default 1080x1920).")
    p.add_argument("--fps", type=float, default=None, help="Frame rate when not using --like (default 30).")
    p.add_argument("--duration", type=float, default=None, help="Overlay length in seconds (default: the clip's, or last pop + 1s).")
    p.add_argument("--offset", type=float, default=0.0, help="Shift every pop by this many seconds (e.g. -0.2 to lead the reveal).")
    p.add_argument("--hop", type=float, default=DEFAULT_HOP, help="Seconds each callout is on screen (default 1.2).")
    p.add_argument("--position", default="0.5,0.45", help="Where callouts start, as x,y fractions of the frame (default 0.5,0.45).")
    p.add_argument("--font-scale", type=float, default=0.065, help="Text height as a fraction of frame height (default 0.065).")
    p.add_argument("--color", default=DEFAULT_COLOR, help="Text colour, ffmpeg syntax (default 0x2BD94A green).")
    p.add_argument("--font", default=None, help="Font file or fontconfig name (default: $CHAT_CARDS_FONT, else a bold sans).")
    p.add_argument("--seed", type=int, default=None, help="Fix the random hop directions so re-renders match.")
    p.add_argument("--min-price", type=float, default=0.0, help="Skip cards priced below this (default: show all).")
    p.add_argument(
        "--spaced", action="store_true",
        help="Ignore the card timestamps: put the callouts one after another with --gap seconds of "
        "empty video between, so only one number is ever on screen and each is easy to cut out. "
        "Also writes a .cues.csv saying where each one is.",
    )
    p.add_argument("--gap", type=float, default=2.0, help="With --spaced: empty seconds between callouts (default 2).")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    require_tools()
    source = Path(args.csv).expanduser()
    if not source.is_file():
        raise SystemExit(f"chat-cards-overlay: no such file: {source}")
    if args.hop <= 0:
        raise SystemExit("--hop must be positive")

    try:
        rows = read_csv_rows(source)
    except ValueError as exc:
        raise SystemExit(f"chat-cards-overlay: {exc}") from None

    width, height = DEFAULT_SIZE
    fps = DEFAULT_FPS
    clip_length: float | None = None
    if args.like:
        width, height, fps, clip_length = probe_video(Path(args.like).expanduser())
    if args.size:
        width, height = parse_size(args.size)
    if args.fps:
        fps = args.fps

    pops = plan_pops(rows, seed=args.seed, offset=args.offset, min_price=args.min_price)
    if not pops:
        raise SystemExit("chat-cards-overlay: no priced rows in the CSV")
    if args.spaced:
        if args.gap < 0:
            raise SystemExit("--gap can't be negative")
        pops = space_pops(pops, gap=args.gap, hop=args.hop)
        # Its own length, not the clip's: it is a reel of callouts to cut from.
        duration = args.duration or (pops[-1].start + args.hop + args.gap)
    else:
        duration = args.duration or clip_length or (pops[-1].start + args.hop + 1.0)
    late = [pop for pop in pops if pop.start >= duration]
    if late:
        print(f"warning: {len(late)} callout(s) start after the overlay ends ({format_timestamp(duration)})", file=sys.stderr)

    default_name = ".overlay-spaced.mov" if args.spaced else ".overlay.mov"
    out = Path(args.out).expanduser() if args.out else source.with_suffix(default_name)
    font = find_font(args.font)
    print(f"Rendering {len(pops)} callout(s) into {width}x{height} @ {fps:g}fps, {format_timestamp(duration)} long…", file=sys.stderr)
    try:
        render(
            pops, out, width=width, height=height, fps=fps, duration=duration, hop=args.hop,
            color=args.color, anchor=parse_anchor(args.position), font=font, font_scale=args.font_scale,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"ffmpeg failed: {(exc.stderr or b'').decode(errors='replace')[-800:]}") from None
    print(f"  {'overlay':>9}  {'price':<12} {'card in clip':>12}  hop")
    for pop in pops:
        side = "left" if pop.direction < 0 else "right"
        print(f"  {format_timestamp(pop.start):>9}  {pop.text:<12} {format_timestamp(pop.card_time):>12}  {side}  {pop.label}")
    print(f"Overlay: {out}")
    if args.spaced:
        cues = out.with_suffix(".cues.csv")
        write_cues(cues, pops, args.hop)
        print(f"Cues:    {cues}")


if __name__ == "__main__":
    main()
