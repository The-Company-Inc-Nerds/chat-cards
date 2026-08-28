"""Async client for the CardSight AI REST API.

Only the handful of endpoints this app needs are wrapped:

    POST /v1/identify/card[/{segment}]   multipart "image" -> detections
    GET  /v1/pricing/{card_id}           raw completed-sale records
    GET  /v1/images/cards/{id}           catalog art (needs the key, so we proxy it)
    GET  /v1/catalog/search              fuzzy search, for manual correction
    GET  /v1/health/auth                 key validation at startup
"""

from __future__ import annotations

import asyncio
import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .names import has_non_latin, translate_card_name

log = logging.getLogger(__name__)

# The API accepts up to 20MB, but the phone downscales long before this. The cap
# exists so a malformed upload cannot pin memory.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

CONFIDENCE_RANK = {"High": 3, "Medium": 2, "Low": 1}


# The API allows 4 requests/second and answers a burst with HTTP 429. The pricing
# ladder alone can fire five calls for one card, and a producer scanning quickly
# has several cards enriching at once — so the ceiling is easy to hit exactly
# when it hurts most. Default a little under the limit for headroom.
DEFAULT_RATE_LIMIT = 3.0
RATE_LIMIT_RETRIES = 3


class RateLimiter:
    """Spaces outbound requests so a burst cannot trip the server's limit."""

    def __init__(self, per_second: float) -> None:
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = loop.time()
            self._next_at = now + self._min_interval


def retry_after_seconds(body: str) -> float:
    """Seconds the server asked us to wait, from its 429 message."""
    match = re.search(r"try again in (\d+)", body, re.IGNORECASE)
    return float(match.group(1)) if match else 1.0


class CardSightError(RuntimeError):
    """An API call failed. Carries the HTTP status so callers can tell 401 from 503."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Identification:
    """One card's identity, flattened out of a `detections[]` entry."""

    card_id: str | None = None
    name: str | None = None
    number: str | None = None
    year: str | None = None
    set_name: str | None = None
    release_name: str | None = None
    manufacturer: str | None = None
    parallel_id: str | None = None
    parallel_name: str | None = None
    numbered_to: int | None = None
    attributes: list[str] = field(default_factory=list)
    confidence: str | None = None
    # Alternate matches the API offers when reprints score similarly. These drive
    # the one-tap correction list on the phone.
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    fields: list[dict[str, Any]] = field(default_factory=list)

    @property
    def exact(self) -> bool:
        """True when the API matched an actual catalog card, not just a set."""
        return bool(self.card_id)

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.set_name or self.release_name:
            return f"Unmatched card — {self.release_name or self.set_name}"
        return "Unidentified card"

    @property
    def language(self) -> str | None:
        """ISO 639-1 code of the scanned card, when the API reports one."""
        return field_value(self.fields, "CARD_LANGUAGE")

    @property
    def translation(self):
        return translate_card_name(self.name)

    @property
    def name_is_english(self) -> bool:
        """False when an operator should still check the name before it airs."""
        return self.translation.complete and not has_non_latin(self.overlay_name)

    @property
    def overlay_name(self) -> str:
        """A name that is safe to put on an English-language stream.

        Translated from the official localised species names where possible, and
        never CJK or Hangul: a name that cannot be resolved at all falls back to
        the set and number, with the producer prompted to type one in.
        """
        if not self.name:
            return self.display_name

        translated = self.translation.name
        if translated and not has_non_latin(translated):
            return translated

        release = self.release_name or self.set_name
        if release and not has_non_latin(release):
            return f"{release} #{self.number}" if self.number else release
        return f"Card #{self.number}" if self.number else "Card"

    @property
    def rarity(self) -> str | None:
        """Display-ready rarity, or None when the card has nothing worth badging."""
        return pick_rarity(self.attributes)

    @property
    def subtitle(self) -> str:
        bits = [b for b in (self.year, self.release_name or self.set_name) if b]
        line = " ".join(bits)
        if self.number:
            line = f"{line} #{self.number}" if line else f"#{self.number}"
        if self.parallel_name:
            line = f"{line} · {self.parallel_name}" if line else self.parallel_name
        return line


