# Shopping list: automatic background rebuild

## Context
Today a saved shopping list (`shopping_lists` row per Sat–Fri week) only rebuilds when someone presses Rebuild; a stale list shows "Out of date — Rebuild". Goal: keep the current and next week's saved lists up to date automatically after menu/recipe edits, with a 5-minute quiet period, a visible pending state, one retry on failure, and a "Changed since last list" section.

Confirmed decisions (grill session):
| # | Decision |
|---|---|
| Trigger | Background rebuild after changes; nothing on page open |
| Quiet period | 5 min with no further change to that week |
| Scope | Only `week_start(today)` and `week_start(today)+7`, only if a list is already saved |
| Change sources | Menu changes for that week, and edits to a recipe on that week's menu. **Pantry edits do not auto-trigger** (they keep the existing "Out of date — Rebuild" banner) |
| Pending UI | Old list + "Updating shortly…" + "Rebuild now"; page refreshes itself every 10 s until done |
| Failure | Keep old list; "Couldn't update: <reason>" + Rebuild; retry once after 15 min; a new edit restarts the cycle |
| Diff | "Changed since last list": added, removed, quantity changed; compared by item name in code; shown until the next rebuild |

Code facts (read this session):
- `server/app/shopping/service.py`: `week_dinners(session, start)`, `pantry_text`, `fingerprint(session, start)` = sha256 of sorted `"{date}|{dish.id}|{dish.updated_at}"` lines + `"\n--\n"` + pantry; `build_list(session, start)` raises `ShoppingError` (no key → `NO_KEY_MESSAGE`; API error → "Couldn't build the list: …"), writes `row.fingerprint`, `row.data` (`aisles[{name, items[{item, quantity, for[]}]}]`, `check_you_have`, `no_ingredients`, `dinners`), `row.created_at`, commits.
- `server/app/shopping/routes.py`: `_parse_week`, `_page(...)` computes `out_of_date = saved.fingerprint != fingerprint(...)`; `POST /shopping/build`.
- `server/app/templates/shopping/list.html`: banner/Build/Rebuild/Copy markup.
- `server/app/menu/entries.py:clear` **deletes** the row on weekdays → a deletion leaves no timestamp. Hence the quiet period is measured from when the loop first *observes* a new fingerprint, not from row timestamps.
- `Dish.updated_at` uses `onupdate=utcnow`, so recipe edits change the fingerprint already.
- Lifespan loops are started in `server/app/main.py` (`run_worker`, `run_sync_loop`, `run_backup_loop`).
- Migrations: Alembic, `render_as_batch=True`, autogenerate works (`DATA_DIR=/tmp/x .venv/bin/alembic revision --autogenerate --rev-id 0002 -m shopping_auto_rebuild` against a DB at head).

## Approach

### 1. Data: new columns on `shopping_lists` (migration `0002_shopping_auto_rebuild`)
Add to `ShoppingList` in `server/app/models.py`, all nullable/defaulted so existing rows load:
- `menu_fingerprint: str | None` — menu-only fingerprint at last successful build.
- `pending_fingerprint: str | None`, `pending_since: datetime | None` — the menu fingerprint the loop is waiting on and when it first saw it (naive UTC).
- `failed_attempts: int` default 0 — failures for `pending_fingerprint`.
- `last_attempt_at: datetime | None`, `last_error: str | None`.
- `previous_data: JSON | None` — the list data before the latest build.

Generate with autogenerate (rev-id `0002`, down_revision `0001`), check it only adds these columns (batch `add_column`), and that `failed_attempts` has `server_default="0"`.
Existing rows have `menu_fingerprint = NULL` → treated as changed → rebuilt once 5 min after deploy (acceptable; at most two lists).

### 2. Service: split fingerprint, record state on build, diff
In `server/app/shopping/service.py`:
- `menu_fingerprint(session, start) -> str`: sha256 of the same sorted dinner lines **without** pantry. Make `fingerprint()` = sha256(`menu_fingerprint` + `"\n--\n"` + pantry) so the existing out-of-date banner semantics (menu or pantry) are unchanged.
- `build_list`: before overwriting, set `row.previous_data = row.data` if the row existed; after a successful build set `row.menu_fingerprint`, and reset `pending_fingerprint=None, pending_since=None, failed_attempts=0, last_error=None, last_attempt_at=now`. Serialise all builds with a module-level `threading.Lock` (`_build_lock`) so a manual "Rebuild now" and the loop never call Claude for the same week concurrently; acquire inside `build_list` around the whole body.
- `diff_lists(old: dict | None, new: dict) -> dict` returning `{"added": [{"item","quantity"}], "removed": [{"item","quantity"}], "changed": [{"item","old","new"}]}`:
  - Key = `item.strip().lower()` across all aisles (ignore `check_you_have` and `no_ingredients`).
  - Duplicate keys within one list: keep the first.
  - `changed` when both present and `quantity.strip()` differs (case-insensitive compare).
  - Each list sorted by item name. `old is None` → all empty.

