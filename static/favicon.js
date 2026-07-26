/* Shared live-favicon logic — same states on every page.
   Include before the page script; call wtbFavicon.update(status). */
"use strict";
(function () {
  const v = (name) => getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();

  function state(s) {
    if (!s.monitoring)
      return { key: "paused", color: v("--muted"), emoji: "⏸" };
    if (s.outage && s.outage.layer !== "degraded")
      // hard outage: red + white X glyph, as loud as the pause icon
      return { key: "outage", color: v("--crit"), emoji: "⛔" };
    // quality ladder, evenly spread: green -> yellow -> orange -> vermilion
    if (s.outage)
      return { key: "degraded", color: "#f08c00", emoji: "🟠" };
    if (s.quality != null && s.quality < 50)
      return { key: "poor", color: "#e0561f", emoji: "🔴" };
    if (s.quality != null && s.quality < 80)
      return { key: "fair", color: "#e2c500", emoji: "🟡" };
    return { key: "good", color: v("--ok"), emoji: "🟢" };
  }

  let last = null;
  function apply(fs) {
    const el = document.getElementById("favicon");
    if (!el) return;
    const key = fs.key + fs.color;
    if (key === last) return;
    last = key;
    // The brand mark (dark tile + pink pulse line) is always drawn; the
    // brand's trailing dot carries the live status color. The tab title still
    // shows the status emoji, so pause/outage stay distinct there.
    const svg =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">' +
      '<rect width="16" height="16" rx="3.5" fill="#0d0f14"/>' +
      '<polyline points="1.5,9 4,9 5.5,5 7.5,12 9.5,6.5 10.8,9 12.4,9" ' +
      'fill="none" stroke="#e04a92" stroke-width="1.6" ' +
      'stroke-linecap="round" stroke-linejoin="round"/>' +
      '<circle cx="13.3" cy="9" r="2" fill="' + fs.color + '" ' +
      'stroke="#0d0f14" stroke-width="0.7"/></svg>';
    el.href = "data:image/svg+xml," + encodeURIComponent(svg);
  }

  window.wtbFavicon = { state, apply, update: (s) => apply(state(s)) };
})();
