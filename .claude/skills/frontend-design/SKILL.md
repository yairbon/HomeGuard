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

| Token | Hex | Use |
|---|---|---|
| `background` | `#0B0F14` | Scaffold background — near-black, not pure black |
| `surface` | `#141B22` | Cards, app bar |
| `surfaceRaised` | `#1B242D` | Elevated cards, settings sections |
| `textPrimary` | `#EDEFF2` | Body text |
| `textMuted` | `#8A94A3` | Secondary/caption text |
| `safe` | `#00E6A8` | Disarmed, OK, success — **never** reuse for anything else |
| `alarm` | `#FF3D57` | Armed, triggered, alerts, destructive actions |
| `transitional` | `#FFB238` | Arming-in-progress only |
| `system` | `#4FB8FF` | Interactive elements (buttons, links) — kept distinct from status colors on purpose, so "clickable" never gets confused with "this is a status" |

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
