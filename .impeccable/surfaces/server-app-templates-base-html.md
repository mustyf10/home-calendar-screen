---
version: 1
slug: "server-app-templates-base-html"
primary_target: "server/app/templates/base.html"
related_targets: ["server/app/templates/display.html"]
---

# Surface brief: Kitchen web UI + e-ink panel

Scope: whole product visual redesign. Mode: Operate (web UI), glanceable Read (panel). Audience: household members editing on a phone/laptop; everyone glancing at the wall panel; self-hosters. Build path: code-led. Direction pinned by the user: Dribbble reference (warm orange recipe-box app) saved at `.impeccable/ref/dribbble-1800.png`.

Unresolved: none blocking. Panel is 1-bit 800x480, so the world reaches it only through type, shape and density.

## Direction contract

THESIS: A warm, rounded recipe-box app that feels like the kitchen it describes, refusing the stock grey Pico document look; the panel is its quiet black-and-white sibling.
OWN-WORLD: Peach-white ground, tinted warm neutrals, one committed tangerine (#c4400a, darkened from the reference's #e4531d family so white text passes AA) for primary action, the selected nav/category icon, badges and today; walnut (#3a1a12) for the brand mark and the one strongest header button. White cards, 18px radius (14px controls), soft walnut-tinted offset shadows. Drawn line icons, one stroke weight. System rounded/sans stack, 700 headings. Pill selected state in peach.
STORY: Everyone can find tonight's dinner and edit the week fast; recipes read as a browsable box of photo cards.
FIRST VIEWPORT: Desktop: 15.5rem (248px) left rail (walnut brand mark, tangerine "Add a recipe" button, "Plan" and "Manage" groups of drawn-icon links, peach pill for the current page) beside a content column with page title + count, actions on the right, then a category icon row over a photo-card grid (4 across). Phone: top bar with brand and horizontal pill nav; cards two-up.
FORM: Pinned user reference (not from the roll); signature move = the peach pill + tangerine "today" marker that recurs from nav to menu week to the panel's black today pill. seed: none (unavailable)
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
