# Setup

## 1. Get a CardSight API key

Sign up at [cardsight.ai](https://cardsight.ai) and copy the key. Write it to a
file that only root can read — never into a Nix expression, since anything in a
Nix expression lands in the world-readable store:

```bash
install -m 600 /dev/null /run/secrets/cardsight-api-key
printf '%s' 'YOUR_KEY' > /run/secrets/cardsight-api-key
```

## 2. Run it

The dev shell provides `serve`, which runs the app straight from `./src` with no
build step and prints the URLs — including the LAN address to open on the phone.

```bash
nix develop

serve                      # mock mode: canned data, no key, nothing billed
serve --key YOUR_KEY       # live
```

```
chat-cards — live, key from the command line (visible in ps)
  scanner (phone)  http://10.53.90.144:8787/scan
  overlay (OBS)    http://localhost:8787/overlay
```

`serve` hands the key to the server as a 0600 temp file, so the long-running
server process has a clean `ps` entry. It does not hide the key altogether — a
key passed with `--key` is in your shell history and in `serve`'s own argv for
as long as it runs. On your own machine that is fine; where it is not:

```bash
serve --key-file /run/secrets/cardsight-api-key
pass show cardsight/api-key | serve --key-stdin
CHAT_CARDS_API_KEY=… serve
```

Any other argument is passed straight through, so `serve --key KEY --port 9000
--segment auto` and `serve --pricing-statistic latest` work as you would expect.
`serve --help` lists everything.

With no key and no environment variable, `serve` starts in mock mode rather than
launching something that would fail on the first scan.

Session state lives in `~/.local/state/chat-cards` (or `$XDG_STATE_HOME`), not in
the repo — a working tree is where builds and cleanups happen, and a session in
progress should not be sitting in the blast radius. `--state-dir` overrides it,
and under systemd the module's `StateDirectory` wins.

For a permanent install see the [NixOS module](../reference/nixos-module.md).

`--segment pokemon` pins identification to the Pokémon catalog. Leave it as
`auto` only if a night mixes sports and TCG cards — pinning is meaningfully more
accurate.

Check it came up, and that the key is good:

```bash
curl -s http://localhost:8787/healthz
# {"ok": true, "mock": false, "cardsight_key": true, "cardsight_auth": true}
```

`cardsight_auth` is three-valued: `true` (key verified), `false` (the API
actively rejected it — fix that before going live, every scan will fail), and
`null` (the check could not be made, e.g. the network was down at startup, which
is not itself a problem).

## 1b. Get a PriceCharting token

PriceCharting supplies the prices; CardSight identifies the card and supplies the
artwork. The API is a paid feature — the token is 40 characters, from the
Subscription page's "API/Download" button. Store it the same way:

```bash
install -m 600 /dev/null /run/secrets/pricecharting-token
printf '%s' 'YOUR_TOKEN' > /run/secrets/pricecharting-token
```

Then pass it alongside the API key:

```bash
serve --key-file /run/secrets/cardsight-api-key \
      --pricecharting-token-file /run/secrets/pricecharting-token
```

Without it the app still runs, using CardSight's completed-sales ladder alone —
which has no data at all for Japanese prints. `/healthz` reports
`"pricecharting": true` once the token returns price data, `false` if the
subscription does not cover it, and `null` when no token is set.

## 2b. Preflight the key

`tools/live_check.py` walks the whole chain the app depends on — key, catalog
search, card art, identification, pricing — and prints what it found at each
step. Run it after rotating a key, and before a stream if you want certainty.

```bash
nix develop
python3 tools/live_check.py --api-key-file /run/secrets/cardsight-api-key
```

```
[  ok  ] API key accepted
[  ok  ] search returned 5 cards for 'charizard ex'
[  ok  ] catalog art fetched (121387 bytes, image/png)
[  ok  ] identified: Charizard ex — 2023 Obsidian Flames #125
         confidence High, rarity 'Double Rare'
[  ok  ] price $5.00  (median of 21 sales)
         range $3.50–$8.50, last sale 2026-08-23T05:33:00Z

All stages passed. The key and the whole chain are good.
```

With no `--image` it identifies a card using that card's *own* catalog art,
which is a clean round-trip test needing no physical card. To test a real photo
instead — the thing that actually matters, since a phone photo has glare and
perspective a scan of clean artwork does not:

```bash
python3 tools/live_check.py --api-key-file KEY --image ~/card.jpg
```

`--diagnose --card-id UUID` probes every pricing rung for one card and shows
which have data — the way to tell a too-narrow window from a card that simply is
not linked to its catalog entry.

`--compare` prices several cards across several windows and reports how many got
a real comp in each — the way to settle `pricing.period` on your own cards rather
than by feel. See [how prices are calculated](../reference/pricing.md).

Identification is the only billed stage of a normal run. `--skip-identify` checks
the key, search and pricing without spending a call. Useful flags: `--query` to pick a
different test card, `--card-id` to target an exact one, and
`--period` / `--listing-type` / `--statistic` to see how a pricing change would
move the number before you commit it to the module.

An `API key REJECTED` line means a 401 — the key is wrong. Anything else that
fails points at that specific stage.

## 3. Point the phone at it

Open `http://<machine-ip>:8787/scan` in Safari and add it to the home screen so
it opens without browser chrome.

The scanner has two capture paths and picks one automatically:

- **Live preview**, an in-page viewfinder with a tap-to-capture shutter. iOS only
  allows this on a *secure context*, so it needs HTTPS (or localhost).
- **System camera**, the iOS camera sheet, used whenever the live preview is not
  available. Works fine over plain `http://192.168.x.x`.

Neither writes to the camera roll — in both cases the frame goes straight into
an upload. The system-camera path costs one extra tap per card.

To get the live preview, serve the app over HTTPS. The easiest route is
Tailscale:

```bash
tailscale serve --bg 8787
# -> https://your-box.your-tailnet.ts.net
```

That gives a real certificate with no warning to tap through, and the phone can
scan from anywhere. Alternatively set `tls.certFile`/`tls.keyFile` on the module.

## 4. Add the overlay to OBS

Add a **Browser** source:

- URL: `http://localhost:8787/overlay`
- Width 1920, height 1080
- Leave *Shutdown source when not visible* **off**, so it stays connected

The page is transparent, so it composites straight over your scene. Handy query
parameters:

| Parameter | Effect |
|---|---|
| `?bg=1` | opaque background, for checking the layout in a normal browser |
| `?scale=1.15` | scale the whole overlay |
| `?history=8` | how many cards stay on the reel when no pack is open |
| `?chase=50` | dollar value at which a card gets the gold treatment (default 25) |

## 5. Optional: require a token

If the app is reachable beyond your LAN, put a shared secret in a file and set
`tokenFile`. Then open both pages once with `?token=…` appended; the token is
stored in a cookie afterwards.
