# Merge Import into Add a recipe; fill recipes in the background

## Problem (observed on the Pi, v0.4.1)

1. Saving a recipe hangs 8–19 s: `POST /dishes` awaits `suggest_tags` before redirecting.
2. "Add a recipe" stores a link but never reads it. Only `/imports` extracts, behind a review step.
3. TikTok fetching is broken: yt-dlp ≥ 2026.08 needs `curl_cffi` for TikTok impersonation and the image lacks it ("Unexpected response from webpage request"). With `curl_cffi` installed the pinned yt-dlp downloads the video and the full caption. The web-page fallback also misses TikTok captions: they live in `<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">` (`__DEFAULT_SCOPE__["webapp.video-detail"].itemInfo.itemStruct.desc`, cover at `.video.cover`), with no `og:description`.

## Decisions (confirmed with the user)

- **One page.** "Add a recipe" takes a link, caption/recipe text, screenshots, and/or typed details. Save is immediate.
- **Link-only save.** The recipe appears at once with a placeholder title (e.g. "TikTok recipe") and an "Importing…" banner. Title, type, description, ingredients, steps, servings, time, photo and tags fill in within a minute or two. There is no review step.
- **Typed fields win.** Background filling only fills fields that are empty when it finishes. A placeholder title counts as empty. Type comes from extraction only when the title was a placeholder. AI tags are added to the user's tags, never replacing them.
- **Import folds into Add a recipe.**
  - "Import" leaves the sidebar, and `/imports` and `/imports/new` redirect to `/dishes/new`.
  - Handwritten batches stay: a link from Add a recipe leads to `/imports/batch`.
  - Batch drafts (and any legacy unsaved drafts) still go through `/imports/{id}` review, because one photo stack becomes several recipes.
  - Drafts awaiting review show as a banner listing them on Add a recipe and on Recipes. The drafts-count badge moves to the "Add a recipe" button.
- **Failure.** If filling fails, the recipe page shows a "Couldn't read the link" banner. The banner has a caption textarea and a screenshots input with "Try again", plus "Dismiss". Recipes lists failed imports in a banner until they are resolved or dismissed.
- **Existing recipes with a link but no ingredients or steps** get a "Fill in from the link" button on their page. This also covers the user's "Crispy garlic chicken wraps".

## Design

- **One queue.** Reuse `IngestJob` and the existing worker (`ingest/worker.py`), with no schema migration. A job with `dish_id` set at creation is an *enrichment job*.
  - Statuses: `queued` → `fetching` → `transcribing` → `extracting` → `ready` when filled, or `failed`. `discarded` means the user dismissed it.
  - `ready` with `dish_id` already means "saved", so existing draft queries (`dish_id IS NULL`) stay correct.
- **Every successful `POST /dishes` enqueues one enrichment job.** It carries `source_url` from the link field, `user_text` from the caption field, and `upload_paths` from the screenshots. The request does no network or Claude work.
  - A job with a link, text or screenshots runs the normal fetch/transcribe/extract path, then fills blanks on the dish.
  - A job with none of those is **tags only**: `suggest_tags` from the dish's current fields, then merge.
- **Placeholder title** comes from a pure function of the platform:
  - "TikTok recipe", "Instagram recipe", "YouTube recipe";
  - "Recipe from <host>" for web links;
  - "Recipe from photos" or "Recipe from text" for screenshots or pasted text.

  Validation: the title is required only when no link, text or screenshots are given.
- **Filling the dish** (worker, fresh read of the dish):
  - The extracted title replaces the placeholder.
  - Description, ingredients, steps, servings and minutes fill only if empty.
  - The photo is set only if the dish has none, using the source cover/thumbnail first, then the first keyframe. Screenshots are never the dish photo.
  - Tags are the current tags plus `clean_tags(extracted tags)`.
  - `source_url` is replaced by the canonical URL if no other dish has it.
  - If the canonical URL belongs to *another* dish, the job fails with "Already saved as <title>" (with a link).
- **Failures:**
  - Fetch got nothing and there is no text or screenshots → `failed` with the existing `FETCH_FAILED` text.
  - `is_recipe: false` → `failed` with "This doesn't look like a recipe".
  - Claude unavailable or erroring (`fallback_extracted`): fill blanks from the scraped fallback, then mark `failed` with that note so the user can retry. Mark fallback results explicitly; don't string-match notes.
- **Recipe page:**
  - An active job shows an "Importing… (status)" banner that polls `/dishes/{id}/import/status` every 2 s and sends `HX-Refresh` when the job finishes. This is the same pattern as `ingest/_status.html`.
  - A failed job shows the retry banner: `POST /dishes/{id}/import/retry` appends text and screenshots and requeues. `POST /dishes/{id}/import/dismiss` sets the job to `discarded`.
  - `POST /dishes/{id}/import` starts a fill from `dish.source_url` when no job is active.
- **Recipes list:** cards with an active job show an "Importing…" pill; failed jobs get a banner at the top with links.
- **TikTok:**
  - Add `curl_cffi` (pinned) to `requirements.txt`.
  - `web.parse_page` falls back to TikTok's rehydration data for the caption (as `og_description`) and the cover (as `og_image`) when meta tags are missing.

## Out of scope

Edit-page extraction, batch-flow changes beyond relinking, and schema migrations.

## Verification

- TDD at the seams:
  - `POST /dishes` never calls Claude or the network and enqueues the right job.
  - The worker fills blanks and respects typed fields.
  - Failure, retry and dismiss work.
  - The TikTok page fallback works (fixture HTML).
  - Redirects work and the sidebar has no Import item.
- Then the full suite and the browser at 1440/390.
- Then a live end-to-end run on the Pi with the user's TikTok link, after deploy.
