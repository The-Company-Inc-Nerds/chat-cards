"""Push pack-opening summaries back to multichat's giveaway ledger.

When multichat's giveaway integration opens a pack under a drawn winner, this
reports the cards pulled and their running value back to multichat's
``/api/turn-report`` endpoint, so its per-winner ledger is complete. It rides the
same Store event stream the overlay uses: every price update flows through with
no extra plumbing, and each POST carries the pack's full current state so a
dropped update is reconciled by the next one (multichat upserts by pack id).

``pack_summary`` is a pure function so it can be unit-tested without a live
server; the ``Reporter`` is the thin async wiring around it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# Events that can change a pack's cards or value. Others (hello, ping, session
# reset) don't map to a single pack, so the reporter ignores them.
_PACK_EVENTS = {
    "pack_opened",
    "pack_closed",
    "card_added",
    "card_identified",
    "card_priced",
    "card_corrected",
    "card_removed",
    "card_failed",
}


def _sec_to_ms(value: Any) -> int | None:
    """multichat timestamps are epoch ms; chat-cards stores seconds."""
    if isinstance(value, (int, float)):
        return int(value * 1000)
    return None


def pack_summary(snapshot: dict[str, Any], pack_id: str) -> dict[str, Any] | None:
    """Build the wire summary for one pack from a Store snapshot. Pure.

    Returns ``None`` when the pack isn't in the snapshot (e.g. it was removed).
    Field names are camelCase to match multichat's PackReport wire format.
    """
    pack = next((p for p in snapshot.get("packs", []) if p.get("id") == pack_id), None)
    if pack is None:
        return None

    cards = [c for c in snapshot.get("cards", []) if c.get("pack_id") == pack_id]
    total = round(sum((c.get("value") or 0.0) for c in cards), 2)

    return {
        "packId": pack_id,
        "ref": pack.get("ref"),
        "winner": pack.get("winner"),
        "label": pack.get("label"),
        "index": pack.get("index"),
        "size": pack.get("size"),
        "openedAt": _sec_to_ms(pack.get("opened_at")) or 0,
        "closedAt": _sec_to_ms(pack.get("closed_at")),
        "totalValue": total,
        "cardCount": len(cards),
        "cards": [
            {
                "name": c.get("name") or "",
                "number": c.get("number"),
                "set": c.get("set_name"),
                "rarity": c.get("rarity"),
                "value": round(c.get("value") or 0.0, 2),
                "image": c.get("image_url"),
            }
            for c in cards
        ],
    }


class Reporter:
    """Subscribes to the Store and POSTs pack summaries to multichat.

    Fire-and-forget: a POST failure is logged but never interrupts the reveal —
    the next event's full snapshot reconciles whatever was missed.
    """

    def __init__(self, store: Any, url: str, token: str | None) -> None:
        self._store = store
        self._url = url
        self._token = token
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._queue = self._store.subscribe()
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._queue is not None:
            self._store.unsubscribe(self._queue)
        if self._session is not None:
            await self._session.close()

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            message = await self._queue.get()
            if message.get("event") not in _PACK_EVENTS:
                continue
            snapshot = message.get("state") or {}
            pack_id = self._pack_id_for(message, snapshot)
            if not pack_id:
                continue
            summary = pack_summary(snapshot, pack_id)
            if summary is not None:
                await self._post(summary)

    @staticmethod
    def _pack_id_for(message: dict[str, Any], snapshot: dict[str, Any]) -> str | None:
        # Pack events carry pack_id directly; card events carry the card uid, so
        # resolve it to the card's pack via the snapshot.
        if message.get("pack_id"):
            return message["pack_id"]
        uid = message.get("card_uid")
        if uid:
            for card in snapshot.get("cards", []):
                if card.get("uid") == uid:
                    return card.get("pack_id")
        return None

    async def _post(self, summary: dict[str, Any]) -> None:
        assert self._session is not None
        headers = {"content-type": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        try:
            async with self._session.post(
                self._url, json=summary, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    log.warning(
                        "turn-report POST %s → HTTP %s %s", self._url, resp.status, body[:200]
                    )
        except Exception as exc:  # a down/unreachable multichat must not stop the reveal
            log.warning("turn-report POST to %s failed: %s", self._url, exc)
