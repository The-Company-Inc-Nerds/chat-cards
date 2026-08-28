/* Overlay renderer.
 *
 * Layout is a single focus-relative strip: every slot is absolutely centred and
 * then pushed sideways by its distance from the focused card, which is always the
 * most recently revealed one. Moving the focus therefore animates the whole reel
 * with one transform per slot, and nothing reflows.
 *
 * Query params: ?bg=1 preview background, ?scale=1.15 overall size,
 * ?history=6 cards to keep on screen when no pack is open, ?chase=25 gold threshold.
 */

const params = new URLSearchParams(location.search);
const HISTORY = Number(params.get("history") || 6);
const CHASE_AT = Number(params.get("chase") || 25);

// Falloff away from the hero card, indexed by absolute distance.
const SCALES = [1.22, 0.88, 0.8, 0.74, 0.7];
const OPACITIES = [1, 0.95, 0.78, 0.55, 0.32];
const MAX_DISTANCE = 5;

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

const el = {
  stage: document.getElementById("stage"),
  track: document.getElementById("track"),
  totalValue: document.getElementById("total-value"),
  totalCount: document.getElementById("total-count"),
  packPanel: document.getElementById("pack-panel"),
  packIndex: document.getElementById("pack-index"),
  packWinner: document.getElementById("pack-winner"),
  packValue: document.getElementById("pack-value"),
  packPips: document.getElementById("pack-pips"),
  packCount: document.getElementById("pack-count"),
  banner: document.getElementById("banner"),
  bannerLabel: document.getElementById("banner-label"),
  bannerSub: document.getElementById("banner-sub"),
  template: document.getElementById("slot-template"),
};

if (params.get("bg")) document.body.classList.add("has-bg");
if (params.get("scale")) el.stage.style.setProperty("--ui-scale", params.get("scale"));

const slots = new Map(); // key -> element
const seen = new Set(); // uids already animated in
let lastState = null;

/* ------------------------------------------------------------------ counters */

const counters = new WeakMap();

function animateNumber(node, to, format) {
  const from = counters.get(node) ?? to;
  counters.set(node, to);
  if (from === to) {
    node.textContent = format(to);
    return;
  }

  node.classList.remove("is-bumped");
  void node.offsetWidth; // restart the pop
  node.classList.add("is-bumped");

  const start = performance.now();
  const duration = 620;
  function frame(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    node.textContent = format(from + (to - from) * eased);
    if (t < 1) requestAnimationFrame(frame);
    else node.textContent = format(to);
  }
  requestAnimationFrame(frame);
}

const asMoney = (v) => money.format(v);
const asMoneyApprox = (v) => `~${money.format(v)}`;
const asCount = (v) => String(Math.round(v));

/* ---------------------------------------------------------------------- slots */

function priceText(card) {
  if (card.status === "identifying") return { text: "···", quiet: true };
  if (card.value == null) return { text: "—", quiet: true };
  // A tilde marks a number derived from asking prices or stale sales rather
  // than recent comps. Passing an estimate off as a sale would be a lie told at
  // scale, and someone is deciding whether to mail these cards.
  const estimated = Boolean(card.pricing && card.pricing.estimated);
  return {
    text: (estimated ? "~" : "") + money.format(card.value),
    quiet: false,
    estimated,
  };
}

function nameText(card) {
  if (card.status === "identifying") return "Identifying…";
  return card.name || "Unknown card";
}

function badgeFor(card) {
  if (card.numbered_to) return { text: `/${card.numbered_to}`, gold: true };
  if (card.parallel_name) return { text: card.parallel_name, gold: true };
  if (card.status === "unmatched") return { text: "no match", gold: false };
  // `rarity` is derived server-side; `attributes` holds raw slugs that must
  // never reach the screen.
  if (card.rarity) return { text: card.rarity, gold: false };
  return null;
}

function makeSlot() {
  return el.template.content.firstElementChild.cloneNode(true);
}

function renderCard(node, card) {
  node.classList.toggle("is-pending", card.status === "identifying");
  node.classList.remove("is-empty");

  const art = node.querySelector(".card__art");
  const wanted = card.image_url;
  if (!wanted) {
    // Nothing servable yet — the shimmer placeholder carries the slot.
    art.removeAttribute("src");
    delete art.dataset.src;
    art.classList.remove("is-loaded");
  } else if (art.dataset.src !== wanted) {
    art.dataset.src = wanted;
    art.classList.remove("is-loaded");
    art.onload = () => art.classList.add("is-loaded");
    art.onerror = () => art.classList.remove("is-loaded");
    art.src = wanted;
    // A already-cached image may report complete without ever firing load.
    if (art.complete && art.naturalWidth > 0) art.classList.add("is-loaded");
  }

  node.querySelector(".meta__name").textContent = nameText(card);

  const price = priceText(card);
  const priceNode = node.querySelector(".meta__price");
  priceNode.textContent = price.text;
  priceNode.classList.toggle("is-quiet", price.quiet);
  priceNode.classList.toggle("is-estimated", Boolean(price.estimated));

  const badgeNode = node.querySelector(".card__badge");
  const badge = badgeFor(card);
  badgeNode.textContent = badge ? badge.text : "";
  badgeNode.classList.toggle("is-shown", Boolean(badge));
  badgeNode.classList.toggle("is-gold", Boolean(badge && badge.gold));

  node.classList.toggle("is-chase", card.value != null && card.value >= CHASE_AT);
}

