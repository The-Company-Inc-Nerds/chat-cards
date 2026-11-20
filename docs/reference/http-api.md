# HTTP API

Everything is JSON except the scan upload and the image endpoints. If a token is
configured, pass it as `Authorization: Bearer <token>`, `?token=…`, or the
`cc_token` cookie.

## Pages

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/scan` | producer's scanner UI |
| `GET` | `/overlay` | OBS browser source |
| `GET` | `/healthz` | liveness plus credential status; never requires a token |

`/healthz` answers as soon as the server is listening, which is before either
upstream API has been asked anything:

```jsonc
{
  "ok": true,
  "mock": false,
  "cardsight_key": true,      // a key is configured
  "cardsight_auth": true,     // true verified | false rejected | null no verdict
  "pricecharting": null,      // true returns prices | false it does not | null no token, or no verdict
  "checks": "done"            // "pending" while a credential is still being verified
}
```

The two verdicts are filled in by a background check. A `null` next to
`"checks": "pending"` means the API has not been reached yet and is being asked
again (after 5 s, 15 s, a minute, then every five minutes); next to `"done"`
it means there was nothing to verify. Treat `null` as "unknown", never as
"bad" — only `false` is a rejection.

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
could not identify it — or because a restart cut the lookup short, in which
case the frame follows a few seconds after the restart, once the pages have
reconnected. The scanner turns it into a "scan it again" prompt;
without it a vanishing row would look like nothing happened. `card_failed` and
`card_removed` frames also carry the `pack_id` the card belonged to, since the
card itself is no longer in the snapshot.

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
