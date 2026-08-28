"""aiohttp application: scanner UI, overlay UI, JSON API, and the WebSocket hub."""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .cardsight import CardSightClient, CardSightError, MAX_UPLOAD_BYTES, Valuation
from .config import Config
from .mock import MockCardSightClient
from .pricecharting import PriceChartingClient
from .reporter import Reporter
from .state import (
    STATUS_ERROR,
    STATUS_IDENTIFYING,
    STATUS_PRICED,
    STATUS_UNMATCHED,
    Store,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_COOKIE = "cc_token"

# Catalog ids come from the API as UUIDs; anything else is not going near a path.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/heic": ".heic",
}
CONTENT_TYPES = {v: k for k, v in EXTENSIONS.items()}


def _safe_id(value: str) -> bool:
    return bool(SAFE_ID.match(value))


# --------------------------------------------------------------------------- auth


def _request_token(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return request.query.get("token") or request.cookies.get(TOKEN_COOKIE)


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    config: Config = request.app["config"]
    if not config.token or request.path == "/healthz":
        return await handler(request)

    if _request_token(request) != config.token:
        if request.method == "GET" and request.headers.get("Accept", "").startswith("text/html"):
            return web.Response(
                status=401,
                content_type="text/html",
                text=(
                    "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                    "<body style='font:16px system-ui;padding:2rem;background:#111;color:#eee'>"
                    "<h1>Token required</h1><p>Open this page with <code>?token=…</code>.</p>"
                ),
            )
        raise web.HTTPUnauthorized(text="missing or invalid token")
    return await handler(request)


def _with_token_cookie(request: web.Request, response: web.StreamResponse) -> web.StreamResponse:
    """Persist a ?token= so the page's own asset and API requests carry it too."""
    token = request.query.get("token")
    if token and token == request.app["config"].token:
        response.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="Lax", max_age=60 * 60 * 24 * 30)
    return response


# ---------------------------------------------------------------------- enrichment


def _cached_image(directory: Path, key: str) -> Path | None:
    return next(iter(sorted(directory.glob(f"{key}.*"))), None)


async def _cache_catalog_image(app: web.Application, card_id: str) -> bool:
    """Fetch and store the catalog art. Returns False when the card has none.

    Done ahead of the reveal so the overlay never waits on CardSight while a card
    is sliding into frame.
    """
    config: Config = app["config"]
    directory = config.image_cache_dir
    directory.mkdir(parents=True, exist_ok=True)

    if _cached_image(directory, card_id):
        return True

    try:
        data, content_type = await app["client"].card_image(card_id)
    except Exception as exc:
        log.info("No catalog image for %s: %s", card_id, exc)
        return False

    extension = EXTENSIONS.get(content_type.split(";")[0].strip(), ".jpg")
    (directory / f"{card_id}{extension}").write_bytes(data)
    return True


async def enrich_card(app: web.Application, uid: str, image: bytes, content_type: str) -> None:
    """Identify then price a scanned card, publishing after each stage.

    Two updates rather than one: the name and artwork land as soon as they are
    known, and the price follows a beat later. On stream that reads as a reveal
    rather than a spinner.
    """
    store: Store = app["store"]
    config: Config = app["config"]
    client = app["client"]

    async def failed(reason: str, status: str = STATUS_ERROR) -> None:
        if config.auto_delete_unmatched:
            await store.remove_card(uid, event="card_failed", reason=reason)
        else:
            await store.update_card(uid, status=status, error=reason)

    try:
        identification = await client.identify(image, content_type)
    except CardSightError as exc:
        await failed(str(exc))
        return
    except Exception as exc:  # network blips must not kill the task silently
        log.exception("identify failed for %s", uid)
        await failed(str(exc))
        return

    card = store.card(uid)
    if card is None:  # deleted while the lookup was in flight
        return

    fields: dict[str, Any] = {
        "card_id": identification.card_id,
        # overlay_name never returns CJK or Hangul, so the stream stays English
        # even when the pack did not.
        "name": identification.overlay_name if identification.exact else identification.display_name,
        "original_name": identification.name,
        "name_is_english": identification.name_is_english,
        "language": identification.language,
        "subtitle": identification.subtitle,
        "number": identification.number,
        "year": identification.year,
        "set_name": identification.set_name,
        "release_name": identification.release_name,
        "parallel_id": identification.parallel_id,
        "parallel_name": identification.parallel_name,
        "numbered_to": identification.numbered_to,
        "attributes": identification.attributes,
        "rarity": identification.rarity,
        "confidence": identification.confidence,
        "suggestions": identification.suggestions,
        "messages": identification.messages,
    }

    image_source = "capture" if card.has_capture else "none"
    if identification.exact and await _cache_catalog_image(app, identification.card_id):
        image_source = "catalog"

    if not identification.exact:
        detail = identification.messages[0] if identification.messages else "No catalog match"
        await failed(detail, STATUS_UNMATCHED)
        return

    await store.update_card(
        uid, event="card_identified", status=STATUS_IDENTIFYING, image_source=image_source, **fields
    )

    await price_card(app, uid, identification.card_id, identification.parallel_id)


