# Theme Service

This app's theming comes from the shared **theme-service** — currently on version `0.3.0`.
The files in this folder are vendored copies of the source of truth; do not hand-edit generated
token files (`theme.css`, `effects.css`, `theme-init.js`, `themes.index.json`), and do not hardcode
colors — consume the theme tokens (`var(--…)`). `bridge.css` is app-specific glue and is safe to edit.

## For agents working in this repo
This repo **already uses the theme-service** (see History below). Use the **theme-service skill**
(or its `AGENTS.md`) for any theme work here — don't improvise, and don't re-apply from scratch.
- Update to latest:  "Update this repo to the latest theme-service version."
- Add/change themes:  see the theme-service repo's `CREATING-THEMES.md`.
Rules: keep WCAG AA 2.2 · default theme is Rink Classic · the selector uses the **external**
`theme-init.js` (never inline scripts — strict CSP blocks them).

## Applied configuration (current decisions on record)
- Component styling: **colors-only** — the app's components are unchanged; a `bridge.css` maps the
  theme-service tokens onto the app's existing custom properties (`--page`, `--surface`, `--ink`,
  `--accent`, `--glow`, …).
- Fonts: **kept app fonts** (system-ui); theme fonts not applied.
- Selector: **app-managed** `<select id="themeSelect">` in Settings → Theme, populated with Auto +
  all 16 themes. `setTheme()` in `app.js` sets `data-theme` + persists `localStorage.theme` and
  re-renders the charts; `theme-init.js` applies the saved theme pre-paint on both pages.
- Existing themes: **removed** (the old `themes.css` neon/midnight/daylight replaced).
- Effects: per-theme **glow** (via `--glow` scaling with `--glow-strength`) and the **retro grid
  backdrop** (`.fx-grid` on `<body>`, auto-hides on "No Background" variants). App's own neon
  scrollbar kept (re-themed via `--accent`).
- **Data/status colors are NOT themed** (deliberate): `--series-1..6`, `--ok/--warn/--serious/--crit`,
  `--lay-link/lan/inet`, `--idle` are fixed in `bridge.css`. The theme-service palette has only 4
  accents and no amber/red, so the good→degraded→down status coding and the 6 distinct chart series
  are kept as a fixed, dual-mode-legible set (readable on both dark and light themes).

## History
<!-- Append one entry per apply/update. Most recent last. Never edit past entries. -->
- `2026-07-26` — Applied theme-service `v0.3.0` (vanilla). Replaced the 3 hand-written themes with all
  16 built-in themes via a token `bridge.css`; colors-only depth, kept fonts; glow + grid effects on;
  status/chart palette kept fixed per user decision.
