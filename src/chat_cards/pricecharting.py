"""PriceCharting price lookups.

CardSight identifies the card and supplies the artwork; PriceCharting supplies
the price. It is the only source verified to carry the Japanese prints that
CardSight has no comps for at all — Charcadet #14 and Heracross #6 from Japanese
Clay Burst both resolve here.

API notes, from https://www.pricecharting.com/api-documentation:

  * auth is a `t` query parameter holding a 40-character subscription token
  * prices are integers in pennies: 1732 means $17.32
  * `loose-price` is the ungraded price for a card; the other price keys are all
    graded tiers, which is a different card economically
  * **one call per second**, enforced, with account suspension threatened for
    persistent abuse — so the limiter here is deliberately strict
  * only current values; no historic sales
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

from .cardsight import Valuation

log = logging.getLogger(__name__)

BASE_URL = "https://www.pricecharting.com/api"
TIMEOUT = aiohttp.ClientTimeout(total=10)

# Their documented ceiling is 1 req/s. Going under it is free insurance against
# an account suspension mid-stream.
DEFAULT_RATE_LIMIT = 0.9

# PriceCharting distinguishes variants with a bracketed suffix on the product
# name: "Growlithe #55" and "Growlithe [Reverse Holo] #55" are separate products
# priced very differently. A best-match search returns whichever it likes, so the
# variant has to be chosen deliberately.
BRACKET = re.compile(r"\[([^\]]+)\]")


def _wanted_variant(parallel_name: str | None) -> str | None:
    """The bracket text to look for, or None for a plain base card."""
    if not parallel_name:
        return None
    lowered = parallel_name.lower()
    if "reverse" in lowered:
        return "reverse holo"
    if "1st" in lowered or "first" in lowered:
        return "1st edition"
    return lowered


# PriceCharting spells the print language out in the set ("console") name:
# "Pokemon Japanese Clay Burst", "Pokemon Korean Cyber Judge". English sets carry
# no language word at all.
LANGUAGE_WORDS = {"ja": "japanese", "ko": "korean", "zh": "chinese"}
_LANGUAGE_ALIASES = {
    "japanese": "ja", "jp": "ja", "jpn": "ja",
    "korean": "ko", "kr": "ko", "kor": "ko",
    "chinese": "zh", "cn": "zh", "chi": "zh",
    "english": "en", "eng": "en",
}


def language_code(language: str | None) -> str | None:
    """'Japanese', 'jp', 'ja', 'ja-JP' -> 'ja'. Unknown or blank -> None."""
    if not language:
        return None
    text = language.strip().lower()
    if not text:
        return None
    if text in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[text]
    return text.split("-")[0][:2] or None


def language_word(language: str | None) -> str | None:
    """The word PriceCharting puts in the set name, or None for English."""
    return LANGUAGE_WORDS.get(language_code(language) or "")


def normalise_number(number: str | None) -> str | None:
    """'052/185' -> '52', '007' -> '7', 'SM230' unchanged.

    PriceCharting names products "Morpeko #52": no printed set total, no
    leading zeros. Cards print "052/185".
    """
    if not number:
        return None
    text = number.strip().lstrip("#").split("/")[0].strip()
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text or None


def build_query(name: str, number: str | None, release: str | None, language: str | None) -> str:
    """Compose the full-text query.

    PriceCharting names its Japanese sets "Pokemon Japanese Clay Burst", so the
    language has to be in the query or a Japanese card matches the English set
    of the same name. Korean and Chinese work the same way.
    """
    parts = [name]
    number = normalise_number(number)
    if number:
        parts.append(f"#{number}")
    parts.append("pokemon")
    word = language_word(language)
    if word:
        parts.append(word)
    if release:
        parts.append(release)
    return " ".join(parts)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def choose_product(
    products: list[dict[str, Any]],
    number: str | None,
    parallel_name: str | None,
    release: str | None = None,
    language: str | None = None,
) -> dict[str, Any] | None:
    """Pick the right product, or nothing at all.

    Refuses to guess. A wrong pick here is not a small error: the reverse-holo
    Growlithe is twelve times the price of the plain one. When the number and
    variant still leave several products (the same card number in the English,
    Japanese and Korean printings of a set), the language and set name settle it.
    """
    if not products:
        return None

    candidates = products
    number = normalise_number(number)
    if number:
        # "#55" must match exactly; "#5" must not satisfy a search for "#55".
        pattern = re.compile(rf"#{re.escape(number)}(?![0-9A-Za-z])")
        exact = [p for p in candidates if pattern.search(str(p.get("product-name", "")))]
        if exact:
            candidates = exact

    wanted = _wanted_variant(parallel_name)
    if wanted:
        matching = [
            p for p in candidates
            if any(wanted in m.lower() for m in BRACKET.findall(str(p.get("product-name", ""))))
        ]
    else:
        # A base card is the one with no bracketed variant in its name.
        matching = [p for p in candidates if not BRACKET.search(str(p.get("product-name", "")))]

    word = language_word(language)
    if word:
        # A Korean or Japanese print is a different card economically. When the
        # language is known, an English product is never an acceptable answer.
        matching = [p for p in matching if word in _words(str(p.get("console-name", "")))]

    if len(matching) > 1:
        matching = _narrow_by_set(matching, release, language)

    if len(matching) == 1:
        return matching[0]
    return None


def _narrow_by_set(
    products: list[dict[str, Any]], release: str | None, language: str | None
) -> list[dict[str, Any]]:
    """Tie-break on the set ("console") name; never widens the list."""
    word = language_word(language)
    all_words = set(LANGUAGE_WORDS.values())

    def console(p: dict[str, Any]) -> set[str]:
        return _words(str(p.get("console-name", "")))

    if word:
        by_language = [p for p in products if word in console(p)]
    else:
        # English: the set name carries no language word.
        by_language = [p for p in products if not (console(p) & all_words)]
    if by_language:
        products = by_language

    if release and len(products) > 1:
        wanted = _words(release) - {"pokemon", "the", "and"}
        if wanted:
            by_set = [p for p in products if wanted <= console(p)]
            if by_set:
                products = by_set
    return products


def price_from(product: dict[str, Any], key: str = "loose-price") -> float | None:
    pennies = product.get(key)
    if isinstance(pennies, int) and pennies > 0:
        return round(pennies / 100.0, 2)
    return None


class PriceChartingClient:
    def __init__(self, token: str | None, rate_limit: float = DEFAULT_RATE_LIMIT, retries: int = 0) -> None:
        self._token = token
        # Retries on 429 / timeouts. Off for the live server, where a late price
        # is worse than falling through to CardSight; on for batch repricing,
        # where PriceCharting throttles a long run of lookups.
        self._retries = max(0, retries)
        # Why the last price() returned None, for callers that report it.
        self.last_miss: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._min_interval = 1.0 / rate_limit if rate_limit > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._token)

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._next_at - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = loop.time() + self._min_interval

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any] | None:
        if not self._token or self._session is None or self._session.closed:
            return None
        payload: dict[str, Any] | None = None
        for attempt in range(self._retries + 1):
            await self._throttle()
            try:
                async with self._session.get(
                    f"{BASE_URL}{path}", params={**params, "t": self._token}
                ) as response:
                    if response.status == 429 and attempt < self._retries:
                        log.info("PriceCharting %s rate limited — retrying", path)
                        await asyncio.sleep(3.0 * (attempt + 1))
                        continue
                    if response.status >= 400:
                        log.info("PriceCharting %s returned %s", path, response.status)
                        return None
                    payload = await response.json(content_type=None)
                    break
            except Exception as exc:  # a slow third party must never fail a scan
                if isinstance(exc, asyncio.TimeoutError) and attempt < self._retries:
                    log.info("PriceCharting %s timed out — retrying", path)
                    continue
                log.info("PriceCharting %s failed: %r", path, exc)
                return None
        if payload is None:
            return None

        if payload.get("status") != "success":
            log.info("PriceCharting %s: %s", path, payload.get("error-message"))
            return None
        return payload

    async def health(self) -> bool:
        """True when the token is accepted and returns price data.

        Identity comes back even for a token with no price entitlement — the
        published demo token does exactly that — so a usable token is one whose
        response actually carries `loose-price`.
        """
        payload = await self._get("/product", {"q": "charizard #4 pokemon base set"})
        return bool(payload and "loose-price" in payload)

    async def price(
        self,
        name: str,
        number: str | None,
        release: str | None,
        language: str | None = None,
        parallel_name: str | None = None,
    ) -> Valuation | None:
        self.last_miss = None
        query = build_query(name, number, release, language)
        payload = await self._get("/products", {"q": query})
        if payload is None:
            self.last_miss = "PriceCharting did not answer"
            return None

        product = choose_product(payload.get("products") or [], number, parallel_name, release, language)
        if product is None:
            log.info("PriceCharting: no unambiguous match for %r", query)
            self.last_miss = "no single PriceCharting product matches"
            return None

        # The list endpoint returns identity only; prices need the product call.
        detail = await self._get("/product", {"id": str(product.get("id"))})
        if detail is None:
            self.last_miss = "PriceCharting did not answer"
            return None

        value = price_from(detail, "loose-price")
        if value is None:
            log.info("PriceCharting: no ungraded price for %r", detail.get("product-name"))
            self.last_miss = (
                f"PriceCharting lists {detail.get('product-name')} ({detail.get('console-name')}) "
                "but has no ungraded price for it"
            )
            return None

        return Valuation(
            value=value,
            sample_size=1,
            statistic="pricecharting-ungraded",
            period="current",
            listing_type="both",
            low=value,
            high=value,
            basis="pricecharting",
            estimated=False,
            note=f"PriceCharting ungraded — {detail.get('product-name')} ({detail.get('console-name')})",
        )
