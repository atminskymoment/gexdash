/* gexdash app shell: the sidebar, tooltips and the ticker search.
 *
 * The look follows the AffectUI design system (affectui.com), recreated in
 * plain CSS in styles.css: its tokens, sizes and behaviours, not its React
 * components, so the dashboard keeps its no-build setup. This file holds the
 * three behaviours that need script:
 *
 *  - Sidebar: expanded with labels, or an icon rail. The choice is
 *    remembered; with none made, narrow windows start on the rail. The width
 *    snaps rather than animates, so the charts lay out once (app.js watches
 *    the main column with a ResizeObserver).
 *  - Tooltips on [data-tip]: AffectUI's 500ms hover delay, shown at once
 *    under keyboard focus, hidden on press, scroll or Escape; a small dark
 *    label that never holds the only copy of anything (every icon-only
 *    control also has an aria-label). SVG <title> tooltips on chart marks
 *    stay native.
 *  - Ticker search: a combobox with the registered symbols and recent ones
 *    as suggestions. It commits on Enter or a pick, never on blur, and
 *    Escape puts back the symbol on screen. "/" focuses it from anywhere
 *    that is not itself a field.
 *
 * Load order: after tz.js, before the rest; nothing runs until app.js calls
 * uiInit(). Every top-level name carries a ui/UI_ prefix.
 */

const UI_NAV_KEY = "gexdash-nav";
const UI_RECENT_KEY = "gexdash-recent-symbols";
/** Below this width the sidebar starts as a rail unless one was chosen. */
const UI_RAIL_BELOW = 1100;

/** True when the event's target is somewhere a key press is typing. */
const uiTyping = (el) => !!el && (el.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName));

/** A small inline icon, 18px, drawn in currentColor. */
const UI_ICONS = {
  play: '<path d="M7 5l8 5-8 5z" fill="currentColor"/>',
  pause: '<rect x="5.5" y="4.5" width="3" height="11" rx="1" fill="currentColor" stroke="none"/><rect x="11.5" y="4.5" width="3" height="11" rx="1" fill="currentColor" stroke="none"/>',
  expand: '<path d="M8 5l5 5-5 5"/>',
  collapse: '<path d="M12 5l-5 5 5 5"/>',
};
const uiIcon = (k) => `<svg class="ico" viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">${UI_ICONS[k] || ""}</svg>`;

/* ------------------------------------------------------------- sidebar */

/** Rail (icons only) or expanded; stored when the reader chooses. */
function uiSetRail(rail, remember = true) {
  const app = document.getElementById("app");
  app.classList.toggle("rail", rail);
  const btn = document.getElementById("nav-collapse");
  btn.setAttribute("aria-expanded", String(!rail));
  btn.setAttribute("aria-label", rail ? "Expand the sidebar" : "Collapse the sidebar");
  btn.querySelector("svg").outerHTML = uiIcon(rail ? "expand" : "collapse");
  btn.querySelector(".nav-label").textContent = rail ? "Expand" : "Collapse";
  if (remember) { try { localStorage.setItem(UI_NAV_KEY, rail ? "rail" : "open"); } catch { /* not kept */ } }
}

function uiInitSidebar() {
  let saved = null;
  try { saved = localStorage.getItem(UI_NAV_KEY); } catch { saved = null; }
  uiSetRail(saved ? saved === "rail" : innerWidth < UI_RAIL_BELOW, false);
  document.getElementById("nav-collapse").addEventListener("click", () =>
    uiSetRail(!document.getElementById("app").classList.contains("rail")));
}

/* ------------------------------------------------------------ tooltips */

const uiTip = { timer: 0, shownAt: 0, target: null };

