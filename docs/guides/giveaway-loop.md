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

multichat upserts by `packId`, so sending a pack twice is harmless and the
latest report always wins. That is also why prices settle a second or two after
the cards appear: the first report goes out the moment a card is added, and the
priced version follows.

## Catching up

A report that fails is not lost. chat-cards resends **every pack in the
session**:

- **when it starts** — a new process cannot know which reports landed before it
  stopped, or whether multichat still has them;
- **as soon as a report lands after one has failed** — multichat is evidently
  back, so it is given everything it missed along with it;
- **on a timer while reports are failing** — 2 s after the first failure, then
  5 s, 15 s, 30 s, and once a minute for as long as multichat stays away, so a
  pack that was opened and finished during the outage is caught up without
  waiting for the next draw.

The resend is one POST at a time and gives way to live events, so the pack on
screen is never queued behind a long session. An attempt stops at its first
failed POST: a multichat that is down costs one refused connection per attempt,
however many packs are waiting.

Because the whole session is resent, clear both sides together. After
`multichat giveaway campaign-reset`, also `POST /api/session/reset` here;
otherwise the next catch-up hands multichat last campaign's packs again (they
show up in `multichat giveaway packs`, attached to no turn).

## When it breaks

Reporting never holds up the show. A multichat that is down, restarting or
misconfigured never interrupts a reveal — the failure is logged, the overlay
carries on, and the ledger is [caught up](#catching-up) once multichat answers.
So the failure mode you will actually see is a *silent* one: the overlay looks
perfect and multichat's ledger is empty, and stays empty because the cause is
configuration rather than an outage.

| Symptom | Cause |
|---|---|
| No pack ever opens on a draw | multichat has no subscriber, or its `baseUrl` is wrong |
| Packs open, ledger stays empty | `--report-url` unset here, or pointed at the wrong port |
| `HTTP 403` in the log | multichat wants its `callbackToken` — set `--report-token-file` |
| `HTTP 501` in the log | multichat has no giveaway enabled; `/api/turn-report` is off |
| Reports arrive with no `ref` | multichat sent no `ref` — check its subscriber `adapter` |

`journalctl -u chat-cards -f` shows a failed report with the status and the
first line of the response body. A successful start logs the target once:
`Reporting pack summaries to http://…`. During an outage the log says once that
multichat `is not taking pack reports … retrying` — the retries themselves are
quiet — and `Resent N pack summaries` marks the moment the ledger is whole
again.

## Tokens

Over loopback, neither side needs one. multichat accepts unauthenticated reports
from loopback and requires `integrations.callbackToken` only from anywhere else;
chat-cards on a trusted LAN typically runs without a `--token` of its own.

Split the two across machines and both halves need a token: set
`integrations.callbackToken` on multichat and pass it here with
`--report-token-file` (or `CHAT_CARDS_REPORT_TOKEN`), and set chat-cards'
`--token-file` and give the same value to multichat's subscriber `token`.
