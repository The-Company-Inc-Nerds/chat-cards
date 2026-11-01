# chat-cards

A local web app for opening trading-card packs on stream. A producer scans each
revealed card with an iPhone; the card is identified and priced through the
[CardSight AI](https://cardsight.ai) API, and the result lands on a Twitch
overlay in about two seconds.

It exists for a giveaway bit: during intermissions a wheel picks a follower, a
pack is opened for them on air, and the overlay shows what was pulled and what
it is worth while they decide whether to have the cards mailed, donated to a
shop's free pile, passed to the next chatter, or destroyed.

## The two surfaces

| Page | Who opens it | What it does |
|---|---|---|
| `/scan` | the producer, on a phone | camera, pack controls, and a tappable list for fixing a bad match |
| `/overlay` | OBS, as a browser source | running totals and the card reel, on a transparent background |

Both stay in step over a WebSocket. The overlay redraws from a full snapshot on
connect, so refreshing the browser source mid-pack loses nothing.

## Where to start

- [Setup](guides/setup.md) — get a key, run it, point the phone and OBS at it.
- [Stream-night runbook](guides/runbook.md) — the loop you actually follow on air.
- [The multichat giveaway loop](guides/giveaway-loop.md) — wiring draws to packs and packs back to the ledger.
- [Pricing a recorded opening](guides/video-pricing.md) — video in, per-card timestamps, prices and a running total out (CSV/SRT) for editing shorts.
- [NixOS module](reference/nixos-module.md) — deploying it as a service.
- [HTTP API](reference/http-api.md) — every endpoint.
- [How prices are calculated](reference/pricing.md) — and why they are what they are.
- [Development workflow](development/workflow.md) — signed commits and the wiki.