/** Show the tooltip for an element, beside it on its data-tip-side. */
function uiShowTip(el) {
  const text = el.getAttribute("data-tip");
  if (!text) return;
  // Nav labels are on screen in the expanded sidebar; only the rail needs them.
  if (el.dataset.tipWhen === "rail" && !document.getElementById("app")?.classList.contains("rail")) return;
  const tip = document.getElementById("ui-tip");
  tip.textContent = text;
  tip.hidden = false;
  const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
  const side = el.dataset.tipSide || "top", gap = 6;
  let x = r.left + r.width / 2 - t.width / 2, y = r.top - t.height - gap;
  if (side === "right") { x = r.right + gap; y = r.top + r.height / 2 - t.height / 2; }
  if (side === "bottom" || (side === "top" && y < 4)) y = r.bottom + gap;
  x = Math.max(8, Math.min(x, innerWidth - t.width - 8));
  y = Math.max(4, Math.min(y, innerHeight - t.height - 4));
  tip.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
  tip.dataset.side = side;
  uiTip.target = el;
  uiTip.shownAt = Date.now();
}

function uiHideTip() {
  clearTimeout(uiTip.timer);
  uiTip.target = null;
  const tip = document.getElementById("ui-tip");
  if (tip && !tip.hidden) { tip.hidden = true; uiTip.shownAt = Date.now(); }
}

function uiInitTooltips() {
  document.addEventListener("mouseover", (ev) => {
    const el = ev.target.closest?.("[data-tip]");
    if (!el || el === uiTip.target) return;
    uiHideTip();
    // Moving straight from one tooltip to its neighbour shows the next at once.
    const quick = Date.now() - uiTip.shownAt < 300;
    uiTip.timer = setTimeout(() => uiShowTip(el), quick ? 0 : 500);
  });
  document.addEventListener("mouseout", (ev) => {
    const el = ev.target.closest?.("[data-tip]");
    if (el && !el.contains(ev.relatedTarget)) uiHideTip();
  });
  document.addEventListener("focusin", (ev) => {
    const el = ev.target.closest?.("[data-tip]");
    if (el && ev.target.matches(":focus-visible")) { clearTimeout(uiTip.timer); uiShowTip(el); }
  });
  document.addEventListener("focusout", uiHideTip);
  document.addEventListener("pointerdown", uiHideTip, true);
  document.addEventListener("scroll", uiHideTip, true);
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") uiHideTip(); });
}

/* ------------------------------------------------------- ticker search */

const uiCombo = { items: [], active: -1, current: () => "", commit: () => {} };

/** Symbols typed before, newest first, kept for the suggestions. */
function uiRecent() {
  try { return JSON.parse(localStorage.getItem(UI_RECENT_KEY) || "[]").filter((s) => typeof s === "string").slice(0, 8); }
  catch { return []; }
}
function uiRemember(symbol) {
  const list = [symbol, ...uiRecent().filter((s) => s !== symbol)].slice(0, 8);
  try { localStorage.setItem(UI_RECENT_KEY, JSON.stringify(list)); } catch { /* not kept */ }
}

/** The suggestions for what is typed: symbols that start with it (registered,
 *  then recent), then registered ones a word of whose name starts with it,
 *  then the typed text itself to look up. The best match comes first, so
 *  Enter on "ND" picks NDX; typing a whole unregistered ticker puts the
 *  look-up first, since nothing else matches. */
function uiSuggest(text, registered) {
  const q = text.trim().toUpperCase();
  const recent = uiRecent().map((s) => ({ symbol: s, label: "Recent" }));
  if (!q) return [...registered, ...recent.filter((r) => !registered.some((g) => g.symbol === r.symbol))];
  const byKey = [...registered, ...recent].filter((r, k, all) =>
    r.symbol.startsWith(q) && all.findIndex((x) => x.symbol === r.symbol) === k);
  const byName = registered.filter((r) => !r.symbol.startsWith(q) &&
    r.label.toUpperCase().split(/[^A-Z0-9&$]+/).some((w) => w.startsWith(q)));
  const out = [...byKey, ...byName];
  if (!out.some((r) => r.symbol === q)) out.push({ symbol: q, label: "Look up this symbol" });
  return out;
}

