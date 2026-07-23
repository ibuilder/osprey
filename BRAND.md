# Osprey — Brand Kit v1

**Product:** Osprey · **Tagline:** *The foreman that never sleeps.*
**Org / packages:** `github.com/ospreyhq` · npm `@ospreyhq/*` · PyPI `osprey-core`
**One-liner:** Open-source background agent that watches every source on a construction/RE project and surfaces the one thing to act on now.

> Drop this file in the repo root as `BRAND.md`. Colors and type below are copy-paste ready for Tailwind, CSS variables, and the Tauri clients.

---

## The mark
Raised osprey wings (the bird's signature bent-wing hover) over an **ember core** — it watches the whole field, then strikes the one priority. It also reads as an upward chevron: *what rises to the top of the list.* Two-tone by design: **Ink** wings/head = the watch, **Ember** body = the signal. Holds down to 16 px.

---

## Color tokens

| Token | Hex | Role |
|---|---|---|
| `ink` | `#0E1A2B` | Primary — text, UI chrome, the "always-on" dark |
| `ember` | `#FF6A2B` | Accent — priority, the strike, primary CTA |
| `sky` | `#3E7CB1` | Secondary — info, links, calm |
| `mist` | `#F6F7F9` | App background / surface |
| `line` | `#E4E7EC` | Borders, dividers |
| `muted` | `#667085` | Secondary text |
| `white` | `#FFFFFF` | Cards, reversed logo |

**Priority semantics — reserved, use ONLY for hotlist buckets (never as decoration):**

| Token | Hex | Bucket |
|---|---|---|
| `prio-red` | `#E5484D` | 🔴 Act today |
| `prio-amber` | `#F5A623` | 🟠 This week |
| `prio-yellow` | `#EAB308` | 🟡 Watch |
| `prio-green` | `#30A46C` | Done / cleared |

Keep Ember (brand) visually distinct from the priority reds/ambers: Ember is chrome/CTA; the R/A/Y set means *priority level* and nothing else.

### CSS variables
```css
:root{
  --ink:#0E1A2B; --ember:#FF6A2B; --sky:#3E7CB1;
  --mist:#F6F7F9; --line:#E4E7EC; --muted:#667085; --white:#FFFFFF;
  --prio-red:#E5484D; --prio-amber:#F5A623; --prio-yellow:#EAB308; --prio-green:#30A46C;
}
```

### Tailwind (`tailwind.config.js`)
```js
theme:{ extend:{ colors:{
  ink:'#0E1A2B', ember:'#FF6A2B', sky:'#3E7CB1',
  mist:'#F6F7F9', line:'#E4E7EC', muted:'#667085',
  prio:{ red:'#E5484D', amber:'#F5A623', yellow:'#EAB308', green:'#30A46C' },
}}}
```

---

## Typography (all SIL OFL — free to bundle & ship)

| Use | Family | Where |
|---|---|---|
| Display / wordmark / headings | **Space Grotesk** | engineered, geometric — the brand voice |
| Product UI / body | **Inter** | legible workhorse for the hotlist UI |
| Data / scores / code | **JetBrains Mono** | tabular data, IDs, scores; dev-tool signal |

TTFs are bundled in `./fonts/`. In the Tauri/web client, self-host them (don't hot-link Google Fonts — keeps the app offline-capable and private):
```css
@font-face{ font-family:'Space Grotesk'; src:url('/fonts/SpaceGrotesk.ttf') format('truetype'); font-weight:300 700; font-display:swap; }
@font-face{ font-family:'Inter'; src:url('/fonts/Inter.ttf') format('truetype'); font-weight:100 900; font-display:swap; }
@font-face{ font-family:'JetBrains Mono'; src:url('/fonts/JetBrainsMono.ttf') format('truetype'); font-weight:100 800; font-display:swap; }
```
**Type scale (suggested):** display 40/600 · h1 28/600 · h2 20/600 (Space Grotesk); body 15–16/400, label 13/500 (Inter); data 13–14/500 (JetBrains Mono). Wordmark tracking is slightly tight (−0.5).

---

## Logo files (`./logo/` = SVG source of truth, `./png/` = raster exports)

| File | Use |
|---|---|
| `osprey-logo-horizontal.svg` | Primary lockup, light backgrounds |
| `osprey-logo-horizontal-reversed.svg` | On Ink / dark backgrounds |
| `osprey-mark.svg` | Symbol only (two-tone) — social avatar, loading states |
| `osprey-mark-mono-ink.svg` / `-white.svg` | Single-color contexts, stamps, watermarks |
| `osprey-icon.svg` + `png/osprey-icon-1024.png` | App icon (rounded navy tile) — desktop, iOS, Android, store listings |
| `png/favicon.ico` (+ 16/32/64) | Web favicon |
| `osprey-wordmark.svg` / `-white.svg` | Wordmark alone (text is outlined — no font needed) |

The wordmark is outlined to vector paths, so every SVG renders identically with **no font install required** and stays editable as shapes in Figma/Illustrator.

### Usage rules
- **Clear space:** keep padding ≥ the height of the mark's ember core on all sides.
- **Min size:** lockup ≥ 120 px wide; symbol ≥ 16 px.
- **Do:** Ink or reversed lockup on solid Ink/Mist/white; keep the ember core ember.
- **Don't:** recolor the wings to ember, stretch, add shadows/gradients, box the lockup on busy photos, or swap the typeface.

---

## Assets manifest
```
osprey-brand/
├── osprey-brand-board.png        # one-look overview (share this)
├── BRAND_KIT.md                  # this file
├── logo/                         # 8 editable SVGs (source of truth)
├── png/                          # icon 1024/256, favicons + .ico, lockups, wordmark, mark 512
└── fonts/                        # Space Grotesk, Inter, JetBrains Mono (OFL) + notice
```

*Brand v1 — deliberately tight so it's easy to apply and easy to evolve. Ink `#0E1A2B` · Ember `#FF6A2B`.*
