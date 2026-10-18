---
name: Kitchen
description: A warm, rounded recipe-box UI with a tangerine accent; its e-ink panel is the quiet black-and-white sibling.
colors:
  tangerine: "#c4400a"
  tangerine-hover: "#a8360a"
  walnut: "#3a1a12"
  walnut-hover: "#5a2a1c"
  peach: "#fbe7d8"
  peach-strong: "#f3d5bd"
  ink: "#2a1d17"
  cocoa: "#5b463b"
  clay-muted: "#7a665b"
  biscuit: "#e6d3c6"
  peach-white: "#fffaf6"
  rail-cream: "#fdf4ee"
  card-white: "#ffffff"
  hairline: "#f0e2d8"
  warning-amber: "#fff4d6"
  warning-line: "#f2dc9b"
  warning-ink: "#4a3500"
  danger: "#b42318"
typography:
  headline:
    fontFamily: "ui-rounded, 'SF Pro Rounded', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "1.75rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  title:
    fontFamily: "ui-rounded, 'SF Pro Rounded', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "1.05rem"
    fontWeight: 700
    lineHeight: 1.25
  body:
    fontFamily: "ui-rounded, 'SF Pro Rounded', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "ui-rounded, 'SF Pro Rounded', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "0.82rem"
    fontWeight: 600
  section:
    fontFamily: "ui-rounded, 'SF Pro Rounded', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "1.2rem"
    fontWeight: 700
    lineHeight: 1.25
  date:
    fontFamily: "ui-rounded, 'SF Pro Rounded', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "1.6rem"
    fontWeight: 700
    lineHeight: 1.1
  small:
    fontFamily: "ui-rounded, 'SF Pro Rounded', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "0.9rem"
    fontWeight: 400
    lineHeight: 1.4
  micro:
    fontFamily: "ui-rounded, 'SF Pro Rounded', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "0.7rem"
    fontWeight: 700
  panel-day:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "88px"
    fontWeight: bold
    lineHeight: 88px
  panel-day-compact:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "62px"
    fontWeight: bold
    lineHeight: 64px
  panel-weekday:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "24px"
    fontWeight: bold
    lineHeight: 28px
  panel-month:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "22px"
    fontWeight: normal
    lineHeight: 26px
  panel-body:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "18px"
    fontWeight: normal
    lineHeight: 20px
  panel-temperature:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "16px"
    fontWeight: bold
    lineHeight: 18px
  panel-pill:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "14px"
    fontWeight: bold
    lineHeight: 19px
  panel-warning:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "13px"
    fontWeight: bold
    lineHeight: 16px
  panel-small:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "12px"
    fontWeight: normal
    lineHeight: 14px
  panel-micro:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "11px"
    fontWeight: normal
    lineHeight: 14px
  panel-micro-long:
    fontFamily: "'DejaVu Sans', sans-serif"
    fontSize: "10px"
    fontWeight: normal
    lineHeight: 14px
rounded:
  xs: "5px"
  sm: "10px"
  md: "14px"
  lg: "18px"
  pill: "999px"
  bank-card: "3mm"
spacing:
  sm: "0.5rem"
  md: "1rem"
  lg: "1.25rem"
components:
  button-primary:
    backgroundColor: "{colors.tangerine}"
    textColor: "{colors.card-white}"
    rounded: "{rounded.md}"
    padding: "0.5rem 0.9rem"
  button-primary-hover:
    backgroundColor: "{colors.tangerine-hover}"
  button-walnut:
    backgroundColor: "{colors.walnut}"
    textColor: "{colors.card-white}"
    rounded: "{rounded.md}"
    padding: "0.5rem 0.9rem"
  button-walnut-hover:
    backgroundColor: "{colors.walnut-hover}"
  button-secondary:
    backgroundColor: "{colors.peach}"
    textColor: "{colors.walnut}"
    rounded: "{rounded.md}"
  button-outline:
    backgroundColor: "transparent"
    textColor: "{colors.cocoa}"
    rounded: "{rounded.md}"
  card:
    backgroundColor: "{colors.card-white}"
    rounded: "{rounded.lg}"
    padding: "1.25rem"
  chip:
    backgroundColor: "{colors.rail-cream}"
    textColor: "{colors.cocoa}"
    rounded: "{rounded.pill}"
  input:
    backgroundColor: "{colors.card-white}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
  nav-item-current:
    backgroundColor: "{colors.peach}"
    textColor: "{colors.walnut}"
    rounded: "{rounded.md}"
