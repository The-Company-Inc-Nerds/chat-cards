"""Runtime configuration: CLI flags, environment, and systemd credentials."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8787
DEFAULT_PACK_SIZE = 5

# CardSight's pricing endpoint hands back raw sale records, so the "market price"
# is ours to compute. See PricingConfig.statistic.
#
# A year, not a quarter: outside the chase cards, singles sell infrequently
# enough that a 90-day window leaves a lot of the pack unpriced. The trade is
# recency — a wider window lags a price that has actually moved.
DEFAULT_PERIOD = "1y"
DEFAULT_LISTING_TYPE = "auction"
DEFAULT_PRICING_LIMIT = 50


def default_state_dir() -> Path:
    """Where session state lives when nothing else says otherwise.

    Deliberately NOT inside the repository. State here is a stream in progress —
    scanned cards, open packs — and a working tree is somewhere builds, cleanups
    and `rm -rf` happen. systemd's StateDirectory still takes precedence.
    """
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "chat-cards"


def _read_secret(path: str | os.PathLike[str] | None) -> str | None:
    """Read a secret from a file, tolerating the trailing newline every editor adds."""
    if not path:
        return None
    value = Path(path).read_text(encoding="utf-8").strip()
    return value or None


def _credential(name: str) -> str | None:
    """Read a systemd LoadCredential= secret, if this process was given one.

    Keeps the API key out of the Nix store: the unit passes a path, systemd copies
    the content into a private tmpfs, and we read it from there.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return None
    path = Path(directory) / name
    return _read_secret(path) if path.exists() else None


@dataclass(frozen=True)
class PricingConfig:
    """How a pile of completed-sale records collapses into one number.

    The defaults deliberately ask for *completed ungraded auction sales*: an auction
    row is a price someone actually paid, whereas a "fixed" row is only what a seller
    is asking. A pack-fresh card is ungraded, so graded comps would be answering a
    different question.
    """

    period: str = DEFAULT_PERIOD
    listing_type: str = DEFAULT_LISTING_TYPE
    limit: int = DEFAULT_PRICING_LIMIT
    statistic: str = "median"
    # When the preferred query finds nothing, fall back through weaker evidence
    # (older sales, then asking prices, then active listings) and mark the result
    # as an estimate. Disable to show a dash instead of a soft number.
    fallbacks: bool = True
    # Last resort for a card with no evidence anywhere. Japanese bulk commons
    # genuinely have no individual market; a flat rate beats a blank.
    bulk_value: float = 0.10


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    api_key: str | None = None
    api_base: str = "https://api.cardsight.ai"
    # The API allows 4 requests/second; stay under it rather than relying on
    # retries to paper over a burst mid-reveal.
    rate_limit: float = 3.0
    # PriceCharting supplies the price; CardSight identifies the card. Without a
    # token the CardSight pricing ladder is used on its own.
    pricecharting_token: str | None = None
    # None means "let CardSight auto-detect the segment"; a name, shortname or UUID
    # pins it. Pinning is a real accuracy win when you know it is a Pokemon night.
    segment: str | None = "pokemon"
    token: str | None = None
    # When set, each pack's cards + running value are POSTed here (multichat's
    # /api/turn-report) as they change, so the giveaway ledger stays complete.
    # report_token is the bearer multichat requires (its integrations.callbackToken).
    report_url: str | None = None
    report_token: str | None = None
    state_dir: Path = field(default_factory=default_state_dir)
    default_pack_size: int = DEFAULT_PACK_SIZE
    pricing: PricingConfig = field(default_factory=PricingConfig)
    mock: bool = False
    # A scan the API cannot match is a re-scan, not something to fix by hand:
    # drop it so the reel does not carry a dead slot mid-reveal.
    auto_delete_unmatched: bool = True
    tls_cert: Path | None = None
    tls_key: Path | None = None

    @property
    def capture_dir(self) -> Path:
        return self.state_dir / "captures"

    @property
    def image_cache_dir(self) -> Path:
        return self.state_dir / "card-images"

    @property
    def session_file(self) -> Path:
        return self.state_dir / "session.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chat-cards",
        description="Scan trading cards from a phone, price them, and drive a Twitch overlay.",
    )
    parser.add_argument("--host", default=os.environ.get("CHAT_CARDS_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("CHAT_CARDS_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--api-key-file",
        default=os.environ.get("CHAT_CARDS_API_KEY_FILE"),
        help="File holding the CardSight API key. Preferred over --api-key.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CARDSIGHTAI_API_KEY"),
        help="CardSight API key. Avoid on a shared host: it is visible in the process list.",
    )
    parser.add_argument("--api-base", default=os.environ.get("CHAT_CARDS_API_BASE", "https://api.cardsight.ai"))
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=3.0,
        help="Max outbound API requests per second. The service allows 4; the "
        "default leaves headroom. 0 disables throttling.",
    )
    parser.add_argument(
        "--segment",
        default=os.environ.get("CHAT_CARDS_SEGMENT", "pokemon"),
        help='Card segment to pin, e.g. "pokemon". Use "auto" to let the API detect it.',
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("CHAT_CARDS_TOKEN_FILE"),
        help="File holding a shared token required to open /scan and mutate state.",
    )
    parser.add_argument(
        "--report-url",
        default=os.environ.get("CHAT_CARDS_REPORT_URL"),
        help="multichat /api/turn-report URL. When set, each pack's cards + value "
        "are POSTed there as they change, for the giveaway ledger.",
    )
    parser.add_argument(
        "--report-token-file",
        default=os.environ.get("CHAT_CARDS_REPORT_TOKEN_FILE"),
        help="File holding the bearer token multichat requires on /api/turn-report "
        "(its integrations.callbackToken). Or set CHAT_CARDS_REPORT_TOKEN.",
    )
    parser.add_argument(
        "--state-dir",
        default=(
            os.environ.get("STATE_DIRECTORY")
            or os.environ.get("CHAT_CARDS_STATE_DIR")
            or str(default_state_dir())
        ),
        help="Where session state, captures and the image cache live. "
        "Defaults outside the repo so a build or a cleanup cannot destroy a session.",
    )
    parser.add_argument("--pack-size", type=int, default=DEFAULT_PACK_SIZE)
    parser.add_argument("--pricing-period", default=DEFAULT_PERIOD)
    parser.add_argument(
        "--pricing-listing-type", choices=["auction", "fixed", "both"], default=DEFAULT_LISTING_TYPE
    )
    parser.add_argument("--pricing-limit", type=int, default=DEFAULT_PRICING_LIMIT)
    parser.add_argument("--pricing-statistic", choices=["median", "mean", "latest"], default="median")
    parser.add_argument(
        "--pricecharting-token-file",
        default=os.environ.get("CHAT_CARDS_PRICECHARTING_TOKEN_FILE"),
        help="File holding the PriceCharting API token. Its ungraded price becomes "
        "the primary source, with the CardSight ladder as fallback.",
    )
    parser.add_argument(
        "--bulk-value",
        type=float,
        default=0.10,
        help="Value for a card with no pricing evidence anywhere. 0 shows $0.00.",
    )
    parser.add_argument(
        "--no-pricing-fallbacks",
        action="store_true",
        help="Show a dash when there are no comps, instead of estimating from "
        "older sales, asking prices or active listings.",
    )
    parser.add_argument(
        "--keep-unmatched",
        action="store_true",
        help="Keep scans the API could not identify, instead of removing them "
        "automatically so they can be re-scanned.",
    )
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Serve canned identify/pricing responses. No API key, no billed calls — "
        "use it to rehearse the overlay.",
    )
    return parser


