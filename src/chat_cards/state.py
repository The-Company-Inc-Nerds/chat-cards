"""Session state: packs, cards, persistence, and the broadcast hub.

The whole show is one Session. It holds an ordered list of Packs and an ordered
list of Cards; every card belongs to at most one pack. State is written to disk on
every mutation so an OBS refresh — or a service restart between packs — costs
nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

STATE_VERSION = 1

# Card lifecycle.
STATUS_IDENTIFYING = "identifying"  # uploaded, API call in flight
STATUS_PRICED = "priced"  # identified and valued
STATUS_UNMATCHED = "unmatched"  # image processed, no confident catalog match
STATUS_ERROR = "error"  # the lookup itself failed


def _now() -> float:
    return time.time()


def _uid() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Card:
    uid: str
    seq: int
    pack_id: str | None = None
    status: str = STATUS_IDENTIFYING

    card_id: str | None = None
    # `name` is always safe to put on stream: Latin script, English where we have
    # it. `original_name` keeps whatever the catalog actually said, so the
    # producer can see what was matched even when it is Japanese or Korean.
    name: str | None = None
    original_name: str | None = None
    name_is_english: bool = True
    language: str | None = None
    subtitle: str = ""
    number: str | None = None
    year: str | None = None
    set_name: str | None = None
    release_name: str | None = None
    parallel_id: str | None = None
    parallel_name: str | None = None
    numbered_to: int | None = None
    attributes: list[str] = field(default_factory=list)
    rarity: str | None = None
    confidence: str | None = None
    suggestions: list[dict[str, Any]] = field(default_factory=list)

    value: float | None = None
    value_source: str = "none"  # "market" | "manual" | "none"
    pricing: dict[str, Any] = field(default_factory=dict)

    # "pending" until enrichment resolves which picture the overlay should show.
    image_source: str = "pending"
    has_capture: bool = False
    capture_type: str = "image/jpeg"

    error: str | None = None
    messages: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=_now)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        # Null until something is actually servable, so clients do not fetch a 404
        # in the moment between the upload landing and the capture being written.
        # `has_capture` is part of the cache key: the producer's photo shows during
        # identification, then the URL changes when catalog art replaces it.
        has_image = self.has_capture or (self.image_source == "catalog" and bool(self.card_id))
        data["image_url"] = (
            f"/api/card/{self.uid}/image?v={self.image_source}-{int(self.has_capture)}"
            if has_image
            else None
        )
        return data


@dataclass
class Pack:
    id: str
    index: int
    size: int
    winner: str | None = None
    label: str | None = None
    # An opaque correlation id from whoever opened the pack (multichat sends the
    # giveaway winner's user id / turn id), echoed back in reports so the opener
    # can tie the pull to the right turn. Not shown on stream.
    ref: str | None = None
    opened_at: float = field(default_factory=_now)
    closed_at: float | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Session:
    started_at: float = field(default_factory=_now)
    seq: int = 0
    packs: list[Pack] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "started_at": self.started_at,
            "seq": self.seq,
            "packs": [p.to_json() for p in self.packs],
            "cards": [asdict(c) for c in self.cards],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Session":
        session = cls(
            started_at=data.get("started_at", _now()),
            seq=int(data.get("seq", 0)),
        )
        for raw in data.get("packs", []):
            # Tolerate fields added by a newer version (same rationale as cards
            # below) so a rollback cannot wedge on a state file it half-understands.
            known = {k: v for k, v in raw.items() if k in Pack.__dataclass_fields__}
            session.packs.append(Pack(**known))
        for raw in data.get("cards", []):
            # Tolerate fields added by a newer version so a rollback cannot wedge
            # the service on a state file it half-understands.
            known = {k: v for k, v in raw.items() if k in Card.__dataclass_fields__}
            session.cards.append(Card(**known))
        return session


def _sum_values(cards: Iterable[Card]) -> float:
    return round(sum(c.value for c in cards if c.value is not None), 2)


class Store:
    """Owns the session, its file on disk, and the set of live WebSocket listeners."""

    def __init__(self, path: Path, default_pack_size: int) -> None:
        self._path = path
        self._default_pack_size = default_pack_size
        self._session = Session()
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue] = set()

    # ---------------------------------------------------------------- persistence

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._session = Session.from_json(data)
            log.info(
                "Restored session: %d cards across %d packs",
                len(self._session.cards),
                len(self._session.packs),
            )
        except Exception as exc:
            # A corrupt state file must never stop the service coming up mid-stream.
            log.error("Could not read %s (%s) — starting a fresh session", self._path, exc)
            self._session = Session()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._session.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, self._path)  # atomic: readers never see a half-written file

    # ------------------------------------------------------------------ broadcast

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _publish(self, event: str | None = None, **extra: Any) -> None:
        message = {"type": "state", "event": event, "state": self.snapshot(), **extra}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # A wedged client must not slow the reveal for everyone else.
                log.warning("Dropping update for a subscriber that stopped reading")

    async def commit(self, event: str | None = None, **extra: Any) -> None:
        self._save()
        self._publish(event, **extra)

    # -------------------------------------------------------------------- reading

    @property
    def current_pack(self) -> Pack | None:
        for pack in reversed(self._session.packs):
            if pack.closed_at is None:
                return pack
        return None

    def cards_in_pack(self, pack_id: str | None) -> list[Card]:
        if pack_id is None:
            return []
        return [c for c in self._session.cards if c.pack_id == pack_id]

    def card(self, uid: str) -> Card | None:
        return next((c for c in self._session.cards if c.uid == uid), None)

    def snapshot(self) -> dict[str, Any]:
        cards = sorted(self._session.cards, key=lambda c: c.seq)
        pack = self.current_pack
        pack_cards = self.cards_in_pack(pack.id if pack else None)

        return {
            "version": STATE_VERSION,
            "started_at": self._session.started_at,
            "default_pack_size": self._default_pack_size,
            "totals": {
                "value": _sum_values(cards),
                "count": len(cards),
                "packs": len(self._session.packs),
            },
            "pack": (
                {
                    **pack.to_json(),
                    "value": _sum_values(pack_cards),
                    "revealed": len(pack_cards),
                    # A pack can overrun its stated size; never report 6/5.
                    "remaining": max(0, pack.size - len(pack_cards)),
                }
                if pack
                else None
            ),
            "packs": [p.to_json() for p in self._session.packs],
            "cards": [c.to_json() for c in cards],
        }

    # -------------------------------------------------------------------- writing

    async def open_pack(
        self,
        size: int,
        winner: str | None,
        label: str | None,
        ref: str | None = None,
    ) -> Pack:
        async with self._lock:
            # Opening a pack implicitly finishes the one before it.
            current = self.current_pack
            if current:
                current.closed_at = _now()
            pack = Pack(
                id=_uid(),
                index=len(self._session.packs) + 1,
                size=max(1, size),
                winner=winner or None,
                label=label or None,
                ref=ref or None,
            )
            self._session.packs.append(pack)
            await self.commit("pack_opened", pack_id=pack.id)
            return pack

    async def close_pack(self) -> Pack | None:
        async with self._lock:
            pack = self.current_pack
            if pack is None:
                return None
            pack.closed_at = _now()
            await self.commit("pack_closed", pack_id=pack.id)
            return pack

    async def add_card(self) -> Card:
        """Reserve a slot the moment the shutter fires, before any API call.

        The sequence number is assigned here, so the reel keeps reveal order even
        when a slow price lookup finishes after a later card's.
        """
        async with self._lock:
            self._session.seq += 1
            pack = self.current_pack
            card = Card(uid=_uid(), seq=self._session.seq, pack_id=pack.id if pack else None)
            self._session.cards.append(card)
            await self.commit("card_added", card_uid=card.uid)
            return card

    async def update_card(self, uid: str, event: str = "card_updated", **fields: Any) -> Card | None:
        async with self._lock:
            card = self.card(uid)
            if card is None:
                return None
            for key, value in fields.items():
                if hasattr(card, key):
                    setattr(card, key, value)
            await self.commit(event, card_uid=uid)
            return card

    async def remove_card(
        self, uid: str, event: str = "card_removed", reason: str | None = None
    ) -> bool:
        async with self._lock:
            card = self.card(uid)
            if card is None:
                return False
            self._session.cards.remove(card)
            # The event distinguishes an operator deleting a scan from the server
            # dropping one, so the phone can say why a card vanished.
            await self.commit(event, card_uid=uid, reason=reason)
            return True

    async def reset(self) -> None:
        async with self._lock:
            self._session = Session()
            await self.commit("session_reset")
