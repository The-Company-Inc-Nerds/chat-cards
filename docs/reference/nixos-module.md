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

## State

Session state, the producer's captured photos and the catalog-image cache live in
`/var/lib/chat-cards`, created by `StateDirectory=` with mode `0700`. State is
written atomically on every change, so a restart mid-pack loses nothing.

## Hardening

The unit runs as a `DynamicUser` with `ProtectSystem=strict`, an empty capability
bounding set, `MemoryDenyWriteExecute`, `ProtectProc=invisible`,
`SystemCallFilter=@system-service ~@privileged @resources`, and address families
restricted to `AF_INET`/`AF_INET6`/`AF_UNIX`. It needs to reach exactly one HTTPS
API and write exactly one directory.

`nix flake check` boots the module in a VM and asserts it starts under all of
that, serves both pages, and keeps its state across `systemctl restart`.
