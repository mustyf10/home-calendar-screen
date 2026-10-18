# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
- Household members who manage the family calendar, recipe collection and weekly dinner menu from a phone or laptop.
- Everyone in the home, who only glance at a wall-mounted kitchen e-ink panel to see today's calendar, tonight's dinner and (optionally) the weather.
- Other self-hosters: the repository is public, so people deploying their own instance are an audience too.

## Product Purpose
A self-hosted household calendar, recipe and dinner-menu app. A small web UI is where the household edits data; an 800×480 1-bit e-ink panel (fed by a Raspberry Pi 3B client polling `/display/frame.png`) shows it at a glance in the kitchen. Success: the panel is correct and legible without anyone touching it, and editing from a phone takes seconds.

## Positioning
Household-owned and self-hosted: data stays on a Raspberry Pi, the display is a quiet e-ink panel rather than a screen or a cloud app, and the whole thing deploys with one Docker Compose file.

## Operating Context
- Server: FastAPI, SQLAlchemy/SQLite, Jinja templates with HTMX, no JS build step. Runs in Docker on a Raspberry Pi 4 behind a reverse proxy.
- Display client: a thin Raspberry Pi 3B script that fetches a PNG every five minutes; quiet hours 23:00–06:00. No networking beyond that on the client.
- External services: Anthropic API (recipe handling) and optional WeatherAPI.com (forecast strip, off by default).
- Surfaces: web UI (calendar, recipes, menu, settings), the e-ink panel (`/display` HTML and its 1-bit PNG), and `/preview` (side-by-side HTML and PNG for QA, linked from Settings).

## Capabilities and Constraints
- The panel image is 800×480, mode "1" (pure black and white), static, read at a distance. The existing offline banner and the weather source credit are fixed elements that design changes must keep legible.
- No custom JS build pipeline; changes stay within server-rendered templates, CSS and HTMX.
- Settings and optional features (weather) are disabled by default; secrets never appear in browser output.

## Brand Commitments
- The user wants design inspiration taken from this reference: https://cdn.dribbble.com/userupload/9163030/file/original-06a2d6232479cdbb1763b37104e51d55.png?resize=3810x2857&vertical=center. It is recorded as a stated reference only. How it translates (and what survives the 1-bit panel) is decided in later design work.

## Evidence on Hand
- A working app with real UI in `server/app/templates` and a generated panel render (`/display/frame.png`, `/preview`).
- No testimonials, customers or usage metrics exist; none should be invented.

## Product Principles
- The panel is glanceable first: correct, legible at distance, and calm.
- Editing should be fast on a phone; the web UI is a tool, not a showcase.
- Optional features stay optional and off by default.
- Keep it simple to self-host: one Compose file, no build step, no required cloud account beyond the recipe API key.
