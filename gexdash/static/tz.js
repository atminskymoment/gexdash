/* Display time zone. Every clock time on screen -- the header clock, intraday
 * axis ticks, crosshair tags, legends, readouts and signal tooltips -- is
 * formatted in the zone chosen here.
 *
 * Display only. Sessions are the exchange's whatever the reader's zone: the
 * 9:30 and 18:00 anchors, 1h/4h aggregation, 1D/5D/10D session counts and
 * roll dates are all computed server-side in New York time and never sent a
 * zone. Daily, weekly and monthly bars are dated by their session, so they are
 * not converted either: a session stamped 05:00Z would land on the previous
 * day in Los Angeles. Likewise the session labels on an intraday axis name the
 * trading day, so in Tokyo a bar from Monday's US session can read "Tue 05:55"
 * under a divider labelled "Mon 23". Both are right.
 *
 * Loaded before technicals.js and app.js. No DOM access at load time, so the
 * helpers can be exercised outside a browser (tests/test_tz.py). */

const TZ_EXCHANGE = "America/New_York";
const TZ_STORAGE_KEY = "gexdash-tz";

/** The picker's zones. `tags` is [standard, daylight]; one tag means the zone
 *  either has no daylight saving or is named generically (ET covers EST and
 *  EDT, as the exchange itself writes it). Intl's own short names are no use
 *  outside the US: "GMT+5:30", or "Germany Time" in the generic style. */
const TZ_CHOICES = [
  { zone: "America/New_York", label: "New York — exchange", tags: ["ET"] },
  { zone: "America/Chicago", label: "Chicago", tags: ["CT"] },
  { zone: "America/Denver", label: "Denver", tags: ["MT"] },
  { zone: "America/Los_Angeles", label: "Los Angeles", tags: ["PT"] },
  { zone: "UTC", label: "UTC", tags: ["UTC"] },
  { zone: "Europe/London", label: "London", tags: ["GMT", "BST"] },
  { zone: "Europe/Berlin", label: "Frankfurt", tags: ["CET", "CEST"] },
  { zone: "Asia/Kolkata", label: "India", tags: ["IST"] },
  { zone: "Asia/Singapore", label: "Singapore", tags: ["SGT"] },
  { zone: "Asia/Hong_Kong", label: "Hong Kong", tags: ["HKT"] },
  { zone: "Asia/Tokyo", label: "Tokyo", tags: ["JST"] },
  { zone: "Australia/Sydney", label: "Sydney", tags: ["AEST", "AEDT"] },
];

/** `choice` is what was picked ("local" follows the machine, so a laptop that
 *  travels keeps showing its own time); `zone` is what it resolves to. */
const tz = { choice: "exchange", zone: TZ_EXCHANGE, listeners: [] };
const TZ_FMT_CACHE = new Map();

/** The browser's canonical id for a zone, or null if it cannot format in it. */
function tzCanonical(zone) {
  if (!zone || typeof zone !== "string") return null;
  try {
    return new Intl.DateTimeFormat("en-US", { timeZone: zone }).resolvedOptions().timeZone;
  } catch {
    return null;
  }
}

/** An IANA zone this browser can format in, spelled as the picker spells it
 *  where it is one of the picker's: ICU canonicalises some names backwards
 *  (Asia/Kolkata → Asia/Calcutta), which would knock India off its entry. */
function tzValid(zone) {
  const canon = tzCanonical(zone);
  if (!canon) return null;
  const known = TZ_CHOICES.find((c) => tzCanonical(c.zone) === canon);
  return known ? known.zone : canon;
}

/** The browser's own zone; New York if it cannot say. */
const tzLocal = () => tzValid(Intl.DateTimeFormat().resolvedOptions().timeZone) || TZ_EXCHANGE;

/** "exchange" | "local" | an IANA zone → the IANA zone, or null if unknown. */
function tzResolve(choice) {
  if (choice === "exchange") return TZ_EXCHANGE;
  if (choice === "local") return tzLocal();
  return tzValid(choice);
}

/** Switch zones. Unknown values fall back to exchange time rather than throw. */
function tzSet(choice) {
  const zone = tzResolve(choice);
  // Stored canonical, so "asia/tokyo" and "Asia/Tokyo" select the same entry,
  // and New York by name is the exchange entry.
  if (!zone) choice = "exchange";
  else if (choice !== "exchange" && choice !== "local") choice = zone === TZ_EXCHANGE ? "exchange" : zone;
  tz.choice = choice;
  tz.zone = zone || TZ_EXCHANGE;
  TZ_FMT_CACHE.clear();
  tz.listeners.forEach((fn) => fn(tz));
  return tz.zone;
}

/** Run `fn(tz)` after every tzSet(). app.js registers before its startup
 *  tzSet(), so it hears that one too. There is no unsubscribe. */
const tzOnChange = (fn) => tz.listeners.push(fn);
/** Whether times are shown in exchange (New York) time. Compares zones, so
 *  "local" on a New York machine counts. */
