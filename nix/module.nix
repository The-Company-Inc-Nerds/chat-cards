{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.chat-cards;
  inherit (lib) mkEnableOption mkOption mkIf types optional optionals literalExpression;
in {
  options.services.chat-cards = {
    enable = mkEnableOption "the chat-cards scanner and Twitch overlay";

    package = mkOption {
      type = types.package;
      description = "The chat-cards package to run.";
    };

    address = mkOption {
      type = types.str;
      default = "0.0.0.0";
      description = ''
        Address to bind. The default accepts connections from the LAN so the
        producer's phone can reach it; use "127.0.0.1" when fronting it with a
        reverse proxy or `tailscale serve`.
      '';
    };

    port = mkOption {
      type = types.port;
      default = 8787;
      description = "TCP port for the scanner, overlay and API.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = "Open {option}`services.chat-cards.port` in the firewall.";
    };

    apiKeyFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/cardsight-api-key";
      description = ''
        File containing the CardSight AI API key. Passed to the unit with
        systemd's `LoadCredential=`, so the key never enters the Nix store and is
        not readable by other users. Required unless {option}`mock` is enabled.
      '';
    };

    pricechartingTokenFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = "/run/secrets/pricecharting-token";
      description = ''
        File containing the PriceCharting API token (40 characters, from the
        Subscription page's "API/Download" button). Passed through systemd's
        `LoadCredential=`, so it never enters the Nix store.

        PriceCharting becomes the primary price source when set: its ungraded
        price is used, and CardSight's completed-sales ladder becomes the
        fallback. It is the only source verified to carry Japanese prints, which
        CardSight has no comps for at all. Without a token, CardSight pricing is
        used on its own.
      '';
    };

    tokenFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = ''
        Optional file containing a shared token. When set, `/scan`, `/overlay`
        and the API all require it — append `?token=…` to the URL once and the
        page stores it in a cookie. Leave unset on a trusted LAN.
      '';
    };

    report = {
      url = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "http://127.0.0.1:8081/api/turn-report";
        description = ''
          multichat's `/api/turn-report` URL. When set, every pack this service
          opens is POSTed there as its cards are scanned and priced, so
          multichat's per-winner giveaway ledger stays complete. Each POST
          carries the pack's full current state, so a dropped update is
          reconciled by the next one and a multichat that is down never
          interrupts the reveal.

          Pair it with multichat's own `integrations.subscribers` entry pointing
          back at this service — that is the half that opens the pack in the
          first place.
        '';
      };

      tokenFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        example = "/run/secrets/multichat-callback-token";
        description = ''
          File containing the bearer token multichat requires on
          `/api/turn-report` (its `integrations.callbackToken`). Passed through
          systemd's `LoadCredential=`, so it never enters the Nix store.

          Only needed when multichat is on another host: multichat accepts
          unauthenticated reports over loopback, so two services on the same box
          need no token at all.
        '';
      };
    };

    segment = mkOption {
      type = types.nullOr types.str;
      default = "pokemon";
      example = "basketball";
      description = ''
        Card segment to pin identification to, by name, shortname or UUID.
        Pinning is noticeably more accurate than letting the API guess. Set to
        `null` to auto-detect, which is what you want for a mixed box.
      '';
    };

    rateLimit = mkOption {
      type = types.numbers.nonnegative;
      default = 3.0;
      description = ''
        Maximum outbound API requests per second. CardSight allows 4 and answers
        a burst with HTTP 429; the pricing ladder can fire five calls for a
        single card, and several cards enrich at once when a pack is opened
        quickly. The default leaves headroom. 0 disables throttling.
      '';
    };

    defaultPackSize = mkOption {
      type = types.ints.positive;
      default = 5;
      description = "Cards per pack the scanner offers by default.";
    };

    pricing = {
      period = mkOption {
        type = types.str;
        default = "1y";
        example = "90d";
        description = ''
          Lookback window for comparable sales: 7d, 2w, 3m, 1y, 365d, all.

          A year by default, because outside the chase cards singles sell
          infrequently enough that a 90-day window leaves much of a pack
          unpriced. Narrow it if you would rather have fewer, fresher comps —
          a wide window lags a price that has genuinely moved.
        '';
      };

      listingType = mkOption {
        type = types.enum ["auction" "fixed" "both"];
        default = "auction";
        description = ''
          Which listings count. `auction` uses completed sales — prices someone
          actually paid. `fixed` uses Buy It Now asking prices, which run high
          because an unsold listing can ask anything.
        '';
      };

      limit = mkOption {
        type = types.ints.positive;
        default = 50;
        description = "Maximum sale records to pull per card.";
      };

      fallbacks = mkOption {
        type = types.bool;
        default = true;
        description = ''
          When there are no completed sales for a card, estimate from weaker
          evidence instead of showing nothing: older sales, then Buy It Now
          asking prices, then active listings. Such values are marked as
          estimates and the overlay shows them with a `~`. Set to false to show
          a dash rather than a soft number.
        '';
      };

      bulkValue = mkOption {
        type = types.numbers.nonnegative;
        default = 0.10;
        description = ''
          Value for a card with no pricing evidence anywhere. Japanese bulk
          commons genuinely have no individual market — nobody sells them
          singly — so a flat bulk rate is both more honest and more useful than
          a blank. Set to 0 to show $0.00 instead.
        '';
      };

      statistic = mkOption {
        type = types.enum ["median" "mean" "latest"];
        default = "median";
        description = ''
          How the records collapse into one price. `median` shrugs off the
          bulk-lot listings and one-off outliers that pollute card sales data.
        '';
      };
    };

    autoDeleteUnmatched = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Drop scans the API could not identify instead of leaving them on the
        reel. A failed scan is usually a re-scan rather than something to fix by
        hand, and the phone says so when one is dropped. Set to false to keep
        them and correct them through the scanner instead.
      '';
    };

    mock = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Serve canned identification and pricing data instead of calling the API.
        No key needed and nothing is billed — useful for rehearsing the overlay
        against your scene layout before going live.
      '';
    };

    tls = {
      certFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = ''
          TLS certificate. Serving HTTPS makes the page a secure context, which
          is what iOS Safari requires before it will grant an in-page camera
          preview. Without it the scanner falls back to the system camera sheet,
          which works fine over plain HTTP.
        '';
      };

      keyFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = "TLS private key matching {option}`tls.certFile`.";
      };
    };

    extraArgs = mkOption {
      type = types.listOf types.str;
      default = [];
      example = literalExpression ''["--pricing-limit" "100"]'';
      description = "Extra command-line arguments appended verbatim.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.mock || cfg.apiKeyFile != null;
        message = "services.chat-cards: set apiKeyFile, or enable mock to run without the API.";
      }
      {
        assertion = (cfg.tls.certFile == null) == (cfg.tls.keyFile == null);
        message = "services.chat-cards: tls.certFile and tls.keyFile must be set together.";
      }
      {
        assertion = cfg.report.tokenFile == null || cfg.report.url != null;
        message = "services.chat-cards: report.tokenFile is set but report.url is not — the token has nowhere to be sent.";
      }
    ];

    services.chat-cards.package = lib.mkDefault (pkgs.callPackage ./package.nix {});

    networking.firewall.allowedTCPPorts = optional cfg.openFirewall cfg.port;

    systemd.services.chat-cards = {
      description = "chat-cards scanner and Twitch overlay";
      wantedBy = ["multi-user.target"];
      after = ["network-online.target"];
      wants = ["network-online.target"];

      serviceConfig = {
        ExecStart = lib.escapeShellArgs (
          [
            (lib.getExe cfg.package)
            "--host"
            cfg.address
            "--port"
            (toString cfg.port)
            "--pack-size"
            (toString cfg.defaultPackSize)
            "--rate-limit"
            (toString cfg.rateLimit)
            "--pricing-period"
            cfg.pricing.period
            "--pricing-listing-type"
            cfg.pricing.listingType
            "--pricing-limit"
            (toString cfg.pricing.limit)
            "--pricing-statistic"
            cfg.pricing.statistic
            "--segment"
            (
              if cfg.segment == null
              then "auto"
              else cfg.segment
            )
          ]
          ++ optional (!cfg.pricing.fallbacks) "--no-pricing-fallbacks"
          ++ ["--bulk-value" (toString cfg.pricing.bulkValue)]
          ++ optionals (cfg.report.url != null) ["--report-url" cfg.report.url]
          ++ optional (!cfg.autoDeleteUnmatched) "--keep-unmatched"
          ++ optional cfg.mock "--mock"
          ++ optionals (cfg.tls.certFile != null) ["--tls-cert" "${cfg.tls.certFile}" "--tls-key" "${cfg.tls.keyFile}"]
          ++ cfg.extraArgs
        );

        # Secrets arrive through systemd's credential store, not the store or the
        # environment; the app reads them from $CREDENTIALS_DIRECTORY.
        LoadCredential =
          optional (cfg.apiKeyFile != null) "api-key:${cfg.apiKeyFile}"
          ++ optional (cfg.tokenFile != null) "token:${cfg.tokenFile}"
          ++ optional (cfg.pricechartingTokenFile != null) "pricecharting-token:${cfg.pricechartingTokenFile}"
          ++ optional (cfg.report.tokenFile != null) "report-token:${cfg.report.tokenFile}";

        # $STATE_DIRECTORY is picked up by the app for session state, captured
        # photos and the catalog-image cache.
        StateDirectory = "chat-cards";
        StateDirectoryMode = "0700";

        DynamicUser = true;
        Restart = "on-failure";
        RestartSec = 2;

        # Hardening. This service talks to one HTTPS API and writes one directory.
        AmbientCapabilities = [];
        CapabilityBoundingSet = [];
        DevicePolicy = "closed";
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateTmp = true;
        ProcSubset = "pid";
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        ProtectSystem = "strict";
        RemoveIPC = true;
        RestrictAddressFamilies = ["AF_INET" "AF_INET6" "AF_UNIX"];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = ["@system-service" "~@privileged @resources"];
        UMask = "0077";
      };
    };
  };
}
