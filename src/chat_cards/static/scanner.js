/* Producer-facing scanner.
 *
 * Two capture paths. iOS Safari only exposes getUserMedia on a secure context, so
 * over plain http://192.168.x.x the live preview is unavailable; there we fall
 * back to <input capture>, which opens the native camera sheet. Neither path
 * writes to the camera roll — the frame goes straight into an upload.
 */

const params = new URLSearchParams(location.search);
const TOKEN = params.get("token") || "";
const MAX_EDGE = 1600;
const JPEG_QUALITY = 0.85;
const CHASE_AT = 25;

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const $ = (id) => document.getElementById(id);

const el = {
  totalValue: $("total-value"), totalCount: $("total-count"),
  link: $("link"), linkText: $("link-text"),
  packbar: $("packbar"), packTitle: $("pack-title"), packSub: $("pack-sub"), newPack: $("new-pack"),
  viewfinder: $("viewfinder"), video: $("video"), hint: $("viewfinder-hint"), flash: $("flash"),
  shutter: $("shutter"), shutterHint: $("shutter-hint"), fileInput: $("file-input"),
  feed: $("feed"), feedEmpty: $("feed-empty"),
  packSheet: $("pack-sheet"), packWinner: $("pack-winner"), packSize: $("pack-size"), packStart: $("pack-start"),
  cardSheet: $("card-sheet"), cardTitle: $("card-sheet-title"), cardSub: $("card-sheet-sub"),
  cardPrice: $("card-price"), altField: $("alt-field"), altChips: $("alt-chips"),
  cardName: $("card-name"), cardNameHint: $("card-name-hint"), cardPriceHint: $("card-price-hint"),
  cardSearch: $("card-search"), searchResults: $("search-results"),
  cardDelete: $("card-delete"), cardSave: $("card-save"),
  toast: $("toast"),
};

let state = null;
let editingUid = null;
let useLiveCamera = false;

/* ------------------------------------------------------------------ plumbing */

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";

  const response = await fetch(path, { ...options, headers });
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  return response.status === 204 ? null : response.json();
}

let toastTimer;
function toast(message, isError = false) {
  el.toast.textContent = message;
  el.toast.classList.toggle("is-error", isError);
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, 2600);
}

/* -------------------------------------------------------------------- camera */

function useSystemCamera(reason) {
  useLiveCamera = false;
  el.hint.textContent = reason;
  el.shutterHint.textContent = "Opens the camera — the photo is uploaded, never saved to Photos";
}

async function startCamera() {
  // Without a secure context getUserMedia does not exist, and on iOS a LAN
  // address is exactly that case. Fall back rather than fail.
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    useSystemCamera("Tap the shutter to open the camera");
    return;
  }

  try {
    // A permission prompt left unanswered, or a machine with no camera, can leave
    // this pending forever. Nothing about a live show tolerates a dead shutter.
    const stream = await Promise.race([
      navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1440 } },
        audio: false,
      }),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 6000)),
    ]);
    el.video.srcObject = stream;
    await el.video.play();
    useLiveCamera = true;
    el.viewfinder.classList.add("is-live");
    el.shutterHint.textContent = "Hold the card flat and fill the frame";
  } catch (error) {
    useSystemCamera(
      error.message === "timeout"
        ? "Camera did not start — tap the shutter to use the system camera"
        : "Camera unavailable — tap the shutter to use the system camera"
    );
  }
}

function loadImage(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
    image.onerror = () => { URL.revokeObjectURL(url); reject(new Error("decode failed")); };
    image.src = url;
  });
}

/* Shrink to something the API digests quickly. Re-encoding also normalises HEIC
   from the iOS camera into JPEG, so the server never has to decode an image. */