# How a price was arrived at, best evidence first. Anything below SOLD_ANY is an
# estimate and must be shown as one — an asking price is not a sale.
BASIS_LABELS = {
    "sold": "sold",
    "sold_any": "sold, any variant",
    "sold_old": "older sales",
    "asking": "asking prices",
    "listed": "listed now",
    "pricecharting": "PriceCharting",
    "bulk": "bulk rate",
    "none": "no data",
}
# PriceCharting is a price guide, not a guess — it is not an estimate.
ESTIMATED_BASES = frozenset({"sold_old", "asking", "listed", "bulk"})


@dataclass
class Valuation:
    """A single market price, plus enough context to defend it on stream."""

    value: float | None = None
    sample_size: int = 0
    statistic: str = "median"
    period: str = "90d"
    listing_type: str = "auction"
    last_sale_date: str | None = None
    low: float | None = None
    high: float | None = None
    note: str | None = None
    # Where the number came from, and whether it is a comp or a guess.
    basis: str = "none"
    estimated: bool = False

    @property
    def basis_label(self) -> str:
        return BASIS_LABELS.get(self.basis, self.basis)

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "sample_size": self.sample_size,
            "statistic": self.statistic,
            "period": self.period,
            "listing_type": self.listing_type,
            "last_sale_date": self.last_sale_date,
            "low": self.low,
            "high": self.high,
            "note": self.note,
            "basis": self.basis,
            "basis_label": self.basis_label,
            "estimated": self.estimated,
        }


# Attributes arrive as raw slugs prefixed by their segment, e.g.
# "pokemon-double-rare". Most of what comes back is format legality and card-type
# noise ("pokemon-legal-standard", "pokemon-stage-2", "pokemon-darkness"); an
# overlay wants the one thing a viewer cares about, which is the rarity.
# Allowlisted on purpose: an unrecognised slug renders no badge, which is a far
# better failure than putting "pokemon-darkness" on stream.
RARITY_KEYWORDS = (
    "secret",
    "special-illustration",
    "illustration",
    "hyper",
    "ultra",
    "full-art",
    "double-rare",
    "radiant",
    "amazing",
    "shiny",
    "holo",
    "promo",
    "rare",
)

# Slugs are prefixed with their segment; only strip a prefix we recognise, so a
# bare "double-rare" is never truncated to "Rare".
SEGMENT_PREFIXES = frozenset(
    {"pokemon", "baseball", "basketball", "football", "hockey", "soccer",
     "magic", "yugioh", "lorcana", "wrestling", "racing"}
)


def format_attribute(raw: str) -> str:
    text = raw.replace("_", "-").strip().lower()
    head, _, tail = text.partition("-")
    if tail and head in SEGMENT_PREFIXES:
        text = tail
    return text.replace("-", " ").title()


def pick_rarity(attributes: list[str]) -> str | None:
    """The most interesting rarity in a card's attribute slugs, formatted."""
    normalised = [a.replace("_", "-").lower() for a in attributes]
    for keyword in RARITY_KEYWORDS:
        for raw, norm in zip(attributes, normalised):
            if keyword in norm and "legal" not in norm:
                return format_attribute(raw)
    return None


