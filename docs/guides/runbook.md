# Stream-night runbook

## Before you go live

1. `curl -s http://localhost:8787/healthz` — confirm `cardsight_auth: true`.
   `null` with `"checks": "pending"` means CardSight has not been reached yet:
   the overlay works, but scans will fail until the internet is back.
2. Open `/scan` on the phone. The dot top-right should read **live**.
3. Check the OBS browser source is showing the overlay, then
   `POST /api/session/reset` to clear last week's numbers:
   ```bash
   curl -X POST http://localhost:8787/api/session/reset
   ```
4. Scan one junk common as a dry run. Confirm it reaches the overlay, then delete
   it from the feed and reset again.

## Per pack

1. Wheel lands on a follower.
2. Producer taps **New pack**, types the winner's name, checks the count (5 by
   default), taps **Start pack**. The overlay shows the sealed slots and a
   "NEW PACK" banner.
3. You open the pack. Each revealed card goes to the producer, who scans it.
4. The card appears on the overlay immediately as a placeholder, fills in with
   its name and artwork, then the price lands a beat later. Pack value, session
   total and the pip counter all update.

The reel keeps reveal order regardless of which lookup finishes first, so scan
as fast as you can hand cards over.

## When a scan goes wrong

Tap the card in the phone's list. The sheet offers, in order of speed:

- **Did you mean** — one tap to swap to an alternate the API already suggested.
  This is the usual fix for reprints. The label is whatever set name the API
  attached, which is sometimes unhelpful ("Checklist"); when it is, skip to
  search rather than guessing.
- **Search the catalog** — type a name, tap the right hit.
- **Market price** — type a number and save. Marks the card as a manual price.
- **Name on stream** — type the English name. Needed when a Japanese or Korean
  card's name could not be translated automatically; the row is flagged gold with
  the language code when that happens.
- **Delete** — removes the scan entirely; totals recalculate.

Every correction re-broadcasts, so the overlay fixes itself without a reload.

**Non-English cards.** Japanese, Korean and Chinese prints carry the localised
species name, and the overlay translates it — `リザードンex` goes up as
`Charizard ex`. The translation comes from the official localisations bundled in
`data/pokemon_names.json`, so it is a lookup, not a guess. A name that cannot be
resolved (trainer cards, unusual decorations) never goes on stream as-is: the
overlay falls back to the set and number, and the phone flags the card for you to
type the name.

## Things that will happen eventually

**A price has a `~` in front of it.** That is an estimate, not a comp — the card
had no recent completed sales, so the number came from older sales, asking
prices, or what it is currently listed for. The card sheet says which. Asking
prices in particular read high.

**A card shows ~$0.10.** That is the bulk rate: nothing was found anywhere, and
Japanese commons genuinely have no individual market. Change it with
`pricing.bulkValue`, or set it to `0` to show $0.00.

**A card shows $0.00.** With `bulkValue = 0`, nothing was found anywhere — no sales, no listings, in
any window. Japanese bulk commons do this routinely: nobody sells them singly, so
there is nothing to comp against. It is the honest answer rather than a bug.
Type a price on the phone if it matters. `live_check.py --diagnose --card-id …`
confirms it card by card.

**A scan disappears.** A scan the API cannot identify is dropped automatically
and the phone says "No match — scan that card again". Re-shoot it, flatter and
filling more of the frame. Set `autoDeleteUnmatched = false` if you would rather
keep failures on the reel and fix them by hand.

**The price looks too low for a hit.** The value is the median of *ungraded*
sales. A card that would grade a PSA 10 sells for far more graded, and the
overlay is deliberately not making that claim. See
[how prices are calculated](../reference/pricing.md).

**OBS drops the overlay.** Refresh the browser source. It reconnects and redraws
from the current state; nothing is lost.

**The service restarts mid-pack.** Session state is on disk. The overlay
reconnects on its own and the open pack is still open. A card that was caught
mid-lookup is settled after the restart rather than left spinning: one that had
already been identified keeps its name and art and gets its price looked up
again, as soon as one of the APIs can be reached; one that had not is dropped a few
seconds after the restart, with the same "scan that card again" on the phone
as any failed scan. (With `autoDeleteUnmatched = false` it stays on the reel,
marked "Interrupted by a restart".) A restart never re-submits a photo by
itself, because identification is the billed call.

**The internet drops.** The overlay, the totals and pack opening carry on —
none of them leaves the box. Scans fail while it is down and are dropped like
any other failed scan; re-scan once it is back. The service also starts fine
without the internet, and does not wait for it, so a reboot during an outage
still brings the overlay up straight away. A card that was waiting for its
price across that reboot keeps waiting until the uplink is there.

**The power goes out.** Same as a restart. Every change is forced to disk
before it replaces the previous state, so the session comes back whole — at
worst without the change that was being written as the power went. If the state
file is ever unreadable anyway, the service starts an empty session and keeps
the bad file next to it as `session.json.corrupt-<time>` instead of writing
over it.

## After the stream

Session state persists until you reset it, so you can screenshot final totals
whenever. `POST /api/session/reset` clears cards and packs; scanned photos and
the catalog-image cache stay in the state directory.
