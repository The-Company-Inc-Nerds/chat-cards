# Pricing a recorded opening

`price-video` takes a recorded pack opening, such as a short, and works out what
came out of it. It finds each card on screen, identifies and prices it, and
writes a CSV you can edit against: a timestamp for each card, its price, and the
running total.

```sh
price-video opening.mp4 --cards 10 --api-key-file ~/.secrets/cardsight
```

The CSV is written next to the clip as `opening.prices.csv`. Change that with
`-o`. The same table also prints to the terminal:

```
  #       time  card                                   price      total
  1   0:01.667  Bulbasaur                          $    0.82  $    0.82
  2   0:03.333  Gardevoir ex                       $   14.41  $   15.23
  3   0:05.000  Charizard ex                       $  294.51  $  309.74
     ...
     10 card(s)  TOTAL $374.25   (* = estimate)
```

Outside the dev shell the packaged command is `chat-cards-video`. It takes the
same arguments.

## Getting it into the edit

- **CSV**: one row per card, then a `TOTAL` row. `timestamp` is when the card
  first sits still on screen. That is where the price callout goes.
- **SRT**: add `--srt opening.srt` to also write a subtitle track. Each card
  shows `Name — $price` and `Total: $running` from its reveal until the next
  card. CapCut, Premiere and Resolve all import SRT. Style it as a text overlay
  and the callouts are already timed.

## Try it first without a key

```sh
price-video opening.mp4 --cards 10 --mock
```

Mock mode runs the real detection on your clip but returns made-up cards and
prices, and nothing is billed. Use it to check that the timestamps land on your
reveals before spending identify calls.

## How cards are found

Each reveal shows up as a swipe (motion) followed by a beat where the card is
held still. The clip is scanned for those still beats. The stillest frame of
each beat goes to CardSight, and repeated reads of the same card are merged into
one row. The wrapper, hands and code cards fail to identify and drop out.

`--cards` tells it how many cards to expect. It is optional but you should give
it. If it finds fewer, it checks more beats until the budget runs out. If it
finds more, it keeps the most confident matches and lists what it dropped.

Each identify call is billed. The total is capped by `--max-identify`, which
defaults to three per expected card.

## When it gets one wrong

| Symptom | Try |
|---|---|
| Misses cards that are only flashed briefly | `--min-hold 0.2` |
| Finds nothing in shaky handheld footage | `--still-threshold 4` (higher means more forgiving) |
| Short of the count | `--max-identify 40` |
| Want to see what was sent | `--save-frames ./frames` |
| Still wrong | Give the moments yourself: `--at 0:03.2,0:05,0:07.5` |

`--at` skips detection. It identifies exactly the frames at those timestamps and
writes one row per timestamp, so the CSV lines up with your edit even when a
card fails to match. Any row with a low-confidence match is flagged in the
`note` column.

## Prices

Prices are worked out the same way as on stream (see
[How prices are calculated](../reference/pricing.md)). PriceCharting comes
first when you pass `--pricecharting-token-file`, then CardSight's sold-comps
ladder, then the flat bulk rate. A price marked `estimated` (or `*` in the
terminal) is based on asking prices, active listings or the bulk rate, not on
real sales. Two copies of the same card are priced with a single lookup.