async def price_card(app: web.Application, uid: str, card_id: str, parallel_id: str | None) -> None:
    """Value one card and publish the result.

    Memoised per (card, parallel) for the session: re-scanning the same card, or
    correcting two copies of it to the same match, bills one lookup.
    """
    store: Store = app["store"]
    config: Config = app["config"]
    cache: dict[tuple[str, str | None], Any] = app["price_cache"]
    key = (card_id, parallel_id)

    cached = cache.get(key)
    if cached is not None:
        await _publish_price(store, uid, cached)
        return

    # PriceCharting first: it is the source verified to carry the Japanese
    # prints CardSight has no comps for at all, and one source for price keeps a
    # pack total summing comparable numbers.
    card = store.card(uid)
    pricecharting = app.get("pricecharting")
    if pricecharting is not None and pricecharting.configured and card and card.name:
        found = await pricecharting.price(
            card.name, card.number, card.release_name, card.language, card.parallel_name
        )
        if found is not None:
            cache[key] = found
            await _publish_price(store, uid, found)
            return

    try:
        valuation = await app["client"].pricing(
            card_id,
            parallel_id,
            period=config.pricing.period,
            listing_type=config.pricing.listing_type,
            limit=config.pricing.limit,
            statistic=config.pricing.statistic,
            fallbacks=config.pricing.fallbacks,
        )
    except Exception as exc:
        log.warning("pricing failed for %s: %s", card_id, exc)
        await store.update_card(
            uid,
            event="card_priced",
            status=STATUS_PRICED,
            value=0.0,
            value_source="none",
            error=f"Pricing unavailable: {exc}",
        )
        return

    # Neither source found anything. Japanese bulk commons land here routinely —
    # nobody sells them individually, so a flat bulk rate is the honest answer.
    if valuation.basis == "none" and config.pricing.bulk_value > 0:
        valuation = Valuation(
            value=round(config.pricing.bulk_value, 2),
            statistic=valuation.statistic,
            period=valuation.period,
            listing_type=valuation.listing_type,
            basis="bulk",
            estimated=True,
            note="No price found anywhere — showing the bulk rate",
        )

    cache[key] = valuation
    await _publish_price(store, uid, valuation)


async def _publish_price(store: Store, uid: str, valuation: Valuation) -> None:
    await store.update_card(
        uid,
        event="card_priced",
        status=STATUS_PRICED,
        value=valuation.value,
        value_source="market" if valuation.basis != "none" else "none",
        pricing=valuation.as_dict(),
        error=None,
    )


# -------------------------------------------------------------------------- routes


async def index(request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound("/scan" + (f"?token={request.query['token']}" if "token" in request.query else ""))


def _page(name: str):
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-cache"})
        return _with_token_cookie(request, response)

    return handler


async def healthz(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "mock": request.app["config"].mock,
            "cardsight_key": bool(request.app["client"].configured),
            "cardsight_auth": request.app.get("auth_ok"),
            "pricecharting": request.app.get("pricecharting_ok"),
        }
    )


async def get_state(request: web.Request) -> web.Response:
    return web.json_response(request.app["store"].snapshot())


async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)

    store: Store = request.app["store"]
    queue = store.subscribe()
    # Snapshot first: an OBS source that reloads mid-pack redraws exactly as it was.
    await ws.send_json({"type": "state", "event": "hello", "state": store.snapshot()})

    async def pump() -> None:
        while True:
            message = await queue.get()
            await ws.send_json(message)

    pump_task = asyncio.create_task(pump())
    try:
        async for message in ws:
            if message.type == WSMsgType.ERROR:
                break
    finally:
        pump_task.cancel()
        store.unsubscribe(queue)
    return ws