def config_from_args(argv: list[str] | None = None) -> Config:
    args = build_parser().parse_args(argv)

    # Precedence: systemd credential > --api-key-file > --api-key / env.
    api_key = _credential("api-key") or _read_secret(args.api_key_file) or args.api_key
    token = _credential("token") or _read_secret(args.token_file)
    pricecharting = _credential("pricecharting-token") or _read_secret(args.pricecharting_token_file)
    report_token = (
        _credential("report-token")
        or _read_secret(args.report_token_file)
        or os.environ.get("CHAT_CARDS_REPORT_TOKEN")
    )

    segment = None if args.segment in (None, "", "auto") else args.segment

    return Config(
        host=args.host,
        port=args.port,
        api_key=api_key,
        api_base=args.api_base.rstrip("/"),
        rate_limit=args.rate_limit,
        pricecharting_token=pricecharting,
        segment=segment,
        token=token,
        report_url=args.report_url or None,
        report_token=report_token,
        state_dir=Path(args.state_dir),
        default_pack_size=args.pack_size,
        pricing=PricingConfig(
            period=args.pricing_period,
            listing_type=args.pricing_listing_type,
            limit=args.pricing_limit,
            statistic=args.pricing_statistic,
            fallbacks=not args.no_pricing_fallbacks,
            bulk_value=max(0.0, args.bulk_value),
        ),
        mock=args.mock,
        auto_delete_unmatched=not args.keep_unmatched,
        tls_cert=Path(args.tls_cert) if args.tls_cert else None,
        tls_key=Path(args.tls_key) if args.tls_key else None,
    )