function renderEmpty(node) {
  node.classList.add("is-empty");
  node.classList.remove("is-pending", "is-chase", "is-hero");
  const art = node.querySelector(".card__art");
  art.removeAttribute("src");
  delete art.dataset.src;
  art.classList.remove("is-loaded");
  node.querySelector(".meta__name").textContent = "";
  node.querySelector(".meta__price").textContent = "";
  node.querySelector(".card__badge").classList.remove("is-shown");
}

/* --------------------------------------------------------------------- render */

function buildSlotList(state) {
  const pack = state.pack;
  if (pack) {
    const cards = state.cards.filter((c) => c.pack_id === pack.id);
    const list = cards.map((card) => ({ key: card.uid, card }));
    // Dim placeholders for the cards still sealed in the pack.
    for (let i = 0; i < pack.remaining; i += 1) list.push({ key: `empty-${pack.id}-${i}` });
    return list;
  }
  return state.cards.slice(-HISTORY).map((card) => ({ key: card.uid, card }));
}

const isEstimated = (card) => Boolean(card.pricing && card.pricing.estimated && card.value != null);

function render(state, event) {
  lastState = state;

  const sessionEstimated = state.cards.some(isEstimated);
  el.totalValue.classList.toggle("is-estimated", sessionEstimated);
  animateNumber(el.totalValue, state.totals.value, sessionEstimated ? asMoneyApprox : asMoney);
  animateNumber(el.totalCount, state.totals.count, asCount);

  const pack = state.pack;
  el.packPanel.hidden = !pack;
  if (pack) {
    el.packIndex.textContent = `Pack ${pack.index}`;
    el.packWinner.textContent = pack.winner ? `for ${pack.winner}` : pack.label || "";
    const packEstimated = state.cards.some((c) => c.pack_id === pack.id && isEstimated(c));
    el.packValue.classList.toggle("is-estimated", packEstimated);
    animateNumber(el.packValue, pack.value, packEstimated ? asMoneyApprox : asMoney);
    el.packCount.textContent = `${pack.revealed} / ${pack.size}`;

    const total = Math.max(pack.size, pack.revealed);
    if (el.packPips.childElementCount !== total) {
      el.packPips.replaceChildren(
        ...Array.from({ length: total }, () => {
          const pip = document.createElement("span");
          pip.className = "pip";
          return pip;
        })
      );
    }
    [...el.packPips.children].forEach((pip, i) => pip.classList.toggle("is-filled", i < pack.revealed));
  }

  const list = buildSlotList(state);
  const keys = new Set(list.map((entry) => entry.key));

  for (const [key, node] of slots) {
    if (!keys.has(key)) {
      node.remove();
      slots.delete(key);
    }
  }

  // The hero is the newest real card. Before anything is revealed there is no
  // hero at all, so centre the row of sealed slots rather than blowing up slot 0.
  let focus = -1;
  for (let i = list.length - 1; i >= 0; i -= 1) {
    if (list[i].card) { focus = i; break; }
  }
  if (focus < 0) focus = Math.floor((list.length - 1) / 2);

  list.forEach((entry, index) => {
    let node = slots.get(entry.key);
    const isNew = !node;
    if (isNew) {
      node = makeSlot();
      slots.set(entry.key, node);
      el.track.appendChild(node);
    }

    if (entry.card) renderCard(node, entry.card);
    else renderEmpty(node);

    const offset = index - focus;
    const distance = Math.min(Math.abs(offset), SCALES.length - 1);
    const x = offset * getStep() + Math.sign(offset) * getPush();
    // Only a real card earns the hero scale; a centred sealed slot stays with
    // its neighbours so an unopened pack reads as an even row.
    const isHero = offset === 0 && Boolean(entry.card);
    const rank = isHero ? 0 : Math.max(1, distance);
    const scale = SCALES[rank];
    let opacity = Math.abs(offset) > MAX_DISTANCE ? 0 : OPACITIES[rank];
    if (!entry.card) opacity *= 0.62;

    node.style.setProperty("--x", `${x}px`);
    node.style.setProperty("--scale", scale);
    node.style.setProperty("--opacity", opacity);
    node.style.setProperty("--z", String(100 - Math.abs(offset)));
    node.classList.toggle("is-hero", isHero);

    // Animate only a card's first appearance, never a re-render of an old one.
    if (entry.card && !seen.has(entry.card.uid)) {
      seen.add(entry.card.uid);
      if (offset === 0) {
        node.classList.add("is-entering");
        setTimeout(() => node.classList.remove("is-entering"), 700);
      }
    }
  });

  if (event === "pack_opened") {
    const label = pack && pack.winner ? pack.winner : `Pack ${pack ? pack.index : ""}`;
    showBanner("NEW PACK", label);
  }
}

function getStep() {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--step")) || 150;
}
function getPush() {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--push")) || 34;
}

function showBanner(label, sub) {
  el.bannerLabel.textContent = label;
  el.bannerSub.textContent = sub || "";
  el.banner.hidden = false;
  el.banner.style.animation = "none";
  void el.banner.offsetWidth;
  el.banner.style.animation = "";
  clearTimeout(showBanner.timer);
  showBanner.timer = setTimeout(() => { el.banner.hidden = true; }, 2600);
}

/* ------------------------------------------------------------------ transport */

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const token = params.get("token");
  const url = `${scheme}://${location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  const socket = new WebSocket(url);
  let delay = 500;

  socket.onopen = () => { delay = 500; };
  socket.onmessage = (message) => {
    const payload = JSON.parse(message.data);
    if (payload.type === "state") render(payload.state, payload.event);
  };
  // OBS reloads browser sources freely, so reconnect must be automatic and quiet.
  socket.onclose = () => setTimeout(connect, delay = Math.min(delay * 1.8, 8000));
  socket.onerror = () => socket.close();
}

connect();
