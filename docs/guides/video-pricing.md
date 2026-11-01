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
  first settles on screen. That is where the price callout goes.
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

Shorts are cut fast. A card might be on screen for half a second and is never
fully still. Word-by-word captions also keep the top of the frame moving. So the
script doesn't wait for stillness:

1. It looks only at the middle of the frame, where cards are held. That skips
   the captions and the pile of cards you've already set down. `--region`
   changes the area.
2. Every moment where motion dips below the moments around it becomes a
   candidate. This is the instant a swiped card settles, however briefly.
3. Each candidate frame goes to CardSight. Repeated reads of the same card are
   merged into one row. The wrapper, hands and code cards fail to identify and
   drop out.

`--cards` tells it how many cards to expect. It is optional but you should give
it. If it finds more than that, it first drops repeats of a card it has already
seen, which is usually the pile of finished cards in shot. After that it keeps
the most confident matches and lists what it dropped. If it finds fewer, the CSV
says so.

Each identify call is billed. The total is capped by `--max-identify`, which
defaults to three per expected card.

## When it gets one wrong

| Symptom | Try |
|---|---|
| Misses cards that are flipped very quickly | `--spacing 0.2` |
| Cards are held near the top or edge of the frame | `--region full` or e.g. `--region 0,0.15,1,1` |
| Too many billed calls on blurry frames | `--still-threshold 40` (skips frames blurrier than that) |
| Short of the count | raise `--max-identify` if it reported skipping candidates, or lower `--spacing` |
| Want to see what was sent | `--save-frames ./frames` |
| Still wrong | Give the moments yourself: `--at 0:03.2,0:05,0:07.5` |

`--at` skips detection. It identifies exactly the frames at those timestamps and
writes one row per timestamp, so the CSV lines up with your edit even when a
card fails to match. Any row with a low-confidence match is flagged in the
`note` column.

## Fixing the CSV and repricing

CardSight can only identify cards in its catalog, and brand-new sets lag behind.
In the Day 08 test the Japanese 30th Celebration cards came back as other cards
or other printings ("Frogadier" for a Pikachu). PriceCharting does list them. So
correct the rows yourself, then reprice from the CSV:

```sh
price-video --reprice opening.prices.csv --pricecharting-token-file ~/pricecharting-token --srt opening.srt
```

This makes no CardSight calls and doesn't need the video. Each row is priced
from these columns:

| Column | What to put |
|---|---|
| `name` | The card's English name, e.g. `Morpeko` |
| `number` | As printed, e.g. `052/103` |
| `set` | e.g. `30th Celebration` or `Cyber Judge` |
| `language` | `english`, `japanese`, `korean` or `chinese` |
| `variant` | e.g. `Reverse Holo`. Leave blank for the plain card |

Editing rules:

- **Add a missed card:** add a row with just `timestamp` (e.g. `0:51.4`) and the
  columns above. Rows are re-sorted by time and renumbered.
- **Remove a false card:** delete its row.
- **Set a price yourself:** type it in `price` and set `price_source` to
  `manual`. A reprice leaves it alone.
- **Rows it can't price:** a row with no single PriceCharting match keeps its old
  price, and its `note` says why. Usually the number, set or language needs
  fixing. Some cards are listed on PriceCharting with no price at all. In the
  test, the Korean Cyber Judge cards were like that, so give those a manual
  price.

The CSV is rewritten in place, and the previous version is kept as `.bak`. `-o`
writes the result to a different file instead. The running totals, the `TOTAL`
row and the `--srt` subtitles are all rebuilt.

## Prices

Prices are worked out the same way as on stream (see
[How prices are calculated](../reference/pricing.md)). PriceCharting comes
first when you pass `--pricecharting-token-file`, then CardSight's sold-comps
ladder, then the flat bulk rate. A price marked `estimated` (or `*` in the
terminal) is based on asking prices, active listings or the bulk rate, not on
real sales. Two copies of the same card are priced with a single lookup.
