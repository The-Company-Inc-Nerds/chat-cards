# How prices are calculated

CardSight's pricing endpoint does not return a price. It returns *sale records* —
individual listings with a price, a date and a source. Turning those into the one
number on the overlay is this app's decision, and it is worth understanding
before you defend a total on air.

## The default

For each identified card:

```
GET /v1/pricing/{card_id}
    ?grade_id=null            ungraded only
    &parallel_id=<detected>   or "null" for the base card
    &listing_type=auction     completed sales only
    &period=1y
    &limit=50
```

The overlay shows the **median** of those prices.

## Why each of those choices

**Ungraded.** A card that came out of a pack sixty seconds ago is raw. Graded
comps answer a different question — what it *could* be worth after grading — and
mixing them in would inflate every total.

**Auctions, not Buy It Now.** An auction row is a price somebody actually paid. A
fixed-price row is what a seller is *asking*, and an unsold listing can ask
anything. Including asks reliably produces numbers that are too high.

**Median, not mean.** Card sales data is full of outliers: bulk lots that happen
to mention the card's name, mis-listed grades, the one buyer who overpaid at 2am.
A single $400 row moves a mean; it barely moves a median.

**A year, not a quarter.** Outside the chase cards, singles sell infrequently.
A 90-day window leaves much of a typical pack with no comps at all, which is a
worse answer than a slightly stale one. The cost is recency: a wide window lags
a price that has genuinely moved, which matters most around a reprint or a
rotation. Narrow it with `pricing.period` if your sets are liquid enough.

**Scoped to the parallel.** A base card and its alt-art parallel are wildly
different money, so the query is pinned to the parallel the API detected — or to
`"null"`, which means base card only.

## When there are no comps

Plenty of cards have no recent completed auctions — bulk commons, and anything
from a set that came out last week. Rather than showing nothing, the app falls
down a ladder until something lands:

| Basis | What it is | Estimate? |
|---|---|---|
| `sold` | completed sales for the exact detected parallel | no |
| `sold_any` | completed sales for any variant of the card | no |
| `sold_old` | completed sales with the time limit removed | **yes** |
| `asking` | Buy It Now asks — what sellers want, not what anyone paid | **yes** |
| `listed` | active marketplace listings right now | **yes** |
| `bulk` | flat bulk rate, when nothing exists anywhere | **yes** |

Each rung is a separate billed call, but a rung only runs when the one above it
found nothing, so a card with normal sales data still costs a single call.

Completed sales outrank asking prices even when they are old: a price someone
actually paid eight months ago is better evidence than a price nobody has paid
yet.

### PriceCharting comes first

With `pricechartingTokenFile` set, PriceCharting's **ungraded** price is the
primary source and the CardSight ladder above becomes the fallback. One source
for price means a pack total sums comparable numbers rather than a mix of
definitions.

It is the only source verified to carry the Japanese prints CardSight has no
comps for at all — Charcadet #14 and Heracross #6 from Japanese Clay Burst both
resolve there, at $1.23 and $1.42, where every CardSight rung returned nothing.

Two things the integration has to get right, both verified against the live API:

- **Variant.** PriceCharting files variants as separate products with a
  bracketed suffix: `Growlithe #55` and `Growlithe [Reverse Holo] #55`. A
  best-match search for "growlithe legend maker" returns the *reverse holo*,
  which is twelve times the price. So the product list is fetched and the
  variant chosen from the parallel CardSight detected — no bracket for a base
  card. An ambiguous list returns nothing and falls through to CardSight.
- **Language.** Their Japanese sets are named "Pokemon Japanese Clay Burst"
  (Korean and Chinese work the same way), so the query includes the language
  CardSight reported. A product in a different language is never accepted. A
  Korean card with no Korean listing falls through rather than taking the
  English price. When the number still leaves several products, the language
  and set name break the tie.
- **Number.** Matched exactly against the product name after normalising the
  printed form. `052/103` searches as `#52`, and `#5` never matches `#55`.

Notes from their API: prices are integers in **pennies**, `loose-price` is the
ungraded figure, only current values are served (no historic sales), and the
documented limit is **one call per second** — the client stays under it.

### The bulk rate