async def post_scan(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    store: Store = request.app["store"]

    reader = await request.multipart()
    field = await reader.next()
    while field is not None and field.name != "image":
        field = await reader.next()
    if field is None:
        raise web.HTTPBadRequest(text="expected an 'image' field")

    content_type = (field.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await field.read_chunk()
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD_BYTES, actual_size=size)
        chunks.append(chunk)
    image = b"".join(chunks)
    if not image:
        raise web.HTTPBadRequest(text="empty image")

    card = await store.add_card()

    # Keep the producer's photo: it is the fallback artwork when the catalog has
    # none, and the visual record of what was actually scanned.
    config.capture_dir.mkdir(parents=True, exist_ok=True)
    (config.capture_dir / f"{card.uid}.bin").write_bytes(image)
    await store.update_card(card.uid, has_capture=True, capture_type=content_type)

    task = asyncio.create_task(enrich_card(request.app, card.uid, image, content_type))
    request.app["tasks"].add(task)
    task.add_done_callback(request.app["tasks"].discard)
    return web.json_response({"uid": card.uid, "seq": card.seq})


async def post_pack(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    body = await request.json()
    size = int(body.get("size") or config.default_pack_size)
    pack = await request.app["store"].open_pack(
        size=size,
        winner=(body.get("winner") or "").strip() or None,
        label=(body.get("label") or "").strip() or None,
        # Correlation id from the opener (multichat sends the giveaway winner's
        # user id / turn id) — echoed back in reports, never shown on stream.
        ref=(str(body.get("ref")) if body.get("ref") else "").strip() or None,
    )
    return web.json_response(pack.to_json())


async def post_pack_close(request: web.Request) -> web.Response:
    pack = await request.app["store"].close_pack()
    return web.json_response({"closed": pack.to_json() if pack else None})


async def patch_card(request: web.Request) -> web.Response:
    """Live correction: override the price, or swap in an alternate match."""
    store: Store = request.app["store"]
    uid = request.match_info["uid"]
    body = await request.json()
    fields: dict[str, Any] = {}

    if "value" in body:
        if body["value"] in (None, ""):
            fields.update(value=None, value_source="none")
        else:
            fields.update(value=round(float(body["value"]), 2), value_source="manual")
        fields["status"] = STATUS_PRICED

    for key in ("name", "subtitle"):
        if key in body:
            fields[key] = str(body[key]).strip()
    if "name" in fields:
        # An operator-supplied name is by definition the one they want on stream.
        fields["name_is_english"] = True

    reprice_id = None
    if body.get("card_id"):
        new_id = str(body["card_id"])
        if not _safe_id(new_id):
            raise web.HTTPBadRequest(text="invalid card_id")
        fields["card_id"] = new_id
        # A suggestion carries no parallel, so re-price against the base card.
        fields["parallel_id"] = None
        fields["parallel_name"] = None
        fields["suggestions"] = []
        if await _cache_catalog_image(request.app, new_id):
            fields["image_source"] = "catalog"
        # Keep an explicit price the operator typed in the same request; otherwise
        # the old identity's valuation would survive a correction that invalidated it.
        if "value" not in body:
            fields["status"] = STATUS_IDENTIFYING
            reprice_id = new_id

    card = await store.update_card(uid, event="card_corrected", **fields)
    if card is None:
        raise web.HTTPNotFound(text="no such card")

    if reprice_id:
        task = asyncio.create_task(price_card(request.app, uid, reprice_id, None))
        request.app["tasks"].add(task)
        task.add_done_callback(request.app["tasks"].discard)

    return web.json_response(card.to_json())


async def delete_card(request: web.Request) -> web.Response:
    ok = await request.app["store"].remove_card(request.match_info["uid"])
    if not ok:
        raise web.HTTPNotFound(text="no such card")
    return web.json_response({"removed": True})


async def post_reset(request: web.Request) -> web.Response:
    await request.app["store"].reset()
    request.app["price_cache"].clear()
    return web.json_response({"reset": True})


async def get_card_image(request: web.Request) -> web.StreamResponse:
    """Serve a card's picture: catalog art when we have it, the scan otherwise."""
    config: Config = request.app["config"]
    card = request.app["store"].card(request.match_info["uid"])
    if card is None:
        raise web.HTTPNotFound()

    headers = {"Cache-Control": "public, max-age=86400"}

    if card.image_source == "catalog" and card.card_id and _safe_id(card.card_id):
        path = _cached_image(config.image_cache_dir, card.card_id)
        if path:
            return web.FileResponse(
                path,
                headers={**headers, "Content-Type": CONTENT_TYPES.get(path.suffix, "image/jpeg")},
            )

    if card.has_capture:
        path = config.capture_dir / f"{card.uid}.bin"
        if path.exists():
            return web.FileResponse(path, headers={**headers, "Content-Type": card.capture_type})

    raise web.HTTPNotFound()


async def get_search(request: web.Request) -> web.Response:
    query = (request.query.get("q") or "").strip()
    if len(query) < 2:
        return web.json_response({"results": []})
    try:
        results = await request.app["client"].search(query)
    except Exception as exc:
        raise web.HTTPBadGateway(text=str(exc))
    return web.json_response({"results": results})


# ----------------------------------------------------------------------- assembly


async def _on_startup(app: web.Application) -> None:
    await app["client"].start()
    pricecharting = app.get("pricecharting")
    if pricecharting is not None:
        await pricecharting.start()
        if pricecharting.configured:
            app["pricecharting_ok"] = await pricecharting.health()
            if not app["pricecharting_ok"]:
                log.error(
                    "PriceCharting token did not return price data — check the "
                    "subscription covers Pokemon cards. Falling back to CardSight pricing."
                )
        else:
            log.info("No PriceCharting token — using CardSight pricing only.")
    if app["client"].configured:
        app["auth_ok"] = await app["client"].health_auth()
        if app["auth_ok"] is False:
            log.error("CardSight rejected the API key — scans will fail until it is fixed")
    else:
        app["auth_ok"] = None
        log.warning("No CardSight API key configured. Start with --mock to rehearse offline.")

    # Report pack summaries to multichat when a URL is configured. Runs in mock
    # mode too, so the whole giveaway → pack → ledger loop can be rehearsed offline.
    config: Config = app["config"]
    if config.report_url:
        reporter = Reporter(app["store"], config.report_url, config.report_token)
        await reporter.start()
        app["reporter"] = reporter
        log.info("Reporting pack summaries to %s", config.report_url)


async def _on_cleanup(app: web.Application) -> None:
    reporter = app.get("reporter")
    if reporter is not None:
        await reporter.close()
    for task in list(app["tasks"]):
        task.cancel()
    await app["client"].close()
    if app.get("pricecharting") is not None:
        await app["pricecharting"].close()


def create_app(config: Config) -> web.Application:
    config.state_dir.mkdir(parents=True, exist_ok=True)

    store = Store(config.session_file, config.default_pack_size)
    store.load()

    app = web.Application(
        middlewares=[auth_middleware],
        client_max_size=MAX_UPLOAD_BYTES + 1024 * 1024,
    )
    app["config"] = config
    app["store"] = store
    app["price_cache"] = {}
    # Set in _on_startup when config.report_url is configured (needs the loop).
    app["reporter"] = None
    # Strong refs: asyncio only holds weak ones, and a collected task stops mid-scan.
    app["tasks"] = set()
    # Skipped entirely in mock mode: rehearsal must not reach the network.
    app["pricecharting"] = (
        None if config.mock else PriceChartingClient(config.pricecharting_token)
    )
    app["client"] = (
        MockCardSightClient()
        if config.mock
        else CardSightClient(config.api_key, config.api_base, config.segment, config.rate_limit)
    )

    app.add_routes(
        [
            web.get("/", index),
            web.get("/scan", _page("scanner.html")),
            web.get("/overlay", _page("overlay.html")),
            web.get("/healthz", healthz),
            web.get("/ws", websocket),
            web.get("/api/state", get_state),
            web.post("/api/scan", post_scan),
            web.post("/api/pack", post_pack),
            web.post("/api/pack/close", post_pack_close),
            web.patch("/api/card/{uid}", patch_card),
            web.delete("/api/card/{uid}", delete_card),
            web.get("/api/card/{uid}/image", get_card_image),
            web.post("/api/session/reset", post_reset),
            web.get("/api/search", get_search),
            web.static("/static", STATIC_DIR),
        ]
    )

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def ssl_context(config: Config) -> ssl.SSLContext | None:
    if not (config.tls_cert and config.tls_key):
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(config.tls_cert, config.tls_key)
    return context