### 3. Pure decision + loop tick
New module `server/app/shopping/auto.py`:
- Constants: `QUIET = timedelta(minutes=5)`, `RETRY_AFTER = timedelta(minutes=15)`, `TICK_SECONDS = 60`.
- `decide(row: ShoppingList, menu_fp: str, now: datetime) -> Literal["idle", "wait", "rebuild", "gave_up"]` and mutates only the pending fields:
  1. `menu_fp == row.menu_fingerprint` → clear `pending_*`, `failed_attempts=0`, `last_error=None` → `"idle"`.
  2. `menu_fp != row.pending_fingerprint` → `pending_fingerprint=menu_fp`, `pending_since=now`, `failed_attempts=0`, `last_error=None` → `"wait"`.
  3. `now - pending_since < QUIET` → `"wait"`.
  4. `failed_attempts == 0` → `"rebuild"`.
  5. `failed_attempts == 1 and now - last_attempt_at >= RETRY_AFTER` → `"rebuild"`.
  6. otherwise → `"gave_up"`.
- `tick(session, now: datetime, build=build_list) -> list[date]` (returns weeks it attempted):
  - Weeks = `clock.week_start(clock.today())` and `+7`; skip weeks with no `ShoppingList` row.
  - For each: `decide(...)`; commit pending-field changes; on `"rebuild"` call `build(session, start)`; on `ShoppingError` or any `Exception`: `session.rollback()`, re-load row, set `failed_attempts += 1`, `last_attempt_at=now`, `last_error=str(e)` (generic exceptions: `"Couldn't build the list: <ExceptionType>"`), commit, log.
  - `now` is naive UTC (`models.utcnow()`); today comes from `clock.today()`.
- `async def run_shopping_loop()`: forever `await asyncio.to_thread(_tick_once)` (own `SessionLocal()`), then `await asyncio.sleep(TICK_SECONDS)`; exceptions logged, never kill the loop.
- Register in `server/app/main.py` lifespan alongside the other three loops.

### 4. Routes and page states
`server/app/shopping/routes.py` `_page` adds context:
- `auto_week = start in (week_start(today), week_start(today)+7)`.
- `menu_stale = saved and saved.menu_fingerprint != menu_fingerprint(session, start)`.
- `state`, first match wins:
  1. `"none"`: no saved list. Keep the existing "Build list" button.
  2. `"failed"`: `auto_week and menu_stale and saved.last_error`. The page then shows "Couldn't update", even while the single 15-minute retry is still to come.
  3. `"updating"`: `auto_week and menu_stale`.
  4. `"stale"`: `out_of_date` (pantry changed, or the week is outside current/next). Keep the existing banner "Out of date — the menu or pantry changed since this list was built." with Rebuild.
  5. `"current"`: otherwise.
- `diff = diff_lists(saved.previous_data, saved.data)` when saved.
`POST /shopping/build` unchanged ("Rebuild now" posts there).

Template `shopping/list.html`:
- Wrap everything below the nav in `<div id="shopping-body">`. When `state == "updating"`, give it `hx-get="/shopping?week={{ start }}" hx-trigger="every 10s" hx-select="#shopping-body" hx-swap="outerHTML"`; other states have no trigger, so polling stops once rebuilt.
- `updating`: `<form class="warn">Updating shortly… the menu changed since this list was built. <button>Rebuild now</button></form>`.
- `failed`: `<form class="warn">Couldn't update: {{ saved.last_error }} <button>Rebuild</button></form>`.
- "Changed since last list" section (only if any of added/removed/changed non-empty), above the aisles: three short lists — "Added: item (qty)", "Removed: item", "More/less: item old → new".
- Remove the old `out_of_date` banner branch in favour of `state`.

## Critical files & anchors
- `server/app/shopping/auto.py` — `decide` rules order; deletion-safe quiet period.
- `server/app/shopping/service.py` — `build_list` must set `menu_fingerprint` and clear pending state on success; `_build_lock`.
- `server/migrations/versions/0002_*.py` — additive columns only; `failed_attempts` server default.

## Verification
Unit tests (`cd server && .venv/bin/pytest -q tests/test_shopping.py tests/test_shopping_auto.py`), TDD one behaviour at a time at these seams:
- `decide` (`test_shopping_auto.py`, row built in memory, `now` literals):
  - matching fp → idle; first new fp → wait; same fp at +4m59s → wait; at +5m → rebuild.
  - fp changes again at +3m → timer restarts (rebuild only at +8m).
  - after one failure: +14m → gave_up/wait is not rebuild; +15m after last attempt → rebuild; after a second failure → gave_up forever until fp changes; new fp → wait then rebuild.
- `tick` with injected `build` (fake records calls; may raise `ShoppingError("boom")`):
  - saved current week + menu edit: tick at t0 → no call; t0+6m → one call for that week only.
  - no saved row for next week → never called; saved list for a past week → never called.
  - clearing a weekday dinner (row deletion) still triggers a rebuild after 5 min.
  - pantry change alone → never called.
  - build raising → `last_error="boom"`, `failed_attempts=1`; retried once at +15m.
- `menu_fingerprint` ignores pantry; `fingerprint` still changes with pantry (existing test stays green).
- `diff_lists`: added/removed/changed, case-insensitive names, quantity whitespace ignored, `None` old.
- Page states via `fastapi.testclient.TestClient(app)` with `get_session` overridden to the test session: `updating` page contains `Updating shortly…` and `hx-trigger="every 10s"`; `failed` shows `Couldn't update: boom`; pantry-only change shows the old "Out of date" banner; diff section renders.

End-to-end on the Pi 4 after deploy (needs `ANTHROPIC_API_KEY` set): build next week's list, change a dinner, open `/shopping` → "Updating shortly…"; ~5–6 min later the page swaps to the new list with "Changed since last list" showing the swapped dish's ingredients.

Full suite once at the end, then release `v0.2.0` (tag → CI → bump compose image tag on the Pi).
