"""Push pack-opening summaries back to multichat's giveaway ledger.

When multichat's giveaway integration opens a pack under a drawn winner, this
reports the cards pulled and their running value back to multichat's
``/api/turn-report`` endpoint, so its per-winner ledger is complete. It rides the
same Store event stream the overlay uses: every price update flows through with
no extra plumbing, and each POST carries the pack's full current state so a
dropped update is reconciled by the next one (multichat upserts by pack id).

A pack with no next update would stay wrong for good, so the reporter also
resends: every pack once when it starts, and every pack again once multichat
answers after a POST has failed.

``pack_summary`` is a pure function so it can be unit-tested without a live
server; the ``Reporter`` is the thin async wiring around it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Sequence

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

# Seconds between attempts to resend the session while multichat is not
# answering; the last one repeats. Close together at first because a restart of
# multichat is over in seconds, then once a minute for as long as it stays down.
# An attempt stops at its first failed POST, so a dead sink costs one refused
# connection per attempt however many packs are waiting.
RESYNC_RETRY_DELAYS = (2.0, 5.0, 15.0, 30.0, 60.0)


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

    Fire-and-forget from the app's side: a POST failure is logged but never
    interrupts the reveal, and nothing here is awaited by a request handler.

    What multichat missed is made up afterwards. Every pack in the session is
    owed a resend when the reporter starts — a new process cannot know which
    reports landed before it, nor whether multichat still has them — and again
    whenever a POST fails. The resend is one POST at a time and only while no
    live event is waiting, so a long session cannot hold up the pack on screen.
    """

    def __init__(
        self,
        store: Any,
        url: str,
        token: str | None,
        retry_delays: Sequence[float] = RESYNC_RETRY_DELAYS,
    ) -> None:
        self._store = store
        self._url = url
        self._token = token
        self._retry_delays = tuple(retry_delays)
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

        # True while multichat may be missing packs or holding stale ones.
        self._owed = True
        # Pack ids the resend in flight has yet to send; None between resends.
        self._backlog: deque[str] | None = None
        # One snapshot serves a whole resend, until an event shows it is stale.
        self._snapshot: dict[str, Any] | None = None
        self._resent = 0
        # Resend attempts that failed in a row, and the loop time of the next one.
        self._attempts = 0
        self._retry_at = 0.0

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
        loop = asyncio.get_running_loop()
        # Held across iterations rather than re-created per wait: cancelling a
        # get() that has already taken its item would drop that event.
        getter: asyncio.Task | None = None
        try:
            while True:
                if getter is None:
                    getter = asyncio.ensure_future(self._queue.get())
                # Live events first. A resend step is taken only when the wait
                # runs out with nothing queued, so the backlog can neither delay
                # the pack being opened now nor back the queue up until the
                # Store starts dropping its updates.
                timeout = max(0.0, self._retry_at - loop.time()) if self._owed else None
                done, _ = await asyncio.wait({getter}, timeout=timeout)
                try:
                    if done:
                        message, getter = getter.result(), None
                        await self._on_event(message)
                    else:
                        await self._resend_step()
                except Exception:  # one bad report must not end reporting for the night
                    log.exception("turn-report failed unexpectedly")
                    # Counted as a failed attempt so that whatever keeps raising
                    # is retried at the backed-off pace, not in a tight loop.
                    self._fell_behind()
                    self._attempts += 1
        finally:
            if getter is not None:
                getter.cancel()

    async def _on_event(self, message: dict[str, Any]) -> None:
        # Whatever a resend read before this event may no longer be current.
        self._snapshot = None
        if message.get("event") not in _PACK_EVENTS:
            return
        snapshot = message.get("state") or {}
        pack_id = self._pack_id_for(message, snapshot)
        if not pack_id:
            return
        summary = pack_summary(snapshot, pack_id)
        if summary is None:
            return

        if not await self._post(summary):
            self._fell_behind()
        elif self._owed:
            # multichat is answering, and it has just been given this pack as it
            # stands now: send it the rest straight away.
            if self._backlog is None:
                self._begin_resend()
            if pack_id in self._backlog:
                self._backlog.remove(pack_id)
            self._retry_at = 0.0

    def _begin_resend(self) -> None:
        self._snapshot = self._store.snapshot()
        self._backlog = deque(p["id"] for p in self._snapshot.get("packs", []) if p.get("id"))
        self._resent = 0

    async def _resend_step(self) -> None:
        """Send one owed pack, or finish the resend when none is left."""
        if self._backlog is None:
            self._begin_resend()
        assert self._backlog is not None

        while self._backlog:
            pack_id = self._backlog.popleft()
            if self._snapshot is None:
                self._snapshot = self._store.snapshot()
            summary = pack_summary(self._snapshot, pack_id)
            if summary is None:  # the session was reset under the resend
                continue
            # Only the first miss of an outage is worth a warning; the retries
            # that follow would repeat it every minute for as long as it lasts.
            if not await self._post(summary, quiet=self._attempts > 0):
                if self._attempts == 0:
                    log.warning(
                        "multichat is not taking pack reports at %s — retrying, and "
                        "every pack will be resent once it answers",
                        self._url,
                    )
                self._fell_behind()
                self._attempts += 1
                return
            self._resent += 1
            if self._backlog:
                return  # back to the queue: a live event may be waiting

        if self._resent or self._attempts:
            log.info("Resent %d pack summaries to %s", self._resent, self._url)
        self._owed = False
        self._backlog = None
        self._snapshot = None
        self._attempts = 0

    def _fell_behind(self) -> None:
        """A report did not land: abandon any resend in flight and schedule another."""
        self._owed = True
        self._backlog = None
        self._snapshot = None
        delay = self._retry_delays[min(self._attempts, len(self._retry_delays) - 1)]
        self._retry_at = asyncio.get_running_loop().time() + delay

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

    async def _post(self, summary: dict[str, Any], quiet: bool = False) -> bool:
        """POST one summary. True when multichat accepted it."""
        assert self._session is not None
        level = logging.DEBUG if quiet else logging.WARNING
        headers = {"content-type": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        try:
            async with self._session.post(
                self._url, json=summary, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    log.log(
                        level, "turn-report POST %s → HTTP %s %s", self._url, resp.status, body[:200]
                    )
                    return False
                return True
        except Exception as exc:  # a down/unreachable multichat must not stop the reveal
            log.log(level, "turn-report POST to %s failed: %s", self._url, exc)
            return False
