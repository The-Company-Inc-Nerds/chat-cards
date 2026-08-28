#!/usr/bin/env python3
"""Preflight the CardSight API with a real key.

Walks the whole chain the app depends on — key, search, catalog art, identify,
pricing — and prints the sale records behind the computed price so the number on
the overlay can be sanity-checked before anyone is watching.

Only the identify stage is billed, and `--skip-identify` omits it.

    python3 tools/live_check.py --api-key-file /run/secrets/cardsight-api-key
    python3 tools/live_check.py --api-key-file KEY --image ~/card.jpg
    python3 tools/live_check.py --api-key-file KEY --skip-identify
    python3 tools/live_check.py --api-key-file KEY --compare --query "iono"
    python3 tools/live_check.py --api-key-file KEY --diagnose --card-id UUID \
        --title "Charcadet Clay Burst 14"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from chat_cards.cardsight import CardSightClient, CardSightError  # noqa: E402
from chat_cards.config import DEFAULT_PERIOD, DEFAULT_LISTING_TYPE  # noqa: E402

OK, BAD, INFO = "  ok  ", " FAIL ", "      "


def line(mark: str, text: str) -> None:
    print(f"[{mark}] {text}")


async def run(args: argparse.Namespace) -> int:
    key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
    if not key:
        line(BAD, f"{args.api_key_file} is empty")
        return 1

    segment = None if args.segment in ("", "auto") else args.segment
    client = CardSightClient(key, args.api_base, segment)
    await client.start()
    failures = 0

    try:
        # ---------------------------------------------------------------- key
        auth = await client.health_auth()
        if auth is True:
            line(OK, "API key accepted")
        elif auth is False:
            line(BAD, "API key REJECTED — nothing else will work")
            return 1
        else:
            line(INFO, "could not verify the key (endpoint unreachable) — continuing")

        # ------------------------------------------------------------- search
        card_id = args.card_id
        if not card_id:
            results = await client.search(args.query, limit=5)
            if not results:
                line(BAD, f"search for {args.query!r} returned nothing")
                return 1
            line(OK, f"search returned {len(results)} cards for {args.query!r}")
            for r in results[:3]:
                bits = " ".join(str(b) for b in (r.get("year"), r.get("releaseName")) if b)
                line(INFO, f"    {r.get('name')}  {bits}")
            card_id = results[0]["id"]
        line(INFO, f"    using card {card_id}")

        # -------------------------------------------------------------- image
        image, ctype = None, "image/jpeg"
        if args.image:
            image = Path(args.image).read_bytes()
            ctype = "image/png" if args.image.lower().endswith(".png") else "image/jpeg"
            line(OK, f"read {args.image} ({len(image)} bytes)")
        else:
            try:
                image, ctype = await client.card_image(card_id)
                line(OK, f"catalog art fetched ({len(image)} bytes, {ctype})")
            except CardSightError as exc:
                line(BAD, f"no catalog art for this card: {exc}")
                failures += 1

        # ----------------------------------------------------------- identify
        if args.skip_identify:
            line(INFO, "identify skipped (--skip-identify)")
        elif image:
            ident = await client.identify(image, ctype)
            if ident.exact:
                line(OK, f"identified: {ident.display_name} — {ident.subtitle}")
                line(INFO, f"    confidence {ident.confidence}, rarity {ident.rarity!r}")
                if not args.image and ident.card_id != card_id:
                    # Feeding a card its own art should return that same card.
                    line(INFO, f"    note: matched a different print ({ident.card_id})")
                card_id = ident.card_id
            else:
                line(BAD, f"no exact match — {ident.display_name}")
                failures += 1
            for message in ident.messages:
                line(INFO, f"    API says: {message}")

        # ------------------------------------------------------------ pricing
        valuation = await client.pricing(
            card_id,
            None,
            period=args.period,
            listing_type=args.listing_type,
            limit=args.limit,
            statistic=args.statistic,
        )
        if valuation.value is None:
            line(INFO, f"no price: {valuation.note}")
        else:
            line(OK, f"price ${valuation.value:.2f}  ({args.statistic} of {valuation.sample_size} sales)")
            line(INFO, f"    range ${valuation.low:.2f}–${valuation.high:.2f}, last sale {valuation.last_sale_date}")
            if valuation.note:
                line(INFO, f"    {valuation.note}")

        print()
        if failures:
            print(f"{failures} stage(s) failed.")
        else:
            print("All stages passed. The key and the whole chain are good.")
        return 1 if failures else 0

    except CardSightError as exc:
        print()
        line(BAD, f"API error: {exc}")
        return 1
    finally:
        await client.close()


async def diagnose(args: argparse.Namespace) -> int:
    """Probe every pricing rung for specific cards and report which have data.

    Answers the only question worth asking about a card showing $0.00: is it the
    window, the auction-only filter, or nothing being linked to that catalog
    entry at all?
    """
    key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
    segment = None if args.segment in ("", "auto") else args.segment
    client = CardSightClient(key, args.api_base, segment)
    await client.start()

    card_ids = list(args.card_id or [])
    if not card_ids:
        line(BAD, "--diagnose needs at least one --card-id")
        return 1

    try:
        for card_id in card_ids:
            print(f"\ncard {card_id}")
            base = {"grade_id": "null", "limit": str(args.limit)}
            probes: list[tuple[str, str]] = []

            for label, params in [
                (f"sold, base card, {args.period}",
                 dict(base, parallel_id="null", period=args.period, listing_type="auction")),
                (f"sold, any variant, {args.period}",
                 dict(base, period=args.period, listing_type="auction")),
                ("sold, any variant, all time",
                 dict(base, period="all", listing_type="auction")),
                ("asks (Buy It Now), all time",
                 dict(base, period="all", listing_type="fixed")),
            ]:
                try:
                    records = await client._sale_records(card_id, params)
                    probes.append((label, f"{len(records)} rows"))
                except CardSightError as exc:
                    probes.append((label, f"error: {exc}"))

            try:
                probes.append(("active listings", f"{len(await client._listed_records(card_id, args.limit))} rows"))
            except CardSightError as exc:
                probes.append(("active listings", f"error: {exc}"))

            if args.title:
                try:
                    rows = await client._title_records(args.title, "all", args.limit)
                    sales = [r for r in rows if r.get("listing_type") == "auction"]
                    probes.append((f"title search {args.title!r}",
                                   f"{len(rows)} rows ({len(sales)} completed sales)"))
                except CardSightError as exc:
                    probes.append((f"title search {args.title!r}", f"error: {exc}"))

            for label, result in probes:
                mark = OK if result[0].isdigit() and not result.startswith("0 ") else INFO
                print(f"  [{mark}] {label:38s} {result}")

        print("\nA card with rows only under 'title search' is not linked to its")
        print("catalog entry — widening the window will not help it.")
        return 0
    except CardSightError as exc:
        line(BAD, f"API error: {exc}")
        return 1
    finally:
        await client.close()


async def compare(args: argparse.Namespace) -> int:
    """Price the same cards across several windows, to settle how wide to go.

    Coverage — how many cards get a real comp rather than an estimate — is the
    number that matters when a pack keeps coming up unpriced.
    """
    key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
    segment = None if args.segment in ("", "auto") else args.segment
    client = CardSightClient(key, args.api_base, segment)
    await client.start()

    try:
        card_ids = list(args.card_id or [])
        if not card_ids:
            results = await client.search(args.query, limit=args.count)
            card_ids = [r["id"] for r in results][: args.count]
            names = {r["id"]: r.get("name", "?") for r in results}
        else:
            names = {cid: cid[:8] for cid in card_ids}

        if not card_ids:
            line(BAD, f"no cards found for {args.query!r}")
            return 1

        periods = [p.strip() for p in args.periods.split(",") if p.strip()]
        print(f"{len(card_ids)} cards x {len(periods)} windows = "
              f"{len(card_ids) * len(periods)} billed pricing calls\n")

        header = f"{'card':26s}" + "".join(f"{p:>16s}" for p in periods)
        print(header)
        print("-" * len(header))

        # Coverage per window: a real comp, an estimate, or nothing at all.
        covered = {p: 0 for p in periods}
        estimated = {p: 0 for p in periods}

        for card_id in card_ids:
            cells = []
            for period in periods:
                valuation = await client.pricing(
                    card_id, None, period=period, listing_type=args.listing_type,
                    limit=args.limit, statistic=args.statistic, fallbacks=False,
                )
                if valuation.basis == "none":
                    cells.append("—")
                else:
                    covered[period] += 1
                    if valuation.estimated:
                        estimated[period] += 1
                    cells.append(f"${valuation.value:,.2f}/{valuation.sample_size}")
            print(f"{names.get(card_id, card_id)[:25]:26s}" + "".join(f"{c:>16s}" for c in cells))

        print()
        print(f"{'priced':26s}" + "".join(f"{f'{covered[p]}/{len(card_ids)}':>16s}" for p in periods))
        print()
        print("Cells show price/sample-size; — means no comps in that window.")
        print("Run with --listing-type both to see how much Buy It Now asks add.")
        return 0
    except CardSightError as exc:
        line(BAD, f"API error: {exc}")
        return 1
    finally:
        await client.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api-key-file", required=True, help="file containing the CardSight API key")
    p.add_argument("--api-base", default="https://api.cardsight.ai")
    p.add_argument("--segment", default="pokemon", help='segment to pin, or "auto"')
    p.add_argument("--query", default="charizard ex", help="catalog search to pick a test card")
    p.add_argument("--card-id", action="append", default=None,
                   help="skip search and use this card UUID; repeat for --compare")
    p.add_argument("--compare", action="store_true",
                   help="price the same cards across several windows and report coverage")
    p.add_argument("--periods", default="90d,1y,all",
                   help="comma-separated windows for --compare (default: 90d,1y,all)")
    p.add_argument("--count", type=int, default=8,
                   help="how many cards to sample for --compare (default: 8)")
    p.add_argument("--diagnose", action="store_true",
                   help="probe every pricing rung for --card-id cards and report which have data")
    p.add_argument("--title", default=None,
                   help='free-text title query to probe with --diagnose, e.g. "Charcadet Clay Burst 14"')
    p.add_argument("--image", default=None,
                   help="photograph to identify; without it, the card's own catalog art is used")
    p.add_argument("--skip-identify", action="store_true", help="do not spend a billed identify call")
    p.add_argument("--period", default=DEFAULT_PERIOD)
    p.add_argument("--listing-type", default=DEFAULT_LISTING_TYPE, choices=["auction", "fixed", "both"])
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--statistic", default="median", choices=["median", "mean", "latest"])
    args = p.parse_args()
    # A single --card-id is a plain string everywhere except compare mode.
    if not (args.compare or args.diagnose) and args.card_id:
        args.card_id = args.card_id[-1]
    if args.diagnose:
        raise SystemExit(asyncio.run(diagnose(args)))
    raise SystemExit(asyncio.run(compare(args) if args.compare else run(args)))


if __name__ == "__main__":
    main()
