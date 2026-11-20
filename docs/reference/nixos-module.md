# NixOS module

## Using it

```nix
{
  inputs.chat-cards.url = "github:you/chat-cards";

  outputs = { nixpkgs, chat-cards, ... }: {
    nixosConfigurations.studio = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        chat-cards.nixosModules.default
        {
          services.chat-cards = {
            enable = true;
            apiKeyFile = "/run/secrets/cardsight-api-key";
            openFirewall = true;   # so the producer's phone can reach it
          };
        }
      ];
    };
  };
}
```

That is the whole deployment. The scanner is at `http://<host>:8787/scan` and the
overlay at `http://<host>:8787/overlay`.

## Secrets

`apiKeyFile`, `tokenFile`, `pricechartingTokenFile` and `report.tokenFile` take
**paths, not values**. They are handed to the unit through systemd's
`LoadCredential=`, which copies the contents into a private tmpfs the service
reads at startup.

Never write the key inline in a Nix expression. Everything in a Nix expression
ends up in `/nix/store`, which is world-readable.

Works with agenix, sops-nix, or a plain root-owned file.

`LoadCredential=` is a snapshot taken at start, which has two consequences on a
host whose secrets arrive after boot:

- A configured file that does not exist yet fails the start before the app runs
  (`status=243/CREDENTIALS`). The unit [keeps retrying](#restarts) and comes up
  by itself once the file is there.
- A file that changes while the service runs is not picked up. Restart the unit
  from whatever writes the secret.

## Options

| Option | Default | Notes |
|---|---|---|
| `enable` | `false` | |
| `package` | built from this flake | |
| `address` | `"0.0.0.0"` | `"127.0.0.1"` when behind a proxy or `tailscale serve` |
| `port` | `8787` | |
| `openFirewall` | `false` | needed for the phone on the LAN |
| `apiKeyFile` | `null` | required unless `mock` |
| `tokenFile` | `null` | shared token for `/scan`, `/overlay` and the API |
| `pricechartingTokenFile` | `null` | PriceCharting API token; makes it the primary price source |
| `report.url` | `null` | multichat `/api/turn-report` URL; see [the giveaway loop](../guides/giveaway-loop.md) |
| `report.tokenFile` | `null` | bearer multichat requires there; unnecessary over loopback |
| `segment` | `"pokemon"` | name, shortname or UUID; `null` to auto-detect |
| `defaultPackSize` | `5` | what the scanner offers |
| `pricing.period` | `"90d"` | see [pricing](pricing.md) |
| `pricing.listingType` | `"auction"` | `auction` / `fixed` / `both` |
| `pricing.limit` | `50` | records per card |
| `pricing.statistic` | `"median"` | `median` / `mean` / `latest` |
| `pricing.fallbacks` | `true` | estimate when there are no comps, instead of showing nothing |
| `pricing.bulkValue` | `0.10` | value for a card with no evidence anywhere; `0` shows $0.00 |
| `rateLimit` | `3.0` | max API requests/second; the service allows 4 |
| `autoDeleteUnmatched` | `true` | drop scans the API could not identify |
| `mock` | `false` | canned data, no key, nothing billed |
| `tls.certFile` / `tls.keyFile` | `null` | serve HTTPS; enables the live camera preview on iOS |
| `extraArgs` | `[]` | appended to the command line verbatim |

## Reporting to multichat

`report.url` closes the giveaway loop: multichat draws a winner and opens a pack
here, and every pack is POSTed back as its cards are scanned and priced. Both
services usually share a box, so both calls stay on loopback and neither needs a
token — multichat requires its `integrations.callbackToken` only from a
non-loopback peer.

Neither unit needs ordering against the other. chat-cards resends the whole
session when it starts and whenever multichat comes back from being away, so
whichever of the two starts first, restarts or crashes, the ledger catches up.

```nix
services.chat-cards = {
  enable = true;
  port = 8787;
  report.url = "http://127.0.0.1:8081/api/turn-report";
};

services.multichat = {
  enable = true;
  port = 8081;
  giveaway = { enable = true; channel = "yourchannel"; };
  integrations.subscribers = [{
    name = "chat-cards";
    adapter = "chat-cards";
    baseUrl = "http://127.0.0.1:8787";
    packSize = 5;
  }];
};
```

Setting `report.tokenFile` without `report.url` fails the build — the token would
have nowhere to go. See [the giveaway loop](../guides/giveaway-loop.md) for the
whole picture.

## HTTPS and the iPhone camera

iOS Safari only grants an in-page camera preview on a secure context. Without
HTTPS the scanner still works — it falls back to the system camera sheet, which
costs one extra tap per card and equally never touches the camera roll.

If you want the live preview, either set `tls.certFile`/`tls.keyFile`, or leave
the service on `127.0.0.1` and front it with `tailscale serve`, which gets you a
real certificate and no warning to tap through.

## Start-up

The unit does not wait for the network. It is ordered after `network.target`
only — not `network-online.target`, which on a host that boots during an outage
is not reached until its wait-online unit gives up, a minute later with
NetworkManager. The service binds its port first and verifies the CardSight key
and PriceCharting token in the background, so the overlay, the scanner page and
`POST /api/pack` are up within a second of the start with or without an
uplink; `/healthz` reports `"checks": "pending"` until the APIs have answered
(see [setup](../guides/setup.md#2-run-it)).

The defaults bind `0.0.0.0`, which needs no interface to be up. With `address`
set to one particular LAN address the first starts can fail until that address
exists; the unit [keeps retrying](#restarts) and comes up once it does.

## Restarts

The unit is meant to stay up for as long as the host does:

| Setting | Value | Why |
|---|---|---|
| `StartLimitIntervalSec` | `0` | never parked in `failed` by a burst of starts |
| `Restart` | `always` | comes back after a clean exit too, not only a crash |
| `RestartSec` | `2` | first retry after two seconds |
| `RestartSteps` / `RestartMaxDelaySec` | `5` / `10` | then backs off to one retry every 10 s |

The start limit matters more than it looks. systemd's default allows five
starts in ten seconds, and a unit retrying every two seconds sits exactly on
that edge: one extra start job in the window — `systemctl restart`, a
`nixos-rebuild switch`, a secrets unit bouncing its consumers — is a sixth
start, and the unit is parked in `failed` where `Restart=` no longer applies.

The back-off keeps a start that cannot succeed yet (a credential file that has
not arrived) from logging a failure every two seconds indefinitely: the gaps
grow 2 s, 2.8 s, 3.8 s, 5.2 s, 7.2 s and then stay at 10 s. The price is that
such a unit can take up to 10 s to notice the file; a unit that writes the
secret and then restarts chat-cards gets it immediately.

systemd counts automatic restarts since the unit was last started by a job —
`systemctl restart`, a rebuild, another unit — and nothing else resets the
count, not even hours of running well. So a crash is back in two seconds, but
after five automatic restarts in one run of the unit, the next crash waits the
full 10 s, and for that long there is no overlay and no pack endpoint. That is
why the ceiling is no higher. Both settings are `mkDefault`, so a host that
would rather have a quieter log than a shorter gap can choose its own pace:

```nix
systemd.services.chat-cards.serviceConfig.RestartMaxDelaySec = 30;
```

## State

Session state, the producer's captured photos and the catalog-image cache live in
`/var/lib/chat-cards`, created by `StateDirectory=` with mode `0700`.

| Path | What | Written |
|---|---|---|
| `session.json` | packs and cards | whole, on every change |
| `captures/<uid>.bin` | the producer's photo of each scan | once |
| `card-images/<card_id>.<ext>` | catalog art, fetched once per card | once |

Every file is written to a temporary name, forced to disk and renamed into
place, so neither a restart mid-pack nor a power cut leaves a half-written one.
A cached image that is somehow empty is treated as missing and fetched again.

If `session.json` cannot be read at start — truncated, empty, not a session —
the service starts with an empty session rather than refusing to come up, and
moves the file aside as `session.json.corrupt-<UTC time>` so the first change
does not write over it. Nothing removes those files; delete them once you have
looked.

A card that was mid-lookup when the service stopped is settled after the next
start instead of staying a placeholder. If it had already been identified it
keeps its identity and its price is looked up again — once one of the APIs
has answered the start-up check, so that a box that boots before its uplink
does not record "price unavailable" for it. If not, it is treated like any other
failed scan — dropped, or kept and flagged when `autoDeleteUnmatched = false` —
and **not** re-submitted: identification is the billed call, and a restart
should never spend one by itself. That happens five seconds after the start,
when the scanner page has reconnected and can tell the producer to scan the
card again.

`captures/` and `card-images/` are never pruned, and `POST /api/session/reset`
leaves them alone. A scan is a few hundred kilobytes, so growth is slow — tens
of megabytes for a long night — but nothing reclaims it: on a small disk, clear
`captures/` by hand now and then. Photos of cards still in the session are the
ones the overlay may be showing.

## Hardening

The unit runs as a `DynamicUser` with `ProtectSystem=strict`, an empty capability
bounding set, `MemoryDenyWriteExecute`, `ProtectProc=invisible`,
`SystemCallFilter=@system-service ~@privileged @resources`, and address families
restricted to `AF_INET`/`AF_INET6`/`AF_UNIX`. It needs to reach exactly one HTTPS
API and write exactly one directory.

`nix flake check` boots the module in a VM and asserts it starts under all of
that, serves both pages, keeps its state across `systemctl restart`, survives a
burst of restarts without being parked, comes back after a clean exit, and
catches multichat up on a pack it missed while it was down. It also runs the
unit tests.