---

# Design System: Kitchen

## Overview

**Creative North Star: "The Recipe Box"**

A household tool that looks like the kitchen it serves: peach-white surfaces, white cards lifted by a soft shadow, one committed tangerine for the actions and states that matter, a deep walnut for the single strongest header action, and drawn line icons. Food photography carries the colour in the recipe grid, so the chrome stays calm. It was taken from the user's chosen Dribbble reference (a warm orange recipe app) and adapted to the app's real content.

The same shapes recur on the e-ink panel in black and white: a black rounded date block, outlined pill section headers and black rounded "today" markers where the web UI uses tangerine ones. The panel keeps its DejaVu Sans, 1-bit, fixed 800×480 layout.

**Key Characteristics:**
- Light only (`data-theme="light"`; Pico's dark tokens would break the palette), warm neutrals tinted toward orange, never grey.
- Tangerine is reserved for primary action, current day and the unread badge.
- 14–18px radii, pill chips and nav states, soft offset shadows.
- Rail navigation with "Plan" and "Manage" groups on desktop; a top bar with a scrolling pill nav on phones.

## Colors

A peach-white ground, cream rail, white cards, one saturated tangerine and one deep walnut. Every colour is a custom property in the token block at the top of `server/app/static/app.css`; nothing outside that block uses a raw colour. The one exception is `<meta name="theme-color" content="#fdf4ee">` in `base.html` (Rail Cream), because a meta tag cannot read a CSS variable. Translucent tints (shadows, focus ring, the time pill) are derived from a token with `color-mix()`.

### Primary
- **Tangerine** (`--accent`, #c4400a): primary buttons, the "Add a recipe" button, today's date block, badges, links, focus ring, the current nav and category icon. White text on it is 5.1:1.
- **Tangerine Hover** (`--accent-hover`, #a8360a): hover and pressed state of tangerine buttons.
- **Walnut** (`--walnut`, #3a1a12): the brand mark tile, the strongest header action (Pico's `.contrast` button: "Shopping list" on Menu, "Menu" on Shopping, "Edit" on a recipe, "Screen preview" on Settings), text on peach fills (including the links in a menu row's recipe pick list and every menu-row link hover) and selection text.
- **Walnut Hover** (`--walnut-hover`, #5a2a1c): hover state of walnut buttons.

### Neutral
- **Peach** (`--peach`, #fbe7d8) and **Peach Strong** (`--peach-strong`, #f3d5bd): current nav item, secondary buttons, today's row, the empty-photo tile, step numbers, selection.
- **Ink** (`--ink`, #2a1d17): body and headings.
- **Cocoa** (`--cocoa`, #5b463b): nav links, chips, outline buttons and decorative icons (no-photo tile, time and fact pills); 8.8:1 on white.
- **Clay Muted** (`--muted`, #7a665b): secondary text and placeholders; 5.4:1 on white, 5.2:1 on the ground, 4.5:1 on peach. Not used on Peach Strong.
- **Biscuit** (`--field-line`, #e6d3c6): input borders and the outline button border.
- **Peach White** (`--ground`, #fffaf6) page ground, **Rail Cream** (`--rail`, #fdf4ee) rail and chips, **Card White** (`--surface`, #ffffff) cards and text on tangerine or walnut, **Hairline** (`--line`, #f0e2d8) dividers.
- **Warning Amber** (`--amber`, #fff4d6) with **Warning Line** (`--amber-line`, #f2dc9b) and **Warning Ink** (`--amber-ink`, #4a3500) for warnings; **Danger** (`--danger`, #b42318) for errors.

### Named Rules
**The One Orange Rule.** Tangerine marks what the user can act on or what is today; it is not a decorative fill. Counts, decorative icons and selected-category text are cocoa, clay or walnut; only the icon of the selected nav item or category turns tangerine.

## Typography

**Display, Body, Label Font:** the system rounded stack (`ui-rounded`, SF Pro Rounded, then system-ui). One family, no webfont, so it works offline on a Pi.

**Character:** friendly and legible at kitchen distance, with weight (600/700) doing the hierarchy rather than size alone.

### Hierarchy
- **Headline** (700, 1.75rem, 1.2): page titles.
- **Title** (700, 1.05rem, 1.25): recipe card titles (at every width), h3 section titles.
- **Body** (400, 1rem, 1.5): content; tabular numerals are on.
- **Label** (600, 0.82rem): chips, type labels, rail group labels. Form labels use Body.
- **Section** (700, 1.2rem, 1.25): section headings (h2) and the brand name. **Date** (700, 1.6rem): the day number in a menu date block.
- **Small** (0.9rem): secondary lines, compact buttons, fact pills. **Micro** (700, 0.7rem, uppercase): the "Today" tag.

### Panel type (DejaVu Sans, px)
Fixed pixel sizes for the 800×480 frame, never thinner than the 1-bit threshold allows: **Day** 88 (62 with weather), **Weekday** 24 (22 with weather), **Month** 22 (18 with weather), **Body** 18 (agenda, dinners, month grid), **Temperature** 16, **Pill** 14 (section headers, grid weekdays, "Weather unavailable"), **Warning** 13, **Small** 12 (weather heading and labels), **Micro** 11 and 10 (the four-cell weather probability line and long hour labels, carried over from the original strip).

### Named Rules
**The Weight Over Size Rule.** Distinguish levels with weight and colour first; headings are balanced and tracked at -0.015em. No eyebrow labels above headings: a recipe's type sits below its title.

## Layout

A two-column shell at ≥56rem: a 15.5rem rail (full height, cream), then a content column capped at 76rem. Below that, a sticky top bar holds the brand, the add button and a horizontally scrolling pill nav that scrolls the current page into view. Every page opens with the same `.page-head` (the `page_head` macro in `templates/_ui.html`): title, a quiet `.page-meta` line (count, date range or source) and actions on the right. Recipe cards are two-up on phones (below 40rem) and `repeat(auto-fill, minmax(13.5rem, 1fr))` with 1.1rem gaps above that (four across on a desktop). A recipe page is a 22rem photo-and-ingredients column beside a method card at ≥64rem. Pages use 1rem on phones and 2–2.5rem on desktop. The week menu is a stack of row cards with a 4.6rem date block.

## Elevation & Depth

Soft, warm-tinted shadows on white cards over a peach-white ground; depth never comes from borders.

### Shadow Vocabulary
- **Card** (`--shadow`: `0 1px 2px` walnut 6%, `0 6px 18px -6px` walnut 14%): cards, list cards, menu rows, fact pills.
- **Primary lift** (`--shadow-lift`: white 25% inset top, `0 4px 10px -4px` tangerine 55%): tangerine buttons.
- **Walnut lift** (`--shadow-walnut`: white 12% inset top, `0 4px 10px -4px` walnut 55%): walnut buttons.

### Named Rules
**The Warm Shadow Rule.** Shadows are tinted with walnut or tangerine, never neutral black.

### Motion
One authored moment: cards and menu rows rise 6px on load with `--ease-out` (`cubic-bezier(.16, 1, .3, 1)`), 0.35s, translate only, so content is visible from the first frame. Off under `prefers-reduced-motion`.

## Shapes

Rounded throughout: 5px checkboxes (so multi-select never reads as a radio), 10px small tiles (brand mark, list icon tile, file button), 14px controls, 18px cards, fully round chips, badges, nav pills, fact pills and the search field. The preview's calibration outline uses a real bank card's 3mm corner. The panel uses the same scale: 18px date block, 14px weather box, 10px today cell and warning box, pills for section headers and the today row. Photos fill the top of a card and are clipped by its radius. Icons are drawn line icons, one 1.75 stroke, from `templates/_icons.html`; an unknown icon name raises instead of drawing nothing.

## Components

### Buttons
- **Shape:** 14px radius, 600 weight, icon plus label with a 0.35rem gap.
- **Primary:** tangerine fill, white text, primary lift; hover Tangerine Hover.
- **Walnut (strongest header action):** Pico's `.contrast`: walnut fill, white text, walnut lift; hover Walnut Hover. At most one per page.
- **Secondary:** peach fill with walnut text; hover Peach Strong.
- **Outline:** transparent with a Biscuit border and Cocoa text; hover fills peach with walnut text.
- **Focus:** a 2px tangerine outline with 2px offset on everything focusable.

### Chips
- Rail Cream pill with a Hairline ring and Cocoa text; core tags are peach, bold and walnut. Hover fills peach. The active tag filter is a core chip with a drawn close icon, labelled "Clear tag filter".

### Cards / Containers
- 18px radius, white, Card shadow. Recipe cards have a 4:3 photo (or a peach tile with a cocoa type icon when there is none) and a time pill (cocoa clock plus minutes) at the photo's bottom-left; the title comes first, then the type.
- **List card:** one white card of hairline-separated rows (imports, batch recipes, location results; the `list_row` macro in `templates/_ui.html`), each with a cream icon tile, a bold title, a muted meta line and actions on the right.
- **Shopping aisles:** a grid of small white cards, one per aisle.

### Inputs / Fields
- White fill, 1px Biscuit border, 14px radius (search is pill and joined to its button). Focus border is tangerine with a tangerine ring. Placeholders are Clay Muted. File inputs show a peach "Choose file" button on a cream field.

### Navigation
- Rail: walnut brand mark, tangerine "Add a recipe", then two labelled groups: **Plan** (Menu, Recipes, Shopping) and **Manage** (Import, Tags, Settings). Links are icon plus label; the current page is a peach pill with walnut text and a tangerine icon. On phones the groups become one scrolling pill row; the group labels stay available to screen readers.
- Category row on Recipes: icon over label; the selected category is a peach tile with walnut text and a tangerine icon.
- Week switchers (Menu, Shopping): outline Previous / Next buttons with drawn chevrons, the walnut cross-link on the right.

### Today marker (signature)
- The current day is a tangerine date block (weekday, day, month and a small "Today" tag) on a peach row in the week menu; its accessible name reads "Sun 18 Oct · Today".
- The panel uses the same idea in black: a black rounded date block for the header, a rounded black cell in the month grid, and a black pill row in the dinner list.

### E-ink panel (`display.html`)
- Pure #000/#fff only (the PNG is thresholded at 128), DejaVu Sans, nothing thinner than 2px.
- Left: the black date block, the month grid (bold day plus a dot for days with events; today is a black rounded inner block), then the optional weather strip in a 2px rounded outline box.
- Right: outlined pill headers ("AGENDA", "DINNERS · range"), agenda lines, then seven dinner rows with today as a black pill.
- Bottom-right: warnings in a 2px outlined rounded box that stays right of the offline stamp's area and wraps to a second line; the right column tightens slightly whenever a warning is shown. The offline stamp is drawn bottom-left by `display/stamp.py`.

## Do's and Don'ts

### Do:
- **Do** keep the page ground peach-white and the cards white.
- **Do** use tangerine only for primary actions, the current day and badges; walnut for at most one header action per page.
- **Do** use the drawn icons from `_icons.html` at one stroke weight.
- **Do** keep the panel pure black and white with rounded "today" blocks; no greys.

### Don't:
- **Don't** add a dark theme; the app is light only.
- **Don't** use emoji or text glyphs as icons.
- **Don't** put coloured side stripes on cards or rows.
- **Don't** add a raw colour outside the token block in `app.css`.
- **Don't** change the panel geometry or font (800×480, DejaVu Sans) without re-checking the weather strip, warning and offline stamp.
