# The multichat giveaway loop

chat-cards can run on its own — open a pack, scan the cards, watch the overlay.
This page is about the other mode: wiring it to
[multichat](https://github.com/The-Company-Inc-Nerds/multichat) so the whole bit
runs itself. Viewers type `!enter` in Twitch chat, a draw picks one of them, a
pack opens here under their name, and what was pulled — and what it was worth —
lands back on that winner's row in multichat's ledger.

## The loop

```
       multichat                                  chat-cards
 ┌───────────────────────┐                  ┌───────────────────────┐
 │ viewer types !enter   │                  │                       │
 │ operator hits Draw    │                  │                       │
 │                       │  POST /api/pack  │                       │
 │ giveaway.turn.start ──┼─────────────────►│ pack opens for @winner│
 │   {winner, ref, size} │                  │ producer scans cards  │
 │                       │                  │ each card is priced   │
 │ turn ledger updated ◄─┼──────────────────┼─ POST /api/turn-report│
 └───────────────────────┘ {packId, ref,    └───────────────────────┘
                            cards[], total}
```

Two independent halves, and you need both:

| Direction | Configured in | Setting |
|---|---|---|
| multichat → chat-cards | multichat | `integrations.subscribers[]` with `adapter = "chat-cards"` |
| chat-cards → multichat | chat-cards | `--report-url` / `services.chat-cards.report.url` |

`ref` is the thread between them. multichat mints it for the turn, sends it with
the pack request, chat-cards stores it on the pack, and every report carries it
back — that is how a report finds its turn. It is never shown on the overlay.

## Running it

From the dev shell, in mock mode, against a multichat on the same box:

```sh
serve --report-url http://127.0.0.1:8081/api/turn-report
```

Mock mode reports exactly like the real thing, so the entire loop can be
rehearsed offline with no key and nothing billed. On the multichat side, enable
the giveaway and add the subscriber:

```json
{
  "giveaway": { "enabled": true, "channel": "yourchannel" },
  "integrations": {
    "subscribers": [
      {
        "name": "chat-cards",
        "adapter": "chat-cards",
        "baseUrl": "http://127.0.0.1:8787",
        "events": ["giveaway.turn.start"],
        "packSize": 5
      }
    ]
  }
}
```

The NixOS equivalent is in the [NixOS module](../reference/nixos-module.md#reporting-to-multichat)
page.

## What gets reported

A report is sent on every event that can change a pack — opened, closed, a card
added, identified, priced, corrected or removed. Each one carries the pack's
**full current state**, not a delta:

```json
{
  "packId": "…", "ref": "…", "winner": "@winner", "label": null,
  "index": 3, "size": 5, "openedAt": 1730000000000, "closedAt": null,
  "totalValue": 41.75, "cardCount": 5,
  "cards": [
    { "name": "Charizard", "number": "4/102", "set": "Base Set",
      "rarity": "Rare Holo", "value": 38.00, "image": "https://…" }
  ]
}
```

multichat upserts by `packId`, so a dropped POST needs no retry — the next
update reconciles it. That is also why prices settle a second or two after the
cards appear: the first report goes out the moment a card is added, and the
priced version follows.

## When it breaks

Reporting is deliberately fire-and-forget. A multichat that is down, restarting
or misconfigured never interrupts a reveal — the failure is logged and the
overlay carries on. So the failure mode you will actually see is a *silent* one:
the overlay looks perfect and multichat's ledger is empty.

| Symptom | Cause |
|---|---|
| No pack ever opens on a draw | multichat has no subscriber, or its `baseUrl` is wrong |
| Packs open, ledger stays empty | `--report-url` unset here, or pointed at the wrong port |
| `HTTP 403` in the log | multichat wants its `callbackToken` — set `--report-token-file` |
| `HTTP 501` in the log | multichat has no giveaway enabled; `/api/turn-report` is off |
| Reports arrive with no `ref` | multichat sent no `ref` — check its subscriber `adapter` |

`journalctl -u chat-cards -f` shows every failed POST with the status and the
first line of the response body. A successful start logs the target once:
`Reporting pack summaries to http://…`.

## Tokens

Over loopback, neither side needs one. multichat accepts unauthenticated reports
from loopback and requires `integrations.callbackToken` only from anywhere else;
chat-cards on a trusted LAN typically runs without a `--token` of its own.

Split the two across machines and both halves need a token: set
`integrations.callbackToken` on multichat and pass it here with
`--report-token-file` (or `CHAT_CARDS_REPORT_TOKEN`), and set chat-cards'
`--token-file` and give the same value to multichat's subscriber `token`.
