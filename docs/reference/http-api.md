# HTTP API

Everything is JSON except the scan upload and the image endpoints. If a token is
configured, pass it as `Authorization: Bearer <token>`, `?token=…`, or the
`cc_token` cookie.

## Pages

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/scan` | producer's scanner UI |
| `GET` | `/overlay` | OBS browser source |
| `GET` | `/healthz` | liveness plus CardSight key status; never requires a token |

## State

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/state` | full session snapshot |
| `GET` | `/ws` | WebSocket; a snapshot on connect and after every change |

A WebSocket frame is `{type: "state", event, state}`. `event` is one of
`hello`, `pack_opened`, `pack_closed`, `card_added`, `card_identified`,
`card_priced`, `card_updated`, `card_corrected`, `card_removed`, `card_failed`,
`session_reset` — the overlay uses it to trigger animations, and ignores frames
it does not recognise.

`card_failed` carries a `reason` and means the scan was dropped because the API
could not identify it. The scanner turns that into a "scan it again" prompt;
without it a vanishing row would look like nothing happened.

The snapshot:

```jsonc
{
  "totals": { "value": 309.74, "count": 3, "packs": 1 },
  "pack": {
    "id": "…", "index": 1, "size": 5, "winner": "@moosefan92",
    "value": 309.74, "revealed": 3, "remaining": 2
  },
  "cards": [
    {
      "uid": "…", "seq": 3, "pack_id": "…",
      "status": "priced",                  // identifying | priced | unmatched | error
      "card_id": "…",
      "name": "Charizard ex",              // always safe to put on stream
      "original_name": "リザードンex",        // what the catalog actually said
      "name_is_english": true,             // false -> prompt the operator
      "language": "ja",
      "subtitle": "2023 Scarlet & Violet — 151 #223",
      "value": 294.51, "value_source": "market",   // market | manual | none
      "pricing": {
        "sample_size": 18, "statistic": "median", "period": "90d",
        "basis": "sold",                   // sold | sold_any | sold_old | asking | listed | none
        "basis_label": "sold",
        "estimated": false,                // true -> render with a leading ~
        "note": null
      },
      "confidence": "High",
      "rarity": "Double Rare",
      "suggestions": [ … ],
      "image_url": "/api/card/…/image?v=catalog-1"   // null until something is servable
    }
  ]
}
```

Cards are ordered by `seq`, which is assigned at upload. Enrichment runs
concurrently, so a slow lookup never reorders the reel.

## Scanning and packs

| Method | Path | Body | Purpose |
|---|---|---|---|
| `POST` | `/api/scan` | multipart, field `image` | queue a card; returns `{uid, seq}` at once |
| `POST` | `/api/pack` | `{size, winner?, label?, ref?}` | open a pack, closing any open one |
| `POST` | `/api/pack/close` | — | close the current pack |
| `POST` | `/api/session/reset` | — | clear all packs and cards |

`POST /api/scan` returns as soon as the upload is stored. Identification and
pricing happen in the background and arrive over the WebSocket.

`ref` is an opaque correlation id stored on the pack and echoed back in every
report — it is how multichat matches a pack to the giveaway turn that opened it
(see [the giveaway loop](../guides/giveaway-loop.md)). It is never rendered on
the overlay.

## Corrections

| Method | Path | Body |
|---|---|---|
| `PATCH` | `/api/card/{uid}` | `{value?, name?, subtitle?, card_id?}` |
| `DELETE` | `/api/card/{uid}` | — |

Setting `value` marks the card `value_source: "manual"`; `null` clears it.
Setting `card_id` swaps the identity, re-fetches the catalog artwork, and
re-prices against the new card — unless the same request also sets `value`, in
which case the operator's price wins. Setting `name` marks the card as having an
English name, which is how a failed translation gets fixed.

## Images

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/card/{uid}/image` | catalog artwork if available, the producer's photo otherwise |
| `GET` | `/api/search?q=…` | catalog search, for the correction UI |

Catalog art comes from CardSight's `/v1/images/cards/{id}`, which requires the
API key — so the browser can never fetch it directly. It is proxied and cached to
disk on first use, and prefetched during enrichment so the overlay never waits on
the API while a card slides into frame.

Catalog images are thumbnail-sized — around 180×250, with no size parameter on
the endpoint. The overlay draws its hero card at roughly 230px wide, so the art
is upscaled slightly and looks a touch soft up close. It holds up fine at stream
bitrates; if you scale the overlay up a lot, that is the limit you will hit.