def pick_detection(detections: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the detection to show.

    Prefer an exact catalog match (one carrying `card.id`) over a set-level match,
    then prefer higher confidence. Without this an image containing a stray second
    card can hand back a set-level guess ahead of the real match.
    """
    if not detections:
        return None

    def rank(detection: dict[str, Any]) -> tuple[int, int]:
        card = detection.get("card") or {}
        return (
            1 if card.get("id") else 0,
            CONFIDENCE_RANK.get(detection.get("confidence") or "", 0),
        )

    return max(detections, key=rank)


def parse_detection(detection: dict[str, Any], messages: list[str] | None = None) -> Identification:
    card = detection.get("card") or {}
    parallel = card.get("parallel") or {}
    return Identification(
        card_id=card.get("id"),
        name=card.get("name"),
        number=card.get("number"),
        year=card.get("year"),
        set_name=card.get("setName"),
        release_name=card.get("releaseName"),
        manufacturer=card.get("manufacturer"),
        parallel_id=parallel.get("id"),
        parallel_name=parallel.get("name"),
        numbered_to=card.get("numberedTo") or parallel.get("numberedTo"),
        attributes=list(card.get("attributes") or []),
        confidence=detection.get("confidence"),
        suggestions=list(card.get("suggestions") or []),
        messages=list(messages or []),
        fields=list(card.get("fields") or []),
    )


def summarise_prices(records: list[dict[str, Any]], statistic: str) -> tuple[float | None, int, float | None, float | None, str | None]:
    """Collapse sale records into (value, sample_size, low, high, last_sale_date)."""
    priced = [
        r
        for r in records
        if isinstance(r.get("price"), (int, float)) and r["price"] > 0
    ]
    if not priced:
        return None, 0, None, None, None

    prices = sorted(float(r["price"]) for r in priced)
    dates = sorted((r["date"] for r in priced if r.get("date")), reverse=True)
    last_sale = dates[0] if dates else None

    if statistic == "mean":
        value = statistics.fmean(prices)
    elif statistic == "latest":
        # "Most recent" needs the record, not the sorted price list.
        dated = [r for r in priced if r.get("date")]
        newest = max(dated, key=lambda r: r["date"]) if dated else priced[-1]
        value = float(newest["price"])
    else:
        value = statistics.median(prices)

    return round(value, 2), len(prices), prices[0], prices[-1], last_sale


def field_value(fields: list[dict[str, Any]], key: str) -> str | None:
    for entry in fields or []:
        if str(entry.get("key", "")).upper() == key.upper():
            return entry.get("value")
    return None


def _basis_note(basis: str, period: str) -> str | None:
    if basis == "sold":
        return None
    if basis == "sold_any":
        return "No comps for this exact variant — widened to all variants"
    if basis == "sold_old":
        return f"No sales in the last {period} — using older sales, so treat it as a rough figure"
    if basis == "asking":
        return "No completed sales — based on Buy It Now asking prices, which run high"
    if basis == "listed":
        return "No sales history — based on what the card is currently listed for"
    return None


class CardSightClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str,
        segment: str | None,
        rate_limit: float = DEFAULT_RATE_LIMIT,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._segment = segment
        self._session: aiohttp.ClientSession | None = None
        self._limiter = RateLimiter(rate_limit)

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-API-Key": self._api_key} if self._api_key else {},
                timeout=aiohttp.ClientTimeout(total=60),
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise CardSightError("HTTP session is not running")
        if not self._api_key:
            raise CardSightError("No CardSight API key configured", status=401)
        return self._session

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        session = self._require_session()
        url = f"{self._base}{path}"

        for attempt in range(RATE_LIMIT_RETRIES + 1):
            await self._limiter.acquire()
            async with session.request(method, url, **kwargs) as response:
                body = await response.text()

                # A 429 is transient by definition; wait the interval the server
                # named and try again rather than failing a scan on air.
                if response.status == 429 and attempt < RATE_LIMIT_RETRIES:
                    delay = retry_after_seconds(body)
                    log.warning("rate limited on %s — retrying in %.0fs", path, delay)
                    await asyncio.sleep(delay)
                    continue

                if response.status >= 400:
                    raise CardSightError(
                        f"{method} {path} failed ({response.status}): {body[:300]}",
                        status=response.status,
                    )
                try:
                    return await response.json(content_type=None)
                except Exception as exc:  # pragma: no cover - malformed upstream
                    raise CardSightError(f"{method} {path} returned non-JSON: {exc}") from exc

        raise CardSightError(f"{method} {path} still rate limited after retries", status=429)

    async def health_auth(self) -> bool | None:
        """Validate the key at startup so a typo surfaces before the stream, not during.

        Tri-state on purpose. Only a 401/403 actually means the key is bad; any
        other failure means the check could not be made, and reporting that as a
        rejected key would send someone hunting a problem that is not there. Note
        the path has no /v1 prefix — the health routes sit at the root.
        """
        try:
            await self._json("GET", "/health/auth")
            return True
        except CardSightError as exc:
            if exc.status in (401, 403):
                log.error("CardSight rejected the API key: %s", exc)
                return False
            log.warning("Could not verify the API key (%s) — continuing anyway", exc)
            return None

    async def identify(self, image: bytes, content_type: str = "image/jpeg") -> Identification:
        if len(image) > MAX_UPLOAD_BYTES:
            raise CardSightError("Image exceeds the 20MB API limit", status=413)

        path = f"/v1/identify/card/{self._segment}" if self._segment else "/v1/identify/card"

        # A FormData is consumed once it is sent, so a retry needs a fresh one.
        last: CardSightError | None = None
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            form = aiohttp.FormData()
            form.add_field("image", image, filename="card.jpg", content_type=content_type)
            try:
                payload = await self._json("POST", path, data=form)
                break
            except CardSightError as exc:
                if exc.status != 429 or attempt == RATE_LIMIT_RETRIES:
                    raise
                last = exc
                await asyncio.sleep(1.0)
        else:  # pragma: no cover - loop always breaks or raises
            raise last or CardSightError("identify failed")

        messages = [
            m.get("message", "") for m in (payload.get("messages") or []) if m.get("message")
        ]
        detection = pick_detection(payload.get("detections") or [])
        if detection is None:
            return Identification(messages=messages or ["No card detected in the image"])
        return parse_detection(detection, messages)

    async def _sale_records(self, card_id: str, params: dict[str, str]) -> list[dict[str, Any]]:
        payload = await self._json("GET", f"/v1/pricing/{card_id}", params=params)
        return self._usable(((payload.get("raw") or {}).get("records")) or [])

    async def _title_records(self, query: str, period: str, limit: int) -> list[dict[str, Any]]:
        """Free-text search over listing titles. A diagnostic, NOT a pricing source.

        It looked like the answer for unlinked Japanese prints, and it is not.
        Searching "Heracross Clay Burst" returns fourteen completed sales, every
        one of them the Art Rare 072/071 rather than the #6 common that was
        actually scanned — median $11.75 for a card worth cents. Titles never
        mention the common's number, so no filter rescues it: the base card has
        no sales, and the search quietly answers about a different card.

        Used by `live_check.py --diagnose` to show what is out there.
        """
        payload = await self._json(
            "GET",
            "/v1/pricing/search",
            params={"q": query, "period": period, "listing_type": "both", "limit": str(limit)},
        )
        results = payload.get("results") or []
        # No grade filter on this endpoint, so drop graded copies here: a PSA 10
        # comp would wreck the number for a card that came out of a pack minutes ago.
        return [r for r in results if not r.get("grade")]

    @staticmethod
    def _usable(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rows that carry a real price.

        The marketplace endpoint returns `listing_type: "search"` placeholders
        with `price: null` — a card can report one "active listing" that is only
        a catalog stub. Counting those as data makes a card look findable when
        nothing is for sale.
        """
        return [
            r for r in records
            if r.get("listing_type") != "search" and isinstance(r.get("price"), (int, float))
        ]

    async def _listed_records(self, card_id: str, limit: int) -> list[dict[str, Any]]:
        """Active listings — what the card is on sale for right now, not what it sold for."""
        payload = await self._json(
            "GET",
            f"/v1/marketplace/{card_id}",
            params={"grade_id": "null", "listing_type": "both", "limit": str(limit)},
        )
        return self._usable(((payload.get("raw") or {}).get("records")) or [])

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
        """Value a card, falling back through weaker evidence until something lands.

        Ungraded throughout (`grade_id=null`) because a pack-fresh card is raw.
        The ladder, best evidence first:

          sold      completed sales for the exact detected parallel
          sold_any  completed sales for any variant of the card
          sold_old  completed sales with the time limit removed
          asking    Buy It Now asks — what sellers want, not what anyone paid
          listed    active marketplace listings

        The first two are real comps. Everything below is flagged `estimated`, so
        the overlay can show it as a guess rather than passing it off as a sale.
        Each rung is a separate billed call, but a rung only runs when the one
        above it found nothing — a card with normal sales data costs one call.
        """
        base = {"grade_id": "null", "limit": str(limit)}

        # `parallel_id="null"` means base card only; a UUID pins the variant.
        ladder: list[tuple[str, dict[str, str]]] = [
            ("sold", dict(base, parallel_id=parallel_id or "null", period=period, listing_type=listing_type)),
        ]
        if fallbacks:
            ladder.append(("sold_any", dict(base, period=period, listing_type=listing_type)))
            if period.lower() != "all":
                ladder.append(("sold_old", dict(base, period="all", listing_type=listing_type)))
            # Only worth asking for asks if we were not already counting them.
            if listing_type == "auction":
                ladder.append(("asking", dict(base, period=period, listing_type="fixed")))

        for basis, params in ladder:
            records = await self._sale_records(card_id, params)
            if not records:
                continue
            value, count, low, high, last_sale = summarise_prices(records, statistic)
            if value is None:
                continue
            return Valuation(
                value=value,
                sample_size=count,
                statistic=statistic,
                period=params.get("period", period),
                listing_type=params.get("listing_type", listing_type),
                last_sale_date=last_sale,
                low=low,
                high=high,
                basis=basis,
                estimated=basis in ESTIMATED_BASES,
                note=_basis_note(basis, period),
            )

        if fallbacks:
            try:
                records = await self._listed_records(card_id, limit)
            except CardSightError as exc:
                log.info("marketplace lookup failed for %s: %s", card_id, exc)
                records = []
            if records:
                value, count, low, high, _ = summarise_prices(records, statistic)
                if value is not None:
                    return Valuation(
                        value=value,
                        sample_size=count,
                        statistic=statistic,
                        period=period,
                        listing_type="both",
                        low=low,
                        high=high,
                        basis="listed",
                        estimated=True,
                        note=_basis_note("listed", period),
                    )

        # A card with no evidence anywhere is worth 0.00 rather than a blank:
        # it keeps pack totals addable and stops a dash reading as "still loading".
        return Valuation(
            value=0.0,
            statistic=statistic,
            period=period,
            listing_type=listing_type,
            basis="none",
            note=f"No sales or listings found for this card in the last {period}",
        )

    async def card_image(self, card_id: str) -> tuple[bytes, str]:
        """Catalog art for a card. Returns (bytes, content-type)."""
        session = self._require_session()
        url = f"{self._base}/v1/images/cards/{card_id}"
        async with session.get(url, params={"format": "raw", "default": "false"}) as response:
            if response.status >= 400:
                raise CardSightError(f"No catalog image for {card_id}", status=response.status)
            content_type = response.headers.get("Content-Type", "image/jpeg")
            return await response.read(), content_type

    async def search(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """Fuzzy catalog search, narrowed to cards.

        The endpoint returns a mixed list of cards, sets, releases and parallels;
        `type=card` is the only kind the correction UI can apply. The configured
        segment narrows it further, so a Pokemon night does not surface baseball.
        """
        params: dict[str, str] = {"q": query, "take": str(limit), "type": "card"}
        if self._segment:
            params["segment"] = self._segment

        try:
            payload = await self._json("GET", "/v1/catalog/search", params=params)
        except CardSightError:
            if not self._segment:
                raise
            # `segment` wants a UUID or an exact name; a shortname like "pokemon"
            # is fine for identify but may not match here. Retry unfiltered.
            params.pop("segment")
            payload = await self._json("GET", "/v1/catalog/search", params=params)

        results = payload.get("results")
        return [r for r in results if r.get("type") == "card"] if isinstance(results, list) else []