A card with no evidence anywhere gets `pricing.bulkValue`, $0.10 by default,
flagged as an estimate. Japanese bulk commons genuinely have no individual
market: nobody sells them singly, so there is nothing to comp against. A flat
bulk rate is what a shop would actually pay, and it keeps a pack of them from
reading as worthless. Set it to `0` to show $0.00 instead.

### Why there is no title-matching rung

`/v1/pricing/search` matches on listing title and explicitly includes "listings
never matched to a canonical card", which looked like the answer for unlinked
Japanese prints. It was tried against the live API and removed.

Searching `Heracross Clay Burst` returns 14 completed sales. Every one of them is
the **Art Rare 072/071**, not the #6 common that was scanned — prices from $4.82
to $47.00, median **$11.75**, for a card worth cents. Titles never mention the
common's number (`Heracross Clay Burst 6` returns nothing at all), so no filter
rescues it: the base card has no sales, and the search quietly answers about a
different, far more valuable card.

Putting that on a stream would inflate a pack total by a factor of fifty. The
probe is still available through `live_check.py --diagnose`, where a human reads
the titles — it is a diagnostic, not a price.

## Why is *this* card $0.00?

Probe every rung for a specific card and see which ones actually hold data:

```bash
python3 tools/live_check.py --api-key-file KEY --diagnose \
    --card-id 8c0f5a8f-597e-4791-ac55-720ad129b8cb \
    --title "Charcadet Clay Burst 14"
```

```
card 8c0f5a8f-597e-4791-ac55-720ad129b8cb
  [      ] sold, base card, 1y                    0 rows
  [      ] sold, any variant, 1y                  0 rows
  [      ] sold, any variant, all time            0 rows
  [      ] asks (Buy It Now), all time            0 rows
  [      ] active listings                        0 rows
  [  ok  ] title search 'Charcadet Clay Burst'   50 rows (0 completed sales)
```

That is a real card, checked against the live API. Nothing under any rung, and
the only title hits are asking prices for other variants. **The card genuinely
has no comparable sales**, and no window setting changes that — $0.00 is the
correct answer, not a bug.

Japanese bulk commons look like this routinely: nobody sells them individually,
so there is nothing to comp against. If a pack full of them reading $0.00 looks
wrong on stream, type a nominal price on the phone — that is what the manual
override is for.

## Measuring the window on your own cards

Guessing at the window is unnecessary — price the same cards across several and
look at the coverage:

```bash
python3 tools/live_check.py --api-key-file KEY --compare --query "iono"
python3 tools/live_check.py --api-key-file KEY --compare \
    --card-id UUID --card-id UUID --periods 90d,1y,all
```

```
card                          90d              1y             all
--------------------------------------------------------------------
Iono                            —      $22.10/6      $21.40/14
Pikachu                  $1.10/4       $1.15/9       $1.15/31
Charizard ex           $294.00/18   $288.50/61    $271.00/140

priced                       2/3           3/3           3/3
```

Cells are price/sample-size. That table is the actual argument for a window: if
`90d` leaves a third of your cards blank and `1y` fills them in without moving
the prices much, widen it. Each cell is a billed call, so it prints the total
before it starts.

`statistic = "latest"` shows the most recent completed sale. It feels the most
live, and it is the noisiest — one odd sale sets the number.

## Caveats worth saying out loud

- Prices are comps, not offers. Nobody is bound to buy at them.
- A `~` price is an estimate, and an estimate built on asking prices reads high.
- `$0.00` means "no evidence found", not "provably worthless".

## Rate limits

The API allows **4 requests per second** and answers a burst with HTTP 429. One
card can take five calls if it walks the whole ladder, and several cards enrich
at once when a pack is opened quickly, so the ceiling is easy to hit exactly when
it matters. The client throttles to 3/s by default and retries a 429 after the
interval the server names. Change it with `services.chat-cards.rateLimit`, or
`--rate-limit`; `0` disables throttling.
- Thin data is thin. A median over four sales is a guess with a decimal point.
- Sealed-product prices move fast around a release; a wide window lags that.
- Pricing is cached per card for the session, so re-scanning the same card does
  not bill a second lookup — and does not pick up an intra-stream price change.
