---
name: canvas-design
description: Generate standalone graphics for HomeGuard — a course-submission poster, README banner, or a printed quick-reference card for the physical keypad — as PNG/PDF/SVG output, using the project's actual design system rather than a generic template.
---

# Canvas Design (HomeGuard)

For producing a finished graphic asset (not a UI screen — those go
through `frontend-design` instead). Typical HomeGuard use cases:

- A poster/cover graphic for the course submission
- A README banner
- A printed "quick reference" card near the physical keypad (arm/disarm
  instructions, emergency number) that matches the app's visual identity
- A one-page architecture diagram for a presentation

## Ground every graphic in this project's actual identity

Don't design from a blank slate — reuse what's already established (see
`frontend-design` skill and `app/lib/theme/app_theme.dart`):

- Background `#0B0F14`, IBM Plex Mono for headings/data, IBM Plex Sans
  for body text, the safe-green/alarm-red/system-blue palette.
- The circuit-ring motif is the one recognizable visual element — a
  poster or banner should feature it (or a clear variant of it), not a
  generic shield/lock icon.
- Content should come from the real project: the actual BOM, the actual
  partner split (Guy — perimeter/ultrasonic/door; Yair — physical
  defense/vibration/keypad), the actual must-have requirements — see
  `README.md` at the project root. Don't invent placeholder copy when
  real content exists.

## How to actually produce the graphic

1. Build it as a single self-contained HTML file (inline CSS, no external
   fonts/CDNs — use a system-font fallback stack or embed IBM Plex via a
   data URI if the exact typeface matters) sized to the target dimensions
   (e.g. a poster at print resolution, a banner at a wide aspect ratio).
2. Render/preview it (the Artifact tool renders HTML directly) and
   iterate on layout before finalizing.
3. For a PNG/PDF deliverable rather than a live page, convert the
   finished HTML/SVG — check what's available in this environment first
   (a headless browser screenshot, or a PDF-export skill/tool) rather
   than assuming a specific converter exists.

## Restraint

One bold, deliberate choice per graphic (the circuit-ring as a large
background element, say) — not five competing decorative elements. A
course-submission poster needs to read clearly at a glance, not impress
with density. Match complexity to the format: a small reference card
needs way less visual incident than a full poster.