async function toUploadBlob(source) {
  const drawable = source instanceof Blob ? await loadImage(source) : source;
  const width = drawable.videoWidth || drawable.naturalWidth || drawable.width;
  const height = drawable.videoHeight || drawable.naturalHeight || drawable.height;
  if (!width || !height) throw new Error("no frame");

  const ratio = Math.min(1, MAX_EDGE / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  canvas.getContext("2d").drawImage(drawable, 0, 0, canvas.width, canvas.height);

  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
}

async function upload(blob) {
  const form = new FormData();
  form.append("image", blob, "card.jpg");
  el.shutter.classList.add("is-busy");
  try {
    await api("/api/scan", { method: "POST", body: form });
    if (!state?.pack) toast("Scanned — no pack open, counted in the session total");
  } catch (error) {
    toast(`Scan failed: ${error.message}`, true);
  } finally {
    el.shutter.classList.remove("is-busy");
  }
}

async function capture() {
  if (!useLiveCamera) {
    el.fileInput.click();
    return;
  }
  el.flash.classList.remove("is-firing");
  void el.flash.offsetWidth;
  el.flash.classList.add("is-firing");
  try {
    const blob = await toUploadBlob(el.video);
    if (blob) await upload(blob);
  } catch (error) {
    toast(`Capture failed: ${error.message}`, true);
  }
}

el.shutter.addEventListener("click", capture);

el.fileInput.addEventListener("change", async () => {
  const file = el.fileInput.files?.[0];
  el.fileInput.value = ""; // let the same photo be retaken
  if (!file) return;
  try {
    const blob = await toUploadBlob(file);
    await upload(blob || file); // if canvas cannot re-encode it, ship the original
  } catch {
    await upload(file); // the API accepts HEIC directly
  }
});

/* -------------------------------------------------------------------- render */

function cardSubtitle(card) {
  if (card.status === "identifying") return "Identifying…";
  if (card.status === "error") return card.error || "Lookup failed";
  if (card.status === "unmatched") return "No catalog match — tap to fix";
  if (card.name_is_english === false) return `${card.original_name || ""} — tap to set an English name`.trim();

  const bits = [card.subtitle].filter(Boolean);
  const pricing = card.pricing || {};
  if (pricing.sample_size) {
    // Say which rung of the ladder the number came from, not just how many rows.
    bits.push(`${pricing.sample_size} ${pricing.basis_label || "comps"}`);
  }
  if (card.value_source === "manual") bits.push("manual price");
  return bits.join(" · ") || "—";
}

function renderFeed() {
  const cards = state.pack
    ? state.cards.filter((c) => c.pack_id === state.pack.id)
    : state.cards.slice(-10);
  const ordered = [...cards].reverse(); // newest at the top, next to the shutter

  el.feedEmpty.hidden = ordered.length > 0;
  el.feed.replaceChildren(
    ...ordered.map((card) => {
      const row = document.createElement("li");
      row.className = "row";
      row.classList.toggle("is-chase", card.value != null && card.value >= CHASE_AT);
      row.classList.toggle("is-unmatched", card.status === "unmatched" || card.status === "error");
      row.classList.toggle("is-translate", card.name_is_english === false);
      row.addEventListener("click", () => openCardSheet(card.uid));

      const thumb = document.createElement("img");
      thumb.className = "row__thumb";
      thumb.alt = "";
      thumb.onerror = () => { thumb.style.visibility = "hidden"; };
      if (card.image_url) thumb.src = card.image_url;
      else thumb.style.visibility = "hidden";

      const body = document.createElement("div");
      body.className = "row__body";
      const name = document.createElement("div");
      name.className = "row__name";
      if (card.name_is_english === false) {
        const flag = document.createElement("span");
        flag.className = "row__flag";
        flag.textContent = card.language || "not en";
        name.append(flag);
      }
      name.append(card.status === "identifying" ? "Scanning…" : card.name || "Unknown card");
      const sub = document.createElement("div");
      sub.className = "row__sub";
      sub.textContent = cardSubtitle(card);
      body.append(name, sub);

      let price;
      if (card.status === "identifying") {
        price = document.createElement("div");
        price.className = "spinner";
      } else {
        price = document.createElement("div");
        price.className = "row__price";
        if (card.value == null) { price.textContent = "—"; price.classList.add("is-quiet"); }
        else {
          const approx = card.pricing?.estimated ? "~" : "";
          price.textContent = approx + money.format(card.value);
        }
      }

      row.append(thumb, body, price);
      return row;
    })
  );
}

function render() {
  el.totalValue.textContent = money.format(state.totals.value);
  el.totalCount.textContent = String(state.totals.count);

  const pack = state.pack;
  el.packbar.classList.toggle("is-open", Boolean(pack));
  if (pack) {
    el.packTitle.textContent = `Pack ${pack.index}${pack.winner ? ` · ${pack.winner}` : ""}`;
    el.packSub.textContent = `${pack.revealed} of ${pack.size} · ${money.format(pack.value)}`;
    el.newPack.textContent = "New pack";
  } else {
    el.packTitle.textContent = "No pack open";
    el.packSub.textContent = "Start one before you scan";
  }

  renderFeed();

  // Keep the sheet's live figures in step while it is open.
  if (editingUid) {
    const card = state.cards.find((c) => c.uid === editingUid);
    if (!card) closeSheets();
    else decorateCardSheet(card);
  }
}

/* -------------------------------------------------------------------- sheets */

function closeSheets() {
  el.packSheet.hidden = true;
  el.cardSheet.hidden = true;
  editingUid = null;
}

document.querySelectorAll("[data-close-sheet]").forEach((button) =>
  button.addEventListener("click", closeSheets)
);
[el.packSheet, el.cardSheet].forEach((sheet) =>
  sheet.addEventListener("click", (event) => { if (event.target === sheet) closeSheets(); })
);

el.newPack.addEventListener("click", () => {
  el.packSize.value = String(state?.default_pack_size ?? 5);
  el.packWinner.value = "";
  el.packSheet.hidden = false;
  el.packWinner.focus();
});

const clampSize = (n) => Math.max(1, Math.min(40, n));
$("size-down").addEventListener("click", () => { el.packSize.value = clampSize(Number(el.packSize.value) - 1); });
$("size-up").addEventListener("click", () => { el.packSize.value = clampSize(Number(el.packSize.value) + 1); });

el.packStart.addEventListener("click", async () => {
  try {
    await api("/api/pack", {
      method: "POST",
      body: JSON.stringify({
        size: clampSize(Number(el.packSize.value) || 5),
        winner: el.packWinner.value.trim(),
      }),
    });
    closeSheets();
    toast("Pack open — go ahead");
  } catch (error) {
    toast(error.message, true);
  }
});

function decorateCardSheet(card) {
  el.cardTitle.textContent = card.name || "Unidentified card";
  el.cardSub.textContent = [card.subtitle, card.confidence && `${card.confidence} confidence`]
    .filter(Boolean)
    .join(" · ");

  // Never clobber what the operator is mid-way through typing.
  if (document.activeElement !== el.cardName) {
    el.cardName.value = card.name || "";
  }
  if (document.activeElement !== el.cardPrice) {
    el.cardPrice.value = card.value != null ? String(card.value) : "";
  }

  const foreign = card.name_is_english === false;
  el.cardNameHint.hidden = !foreign;
  el.cardNameHint.classList.toggle("is-warning", foreign);
  if (foreign) {
    el.cardNameHint.textContent =
      `Catalog name is ${card.language ? card.language.toUpperCase() : "not English"}: ` +
      `${card.original_name || "—"}. Type the English name — the overlay is showing a placeholder.`;
  }

  // Explain a soft price rather than leaving the producer to guess at it.
  const pricing = card.pricing || {};
  const priceNote = pricing.note || (pricing.estimated ? "Estimated, not a completed sale" : "");
  el.cardPriceHint.hidden = !priceNote;
  el.cardPriceHint.classList.toggle("is-warning", Boolean(pricing.estimated));
  el.cardPriceHint.textContent = priceNote;

  const alternates = card.suggestions || [];
  el.altField.hidden = alternates.length === 0;
  el.altChips.replaceChildren(
    ...alternates.map((suggestion) => {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.textContent = suggestion.setName || suggestion.name || suggestion.id;
      chip.addEventListener("click", () => applyMatch(card.uid, suggestion.id, chip.textContent));
      return chip;
    })
  );
}

function openCardSheet(uid) {
  const card = state.cards.find((c) => c.uid === uid);
  if (!card) return;
  editingUid = uid;
  el.cardSearch.value = "";
  el.cardName.value = "";
  el.searchResults.replaceChildren();
  decorateCardSheet(card);
  el.cardSheet.hidden = false;
}

async function applyMatch(uid, cardId, label) {
  try {
    await api(`/api/card/${uid}`, { method: "PATCH", body: JSON.stringify({ card_id: cardId, name: label }) });
    toast("Match updated");
  } catch (error) {
    toast(error.message, true);
  }
}

el.cardSave.addEventListener("click", async () => {
  if (!editingUid) return;
  const card = state.cards.find((c) => c.uid === editingUid);
  const raw = el.cardPrice.value.trim();
  const name = el.cardName.value.trim();

  const body = { value: raw === "" ? null : Number(raw) };
  // Only send the name when it actually changed, so saving a price does not
  // silently mark an untouched foreign name as English.
  if (name && card && name !== card.name) body.name = name;

  try {
    await api(`/api/card/${editingUid}`, { method: "PATCH", body: JSON.stringify(body) });
    closeSheets();
    toast(body.name ? "Card updated" : "Price updated");
  } catch (error) {
    toast(error.message, true);
  }
});

el.cardDelete.addEventListener("click", async () => {
  if (!editingUid) return;
  try {
    await api(`/api/card/${editingUid}`, { method: "DELETE" });
    closeSheets();
    toast("Scan removed");
  } catch (error) {
    toast(error.message, true);
  }
});

let searchTimer;
el.cardSearch.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const query = el.cardSearch.value.trim();
  if (query.length < 2) { el.searchResults.replaceChildren(); return; }

  searchTimer = setTimeout(async () => {
    try {
      const { results } = await api(`/api/search?q=${encodeURIComponent(query)}`);
      el.searchResults.replaceChildren(
        ...results.map((hit) => {
          const label = [hit.name, hit.year, hit.setName || hit.releaseName, hit.parallelName]
            .filter(Boolean)
            .join(" · ");
          const chip = document.createElement("button");
          chip.className = "chip";
          chip.textContent = label;
          chip.addEventListener("click", () => applyMatch(editingUid, hit.id, label));
          return chip;
        })
      );
    } catch (error) {
      toast(error.message, true);
    }
  }, 320);
});

/* ----------------------------------------------------------------- transport */

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(
    `${scheme}://${location.host}/ws${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ""}`
  );
  let delay = 500;

  socket.onopen = () => {
    delay = 500;
    el.link.classList.add("is-live");
    el.linkText.textContent = "live";
  };
  socket.onmessage = (message) => {
    const payload = JSON.parse(message.data);
    if (payload.type !== "state") return;
    state = payload.state;
    render();
    // A dropped scan just disappears from the feed; without this the producer
    // has no idea whether it failed or is still working.
    if (payload.event === "card_failed") toast("No match — scan that card again", true);
  };
  socket.onclose = () => {
    el.link.classList.remove("is-live");
    el.linkText.textContent = "offline";
    setTimeout(connect, delay = Math.min(delay * 1.8, 8000));
  };
  socket.onerror = () => socket.close();
}

connect();
startCamera();
