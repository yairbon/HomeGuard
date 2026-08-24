---
name: frontend-design
description: HomeGuard's actual design system — use before building or changing any Flutter UI, so new screens stay consistent with the established dark control-panel aesthetic instead of drifting toward generic Material defaults.
---

# HomeGuard Frontend Design

This codifies the design system already built into the app (see
`app/lib/theme/app_theme.dart`, `app/lib/widgets/`). Don't reinvent it —
extend it. If a new screen needs a color/type/motif this doesn't cover,
add it here once decided, don't just pick something that "looks fine" in
isolation.

## Why this direction

HomeGuard is a real embedded hardware security system (ESP32, sensors,
buzzers), not a generic consumer app. The design leans into that: a dark
control-panel aesthetic, monospace readouts for status/data, and a
signature motif (a circuit-trace ring — closed loop = armed/protected,
broken = disarmed) that's a literal metaphor for how the hardware works,
not decoration bolted on. Avoid anything that reads as default-Material
("AI-generated generic") — no default purple, no unstyled `ElevatedButton`s
without the theme applied.

## Palette (`AppColors` in `app_theme.dart`)

These are copied from `AppColors` and must be kept in step with it — the
code is the source of truth, and this table has been wrong before.

| Token | Hex | Use |
|---|---|---|
| `background` | `#0E1310` | Scaffold background — near-black with a green bias, not pure black |
| `surface` | `#141A16` | Cards, app bar |
| `surfaceRaised` | `#1C241F` | Elevated cards, settings sections |
| `textPrimary` | `#ECEFE7` | Body text |
| `textMuted` | `#8B9A8E` | Secondary/caption text |
| `trace` | `#3E4A40` | Hairlines, inactive circuit traces, dividers |
| `safe` | `#43C285` | Disarmed, OK, success — **never** reuse for anything else |
| `alarm` | `#E14B3C` | Armed, triggered, alerts, destructive actions |
| `transitional` | `#E2953A` | Arming-in-progress only |
| `system` | `#4198D1` | Interactive elements (buttons, links) — kept distinct from status colors on purpose, so "clickable" never gets confused with "this is a status" |

The neutrals are deliberately hue-shifted toward the green of `safe` rather
than being flat grey. That is what stops the panel reading as a generic dark
theme.

Don't introduce a new color without a reason tied to meaning (status vs.
interactive vs. text hierarchy) — that discipline is what keeps the
2-color status system (safe/alarm) readable at a glance.

## Typography

- **IBM Plex Mono** (`AppTheme.monoStyle()`) — status readouts, numbers,
  timestamps, section eyebrows (e.g. "RECENT ACTIVITY"). Reinforces
  "engineered system."
- **IBM Plex Sans** (default via `google_fonts` text theme) — body copy,
  labels, buttons. Don't use mono for paragraph-length text — it's for
  data/status, not prose.
- **Big Shoulders Display** (`AppTheme.displayStyle()`) — the condensed face
  used for large headline numerals and screen titles. Sparingly; it carries
  weight precisely because it's rare.

All three are **bundled as assets** in `app/google_fonts/`, not fetched at
runtime — an alarm panel has to render on a device with no route to the open
internet. The filenames encode the match (`<Family>-<Weight>.ttf`), so
renaming one silently sends that face back to a network download.

## Signature motif: the circuit ring

`AlarmStatusIndicator` (`widgets/alarm_status_indicator.dart`) is the one
thing this app should be visually remembered by. Closed ring = protected,
broken ring = not. If you add a new status-representing widget elsewhere,
ask whether it should reuse this metaphor (a ring/loop) rather than a
generic icon — consistency of the metaphor matters more than novelty.

## Practical rules for new screens

1. Every screen gets its own `Scaffold` + `AppBar` (see `main_shell.dart`
   for how bottom-nav tabs each keep their own).
2. Cards/sections use `surfaceRaised` with `BorderRadius.circular(10-12)`,
   not raw `Card` defaults.
3. Status text (ARMED/DISARMED/ONLINE/OFFLINE-style words) is always
   mono, uppercase, letter-spaced (~1-2), and colored by meaning from the
   palette above — never plain black/white text.
4. Buttons: primary action = `system` blue filled; destructive/disarm =
   `alarm` red filled; secondary = outlined, `textMuted`.
5. Empty states get a muted, centered one-liner (see `EventLogScreen`'s
   "No events yet") — never a blank screen with no explanation.
6. Before shipping a new screen, sanity-check it against 3 and 4 —
   details are 250-word max, don't write more than needed.
