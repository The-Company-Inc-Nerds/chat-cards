"""A stand-in for CardSightClient that burns no API credits.

`chat-cards --mock` swaps this in wherever the real client would go, so the reel,
the counters, the pack lifecycle and the correction flow can all be driven
end-to-end with no key. It is also the rehearsal mode: run it before the stream to
check the overlay against your scene layout.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from .cardsight import Identification, Valuation, summarise_prices
from .names import translate_card_name

# A plausible pull distribution: mostly commons, the occasional chase card.
FAKE_DECK: list[dict[str, Any]] = [
    {"name": "Pikachu", "number": "058", "rarity": "Common", "price": 1.25,
     "attrs": ["pokemon-lightning", "pokemon-legal-standard"]},
    {"name": "Bulbasaur", "number": "001", "rarity": "Common", "price": 0.85,
     "attrs": ["pokemon-grass", "pokemon-legal-standard"]},
    {"name": "Gardevoir ex", "number": "086", "rarity": "Double Rare", "price": 14.40,
     "attrs": ["pokemon-psychic", "pokemon-double-rare", "pokemon-legal-standard"]},
    {"name": "Charizard ex", "number": "223", "rarity": "Special Illustration Rare", "price": 287.50,
     "attrs": ["pokemon-fire", "pokemon-special-illustration-rare", "pokemon-legal-standard"]},
    {"name": "Snorlax", "number": "143", "rarity": "Uncommon", "price": 2.10,
     "attrs": ["pokemon-colorless", "pokemon-legal-standard"]},
    {"name": "Mew ex", "number": "151", "rarity": "Ultra Rare", "price": 62.00,
     "attrs": ["pokemon-psychic", "pokemon-ultra-rare", "pokemon-legal-standard"]},
    {"name": "Squirtle", "number": "007", "rarity": "Common", "price": 0.95,
     "attrs": ["pokemon-water", "pokemon-legal-standard"]},
    {"name": "Iono", "number": "185", "rarity": "Illustration Rare", "price": 23.75,
     "attrs": ["pokemon-trainer", "pokemon-illustration-rare", "pokemon-legal-standard"]},
    {"name": "Rare Candy", "number": "141", "rarity": "Uncommon", "price": 1.60,
     "attrs": ["pokemon-trainer", "pokemon-legal-standard"]},
    {"name": "Miraidon ex", "number": "081", "rarity": "Double Rare", "price": 8.30,
     "attrs": ["pokemon-lightning", "pokemon-double-rare", "pokemon-legal-standard"]},
    # A Japanese print: the catalog name is not Latin script, so the overlay has
    # to fall back to something English and the phone has to prompt for a name.
    {"name": "リザードンex", "number": "104", "rarity": "Art Rare", "price": 41.00,
     "attrs": ["pokemon-fire", "pokemon-art-rare", "pokemon-legal-standard"],
     "language": "ja"},
    # No completed sales at all — drives the estimate ladder.
    {"name": "Cleffa", "number": "080", "rarity": "Common", "price": 0.60,
     "attrs": ["pokemon-psychic", "pokemon-legal-standard"], "comps": "none"},
    # A Japanese common with no comps anywhere — verified against the live API,
    # this is simply what a bulk Japanese card looks like.
    {"name": "ヘラクロス", "number": "006", "rarity": "Common", "price": 1.40,
     "attrs": ["pokemon-grass", "pokemon-legal-standard"],
     "language": "ja", "comps": "none"},
]

SET_NAME = "Scarlet & Violet 151"
RELEASE_NAME = "Scarlet & Violet — 151"


def _svg_card(title: str, subtitle: str, seed: int) -> bytes:
    """Draw a stand-in card so the overlay has real artwork to lay out."""
    rng = random.Random(seed)
    hue = rng.randint(0, 359)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 366 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="hsl({hue} 70% 55%)"/>
      <stop offset="100%" stop-color="hsl({(hue + 60) % 360} 75% 32%)"/>
    </linearGradient>
  </defs>
  <rect width="366" height="512" rx="18" fill="url(#g)"/>
  <rect x="18" y="18" width="330" height="230" rx="10" fill="rgba(255,255,255,.22)"/>
  <text x="183" y="300" font-family="system-ui,sans-serif" font-size="26" font-weight="700"
        fill="#fff" text-anchor="middle">{title[:20]}</text>
  <text x="183" y="332" font-family="system-ui,sans-serif" font-size="16"
        fill="rgba(255,255,255,.85)" text-anchor="middle">{subtitle[:30]}</text>
  <text x="183" y="470" font-family="system-ui,sans-serif" font-size="13"
        fill="rgba(255,255,255,.65)" text-anchor="middle">MOCK CARD</text>
</svg>""".encode("utf-8")


class MockCardSightClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._n = 0
        self._by_id: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @property
    def configured(self) -> bool:
        return True

    async def health_auth(self) -> bool:
        return True

    async def identify(self, image: bytes, content_type: str = "image/jpeg") -> Identification:
        # Real identification takes a beat; without the delay the "identifying"
        # placeholder never renders and the animation goes untested.
        await asyncio.sleep(random.uniform(0.6, 1.6))

        self._n += 1
        entry = FAKE_DECK[self._n % len(FAKE_DECK)]
        card_id = f"mock-{self._n:04d}"
        self._by_id[card_id] = entry

        # One scan in eight fails to match, which is roughly what a hurried photo
        # of a glossy card does in practice — the correction UI needs to be exercised.
        if self._n % 8 == 0:
            return Identification(
                set_name=SET_NAME,
                release_name=RELEASE_NAME,
                year="2023",
                confidence="Low",
                messages=["Mock: glare detected, no exact match"],
            )

        fields = [{"key": "Rarity", "value": entry["rarity"]}]
        if entry.get("language"):
            fields.append({"key": "CARD_LANGUAGE", "value": entry["language"]})

        return Identification(
            card_id=card_id,
            name=entry["name"],
            number=entry["number"],
            year="2023",
            set_name=SET_NAME,
            release_name=RELEASE_NAME,
            manufacturer="The Pokemon Company",
            attributes=list(entry["attrs"]),
            confidence="High" if entry["price"] > 5 else "Medium",
            suggestions=[
                {"id": f"{card_id}-alt", "setName": f"{SET_NAME} (Reverse Holo)"},
            ],
            fields=fields,
        )

    async def pricing(
        self,
        card_id: str,
        parallel_id: str | None,
        *,
        period: str,
        listing_type: str,
        limit: int,
        statistic: str,
        fallbacks: bool = True,
    ) -> Valuation:
        await asyncio.sleep(random.uniform(0.3, 0.9))

        entry = self._by_id.get(card_id, {"price": 3.0})
        centre = float(entry["price"])

        if entry.get("comps") == "none":
            if not fallbacks:
                # basis "none" is what the server turns into the bulk rate, so
                # rehearsal exercises the same path production takes.
                return Valuation(
                    value=0.0, statistic=statistic, period=period, listing_type=listing_type,
                    basis="none", note=f"No sales or listings found for this card in the last {period}",
                )
            # Asks run above what a card actually sells for; the mock reflects that.
            asks = [round(centre * random.uniform(1.3, 2.2), 2) for _ in range(3)]
            value, count, low, high, _ = summarise_prices(
                [{"price": a} for a in asks], statistic
            )
            return Valuation(
                value=value, sample_size=count, statistic=statistic, period=period,
                listing_type="fixed", low=low, high=high, basis="asking", estimated=True,
                note="No completed sales — based on Buy It Now asking prices, which run high",
            )
        rng = random.Random(card_id)
        # Scatter comps around the true price the way eBay actually does.
        records = [
            {
                "price": round(max(0.25, rng.gauss(centre, centre * 0.18)), 2),
                "date": f"2026-0{rng.randint(6, 8)}-{rng.randint(10, 28)}",
                "source": "ebay",
                "listing_type": "auction",
            }
            for _ in range(rng.randint(4, 18))
        ]

        value, count, low, high, last_sale = summarise_prices(records, statistic)
        return Valuation(
            value=value,
            sample_size=count,
            statistic=statistic,
            period=period,
            listing_type=listing_type,
            last_sale_date=last_sale,
            low=low,
            high=high,
            basis="sold",
            note="Mock pricing — not real market data",
        )

    async def card_image(self, card_id: str) -> tuple[bytes, str]:
        entry = self._by_id.get(card_id)
        if entry is None:
            raise KeyError(card_id)
        seed = int(card_id.split("-")[-1]) if card_id.split("-")[-1].isdigit() else 0
        # Draw the translated name: a box on a machine with no CJK font makes a
        # rehearsal look broken when nothing is actually wrong.
        label = translate_card_name(entry["name"]).name or entry["name"]
        return _svg_card(label, entry["rarity"], seed), "image/svg+xml"

    async def search(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        await asyncio.sleep(0.2)
        hits = [e for e in FAKE_DECK if query.lower() in e["name"].lower()] or FAKE_DECK
        # Same shape as a real /v1/catalog/search result of type "card".
        return [
            {
                "type": "card",
                "id": f"mock-search-{i}",
                "name": e["name"],
                "relevance": 1.0 - i * 0.05,
                "year": "2023",
                "setName": SET_NAME,
                "releaseName": RELEASE_NAME,
            }
            for i, e in enumerate(hits[:limit])
        ]