const tzIsExchange = () => tz.zone === TZ_EXCHANGE;

/** An Intl formatter in the current zone, built once per zone and option set. */
function tzFmt(name, opts) {
  const key = `${tz.zone}|${name}`;
  let f = TZ_FMT_CACHE.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { ...opts, timeZone: tz.zone });
    TZ_FMT_CACHE.set(key, f);
  }
  return f;
}

/** Bar stamps arrive as "YYYY-MM-DDTHH:MM:SS" in UTC, with no suffix. */
const tzInstant = (stamp) => new Date(`${String(stamp).slice(0, 19)}Z`);

/** Wall-clock fields of `date` in the current zone, as numbers. */
function tzParts(date) {
  const out = {};
  for (const p of tzFmt("parts", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).formatToParts(date)) {
    if (p.type !== "literal") out[p.type] = Number(p.value);
  }
  return out;
}

/** Two-digit zero pad for clock fields. */
const tzPad = (n) => String(n).padStart(2, "0");

/** "14:05" */
function tzHM(date) {
  const p = tzParts(date);
  return `${tzPad(p.hour)}:${tzPad(p.minute)}`;
}

/** "14:05:09" */
function tzHMS(date) {
  const p = tzParts(date);
  return `${tzPad(p.hour)}:${tzPad(p.minute)}:${tzPad(p.second)}`;
}

/** "2026-11-23", the date on the reader's clock (not the session date). */
function tzYMD(date) {
  const p = tzParts(date);
  return `${p.year}-${tzPad(p.month)}-${tzPad(p.day)}`;
}

/** Minutes after local midnight. */
function tzMinutes(date) {
  const p = tzParts(date);
  return p.hour * 60 + p.minute;
}

/** "Mon, 23 Nov, 14:05", the intraday legend and tooltip stamp. */
const tzStamp = (date) => tzFmt("stamp", {
  weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
}).format(date);

/** Zone-name formatters, per zone and style. Building an Intl formatter costs
 *  far more than using one, and a tag is asked for on every legend and tooltip
 *  stamp. Keyed by zone, so a zone change needs no clearing. */
const TZ_NAME_FMT = new Map();
/** Intl's name for `zone` at `date` in a timeZoneName style ("short" →
 *  "EST", "shortOffset" → "GMT-5"); undefined if it gives none, and callers
 *  fall back with ||. */
function tzZoneName(date, zone, style) {
  const key = `${zone}|${style}`;
  let f = TZ_NAME_FMT.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: style });
    TZ_NAME_FMT.set(key, f);
  }
  return f.formatToParts(date).find((p) => p.type === "timeZoneName")?.value;
}

/** UTC offset of `zone` at `date`, in minutes ("GMT+5:30" → 330). */
function tzOffset(date, zone) {
  const name = tzZoneName(date, zone, "shortOffset") || "GMT";
  const m = /GMT([+-])(\d{1,2})(?::(\d{2}))?/.exec(name);
  return m ? (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3] || 0)) : 0;
}

/** Standard (winter) offset per zone and year: the lesser of January's and July's. */
const TZ_STANDARD = new Map();

/** Short zone name at `date`: "ET", "BST" in a London summer, "GMT+3" for a
 *  zone outside the picker's list. */
function tzTag(date = new Date(), zone = tz.zone) {
  const known = TZ_CHOICES.find((c) => c.zone === zone);
  if (known) {
    if (known.tags.length === 1) return known.tags[0];
    const y = date.getUTCFullYear();
    const key = `${zone}|${y}`;
    if (!TZ_STANDARD.has(key)) {
      TZ_STANDARD.set(key, Math.min(tzOffset(new Date(Date.UTC(y, 0, 1)), zone),
                                    tzOffset(new Date(Date.UTC(y, 6, 1)), zone)));
    }
    return known.tags[tzOffset(date, zone) > TZ_STANDARD.get(key) ? 1 : 0];
  }
  if (zone === "UTC" || zone === "Etc/UTC") return "UTC";
  return tzZoneName(date, zone, "short") || zone;
}

/** Picker entries: exchange first, the machine's own zone second. A zone set
 *  by URL that is not in the list is added, so the picker never lies. */
function tzOptions() {
  const local = tzLocal();
  const city = (zone) => zone.split("/").pop().replace(/_/g, " ");
  const opts = [{ value: "exchange", text: `New York — exchange (ET)` },
                { value: "local", text: `Local — ${city(local)} (${tzTag(new Date(), local)})` }];
  TZ_CHOICES.slice(1).forEach((c) => {
    const tags = c.tags.join("/");
    opts.push({ value: c.zone, text: c.label === tags ? c.label : `${c.label} (${tags})` });
  });
  if (!["exchange", "local"].includes(tz.choice) && !TZ_CHOICES.some((c) => c.zone === tz.choice)) {
    opts.push({ value: tz.choice, text: `${city(tz.zone)} (${tzTag()})` });
  }
  return opts;
}
