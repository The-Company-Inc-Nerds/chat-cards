"""Entry point: parse configuration and run the server."""

from __future__ import annotations

import logging

from aiohttp import web

from .config import Config, config_from_args
from .server import create_app, ssl_context


def _describe(config: Config) -> None:
    scheme = "https" if config.tls_cert and config.tls_key else "http"
    host = "127.0.0.1" if config.host in ("0.0.0.0", "::") else config.host
    log.info("scanner  %s://%s:%d/scan", scheme, host, config.port)
    log.info("overlay  %s://%s:%d/overlay", scheme, host, config.port)
    if config.mock:
        log.info("MOCK MODE — canned responses, no API calls, prices are not real")
    elif not config.api_key:
        log.warning("No API key: scans will fail. Pass --api-key-file, or --mock to rehearse.")
    if config.token:
        log.info("Token required — append ?token=… to both URLs")


log = logging.getLogger("chat_cards")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = config_from_args()
    log.info("state    %s", config.state_dir)
    _describe(config)

    web.run_app(
        create_app(config),
        host=config.host,
        port=config.port,
        ssl_context=ssl_context(config),
        print=None,
    )


if __name__ == "__main__":
    main()