/** Fill the list and mark the active option. */
function uiRenderCombo() {
  const input = document.getElementById("f-symbol"), list = document.getElementById("symbol-list");
  if (!uiCombo.open) { list.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); return; }
  list.innerHTML = uiCombo.items.length
    ? uiCombo.items.map((r, k) => `<div class="menu-item${k === uiCombo.active ? " active" : ""}" role="option" id="sym-opt-${k}" data-k="${k}"
        aria-selected="${k === uiCombo.active}"><b>${esc(r.symbol)}</b><span class="menu-desc">${esc(r.label)}</span></div>`).join("")
    : `<div class="menu-empty">No suggestions. Type a symbol and press Enter.</div>`;
  list.hidden = false;
  input.setAttribute("aria-expanded", "true");
  if (uiCombo.active >= 0) input.setAttribute("aria-activedescendant", `sym-opt-${uiCombo.active}`);
  else input.removeAttribute("aria-activedescendant");
  list.querySelector(".menu-item.active")?.scrollIntoView({ block: "nearest" });
}

function uiOpenCombo() {
  const input = document.getElementById("f-symbol");
  uiCombo.open = true;
  // While the box still shows the current symbol, list everything.
  const text = input.value.trim().toUpperCase() === uiCombo.current() ? "" : input.value;
  uiCombo.items = uiSuggest(text, uiCombo.registered());
  uiCombo.active = -1;
  uiRenderCombo();
}

function uiCloseCombo(restore = true) {
  uiCombo.open = false;
  if (restore) document.getElementById("f-symbol").value = uiCombo.current();
  uiRenderCombo();
}

function uiCommitCombo(symbol) {
  const s = String(symbol || "").trim().toUpperCase();
  uiCloseCombo(false);
  const input = document.getElementById("f-symbol");
  if (!s) { input.value = uiCombo.current(); return; }
  input.value = s;
  input.blur();
  uiCombo.commit(s);
}

/** Wire the search box. `registered()` lists {symbol, label}; `current()` is
 *  the symbol the view on screen shows; `commit(symbol)` switches to one. */
function uiInitSearch({ registered, current, commit }) {
  Object.assign(uiCombo, { registered, current, commit });
  const input = document.getElementById("f-symbol"), list = document.getElementById("symbol-list");
  input.addEventListener("focus", () => { input.select(); uiOpenCombo(); });
  input.addEventListener("input", () => {
    uiCombo.open = true;
    uiCombo.items = uiSuggest(input.value, registered());
    uiCombo.active = input.value.trim() ? 0 : -1;
    uiRenderCombo();
  });
  input.addEventListener("keydown", (ev) => {
    const n = uiCombo.items.length;
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (!uiCombo.open) return uiOpenCombo();
      uiCombo.active = n ? (uiCombo.active + (ev.key === "ArrowDown" ? 1 : -1) + n) % n : -1;
      uiRenderCombo();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      uiCommitCombo(uiCombo.active >= 0 && uiCombo.open ? uiCombo.items[uiCombo.active].symbol : input.value);
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      if (uiCombo.open) uiCloseCombo(); else input.blur();
    } else if (ev.key === "Tab") {
      uiCloseCombo();
    }
  });
  // Leaving the box puts the shown symbol back; only Enter or a pick switches.
  input.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== input) uiCloseCombo(); }, 120));
  list.addEventListener("pointerdown", (ev) => {
    const item = ev.target.closest(".menu-item");
    if (!item) return;
    ev.preventDefault();                   // keep focus, so blur does not close it first
    uiCommitCombo(uiCombo.items[Number(item.dataset.k)].symbol);
  });
  list.addEventListener("mousemove", (ev) => {
    const item = ev.target.closest(".menu-item");
    if (item && Number(item.dataset.k) !== uiCombo.active) { uiCombo.active = Number(item.dataset.k); uiRenderCombo(); }
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "/" && !uiTyping(ev.target) && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
      ev.preventDefault();
      input.focus();
    }
  });
}

/** Show a symbol in the search box without committing anything. */
function uiShowSymbol(symbol) {
  const input = document.getElementById("f-symbol");
  if (input && document.activeElement !== input) input.value = symbol || "";
}

/** Start the shell: sidebar and tooltips. The search is wired by app.js,
 *  which knows the symbols. */
function uiInit() {
  uiInitSidebar();
  uiInitTooltips();
}
