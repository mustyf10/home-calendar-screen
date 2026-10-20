import json
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image

import frame

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
OFF = frame.Policy(False, "23:00", "06:00", "Europe/London")
NIGHT = frame.Policy(True, "23:00", "06:00", "Europe/London")


def png(color=1, size=frame.SIZE):
    buf = BytesIO()
    Image.new("1", size, color).save(buf, format="PNG")
    return buf.getvalue()


def wire(policy=OFF):
    return json.dumps(frame.asdict(policy))


class Harness:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.opts = frame.Options("http://x/frame.png", None, tmp_path)
        self.shown = []
        self.etags = []
        self.now = NOW

    def run(self, result=None, now=None, delay=timedelta(), duration=timedelta(), show=None):
        if now is not None:
            self.now = now
        if result is None:
            result = frame.Fetched(png(), '"v1"', OFF)

        def fetch(url, etag, ca_file):
            assert (url, ca_file) == (self.opts.url, None)
            self.etags.append(etag)
            self.now += delay
            return result

        def display(image):
            self.shown.append(image.copy())
            self.now += duration
            if show:
                show(image)

        return frame.run(self.opts, lambda: self.now, fetch, display)

    @property
    def state(self):
        return frame.load_state(self.dir)


@pytest.mark.parametrize("change", [
    {"night_enabled": 1}, {"night_start": "1:00"}, {"night_end": "24:00"},
    {"night_enabled": True, "night_start": "06:00"}, {"timezone": "Bad/Zone"},
    {"timezone": None},
])
def test_policy_rejects_invalid_fields(change):
    data = frame.asdict(OFF)
    data.update(change)
    assert frame.Policy.parse(json.dumps(data)) is None


@pytest.mark.parametrize("value", [None, "", "[]", "null", "{}", "broken"])
def test_policy_rejects_invalid_json(value):
    assert frame.Policy.parse(value) is None


@pytest.mark.parametrize("hour,minute,paused", [(21, 59, False), (22, 0, True),
                                               (4, 59, True), (5, 0, False)])
def test_app_timezone_pause_boundaries(hour, minute, paused):
    assert NIGHT.paused(NOW.replace(hour=hour, minute=minute)) is paused
    assert not OFF.paused(NOW.replace(hour=hour, minute=minute))


@pytest.mark.parametrize("instant", ["2026-10-25T00:30:00+00:00", "2026-10-25T01:30:00+00:00",
                                     "2026-03-29T00:30:00+00:00", "2026-03-29T01:30:00+00:00"])
def test_pause_covers_both_dst_fold_and_gap(instant):
    assert NIGHT.paused(datetime.fromisoformat(instant))


def test_same_day_pause():
    policy = frame.Policy(True, "09:00", "17:00", "UTC")
    assert policy.paused(NOW)
    assert not policy.paused(NOW.replace(hour=17))


@pytest.mark.parametrize("status", [200, 304, 503])
def test_native_fetch_reads_policy_on_success_and_http_errors(monkeypatch, status):
    headers = {"X-Display-Policy": wire(), "ETag": '"v1"'}

    class Response(BytesIO):
        def __init__(self):
            super().__init__(png())
            self.status = status
            self.headers = headers

    def urlopen(req, **kwargs):
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "status", headers, None)
        return Response()

    monkeypatch.setattr(frame.urllib.request, "urlopen", urlopen)
    result = frame.fetch("http://x/frame.png", '"old"', None)
    assert result.policy == OFF
    assert isinstance(result, {200: frame.Fetched, 304: frame.NotModified, 503: frame.FetchFailed}[status])


def test_first_draw_needs_policy_and_preserves_retriable_image(tmp_path):
    h = Harness(tmp_path)
    h.run(frame.Fetched(png(), '"v1"'))
    assert not h.shown and h.state.etag is None
    h.run()
    assert len(h.shown) == 1 and h.shown[0].mode == "1"
    assert h.state.etag == '"v1"'
    assert h.state.last_attempt == h.state.last_completed == NOW
    assert (tmp_path / "last.png").exists()


@pytest.mark.parametrize("body", [b"bad image", png(size=(10, 10))])
def test_bad_image_never_replaces_clean_cache(tmp_path, body):
    h = Harness(tmp_path)
    h.run()
    before = (tmp_path / "last.png").read_bytes()
    assert h.run(frame.Fetched(body, '"bad"', OFF), NOW + HOUR) == 2
    assert len(h.shown) == 1 and h.state.etag == '"v1"'
    assert (tmp_path / "last.png").read_bytes() == before


def test_overdue_maintenance_uses_valid_cache_when_new_frame_has_wrong_size(tmp_path):
    h = Harness(tmp_path)
    h.run()
    cached = (tmp_path / "last.png").read_bytes()
    due = NOW + frame.MAINTENANCE_AFTER
    assert h.run(frame.Fetched(png(size=(10, 10)), '"bad"', OFF), due) == 2
    assert h.shown[-1].size == frame.SIZE
    assert h.shown[-1].getpixel((795, 5)) == 255
    assert h.state.last_completed == due and h.state.etag == '"v1"'
    assert (tmp_path / "last.png").read_bytes() == cached


def test_paused_client_keeps_fetching_and_learns_disable_from_304(tmp_path):
    h = Harness(tmp_path)
    evening = NOW.replace(hour=22)
    h.run(frame.Fetched(png(), '"v1"', NIGHT), evening)
    assert not h.shown
    h.run(frame.Fetched(png(), '"v1"', OFF), evening)
    h.run(frame.NotModified(NIGHT), evening + timedelta(minutes=5))
    h.run(frame.Fetched(png(0), '"v2"'), evening + timedelta(minutes=10))
    assert len(h.shown) == 1 and h.state.etag == '"v1"'
    h.run(frame.NotModified(OFF), evening + timedelta(minutes=15))
    h.run(frame.Fetched(png(0), '"v2"'), evening + timedelta(minutes=20))
    assert len(h.shown) == 2 and h.state.etag == '"v2"'
    assert h.etags[-1] == '"v1"'


def test_error_updates_policy_but_missing_or_bad_policy_keeps_last_valid(tmp_path):
    h = Harness(tmp_path)
    h.run()
    h.run(frame.FetchFailed("down", NIGHT), NOW + timedelta(minutes=5))
    h.run(frame.FetchFailed("down", frame.Policy.parse("bad")), NOW + timedelta(minutes=10))
    assert h.state.policy == NIGHT


@pytest.mark.parametrize("seconds,draws", [(179, 1), (180, 2)])
def test_restart_minimum_boundary(tmp_path, seconds, draws):
    h = Harness(tmp_path)
    h.run()
    restarted = Harness(tmp_path)
    restarted.run(frame.Fetched(png(0), '"v2"', OFF), NOW + timedelta(seconds=seconds))
    assert len(h.shown) + len(restarted.shown) == draws
    assert restarted.state.etag == ('"v2"' if draws == 2 else '"v1"')


def test_network_delay_measured_after_fetch_and_completion(tmp_path):
    h = Harness(tmp_path)
    h.run(delay=timedelta(seconds=30), duration=timedelta(seconds=20))
    assert h.state.last_attempt == NOW + timedelta(seconds=30)
    assert h.state.last_completed == NOW + timedelta(seconds=50)
    h.run(frame.Fetched(png(0), '"v2"', OFF), NOW + timedelta(seconds=199),
          delay=timedelta(seconds=30))
    assert len(h.shown) == 1
    h.run(frame.Fetched(png(0), '"v2"', OFF), NOW + timedelta(seconds=200),
          delay=timedelta(seconds=30))
    assert len(h.shown) == 2


def test_backward_clock_does_not_bypass_guard(tmp_path):
    h = Harness(tmp_path)
    h.run()
    h.run(now=NOW - HOUR)
    assert len(h.shown) == 1
    with pytest.raises(ValueError, match="timezone-aware"):
        h.run(now=NOW.replace(tzinfo=None))


def test_interrupted_attempt_is_persisted_before_driver(tmp_path):
    h = Harness(tmp_path)

    def interrupt(image):
        assert h.state.last_attempt == NOW
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        h.run(show=interrupt)
    restarted = Harness(tmp_path)
    restarted.run(now=NOW + timedelta(seconds=179))
    assert not restarted.shown
    restarted.run(now=NOW + timedelta(seconds=180))
    assert len(restarted.shown) == 1


@pytest.mark.parametrize("failure_at", [1, 2])
def test_persistence_failure_cannot_allow_immediate_repeat(tmp_path, monkeypatch, failure_at):
    h = Harness(tmp_path)
    original = frame.save_state
    calls = 0

    def save(sd, state):
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise OSError("disk failed")
        original(sd, state)

    monkeypatch.setattr(frame, "save_state", save)
    with pytest.raises(OSError):
        h.run()
    assert len(h.shown) == failure_at - 1
    monkeypatch.setattr(frame, "save_state", original)
    h.run(now=NOW + timedelta(seconds=1))
    assert len(h.shown) == 1  # Either first safe draw or blocked repeated draw.


def test_unchanged_maintenance_trigger_and_no_early_repeat(tmp_path):
    h = Harness(tmp_path)
    h.run()
    h.run(frame.NotModified(), NOW + frame.MAINTENANCE_AFTER - timedelta(seconds=1))
    assert len(h.shown) == 1
    h.run(frame.NotModified(), NOW + frame.MAINTENANCE_AFTER)
    assert len(h.shown) == 2
    h.run(frame.NotModified(), NOW + frame.MAINTENANCE_AFTER + timedelta(minutes=5))
    assert len(h.shown) == 2


def test_offline_maintenance_preserves_warning_original_instant_and_clean_image(tmp_path):
    h = Harness(tmp_path)
    h.run()
    clean = (tmp_path / "last.png").read_bytes()
    since = NOW + timedelta(minutes=5)
    h.run(frame.FetchFailed("down"), since)
    h.run(frame.FetchFailed("down"), since + HOUR)
    first_stamp = h.shown[-1].tobytes()
    assert h.state.stamped and h.state.failing_since == since
    h.run(frame.FetchFailed("down"), since + HOUR + frame.MAINTENANCE_AFTER)
    assert len(h.shown) == 3 and h.shown[-1].tobytes() == first_stamp
    assert h.state.failing_since == since
    assert (tmp_path / "last.png").read_bytes() == clean


def test_offline_stamp_uses_app_timezone(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.run()
    seen = []
    monkeypatch.setattr(frame, "stamp", lambda image, since: seen.append(since) or image)
    h.run(frame.FetchFailed("down"), NOW + timedelta(minutes=5))
    h.run(frame.FetchFailed("down"), NOW + HOUR + timedelta(minutes=5))
    assert seen[0].hour == 13 and seen[0].minute == 5
    assert seen[0].astimezone(timezone.utc) == h.state.failing_since


@pytest.mark.parametrize("blocked_by", ["pause", "minimum"])
def test_pending_recovery_survives_until_physical_draw(tmp_path, blocked_by):
    h = Harness(tmp_path)
    h.run()
    h.run(frame.FetchFailed("down"), NOW + timedelta(minutes=5))
    stamped_at = NOW + HOUR + timedelta(minutes=5)
    h.run(frame.FetchFailed("down"), stamped_at)
    before = h.state.failing_since
    policy = frame.Policy(True, "00:00", "23:59", "UTC") if blocked_by == "pause" else OFF
    recovery = stamped_at + timedelta(seconds=179)
    h.run(frame.Fetched(png(0), '"v2"', policy), recovery)
    assert h.state.stamped and h.state.failing_since == before
    assert h.state.etag == '"v1"' and h.etags[-1] is None
    h.run(frame.Fetched(png(0), '"v2"', OFF), stamped_at + timedelta(seconds=180))
    assert not h.state.stamped and h.state.failing_since is None
    assert h.state.etag == '"v2"'


def test_preventive_draw_before_pause_when_deadline_would_fall_within_it(tmp_path):
    h = Harness(tmp_path)
    start = NOW.replace(hour=3)
    h.run(frame.Fetched(png(), '"v1"', OFF), start)
    h.run(frame.NotModified(NIGHT), NOW.replace(hour=21, minute=54))
    assert len(h.shown) == 1
    h.run(frame.NotModified(), NOW.replace(hour=21, minute=55))
    assert len(h.shown) == 2


def test_long_pause_deadline_overrides_pause(tmp_path):
    h = Harness(tmp_path)
    policy = frame.Policy(True, "12:05", "12:00", "UTC")
    h.run(frame.Fetched(png(), '"v1"', policy))
    h.run(frame.NotModified(), NOW + frame.MAINTENANCE_AFTER)
    assert policy.paused(h.now) and len(h.shown) == 2


def test_cached_offline_safety_without_any_known_policy(tmp_path):
    h = Harness(tmp_path)
    Image.open(BytesIO(png())).save(tmp_path / "last.png")
    assert h.run(frame.FetchFailed("down")) == 1
    assert not h.shown and h.state.policy is None
    h.run(frame.FetchFailed("down"), NOW + timedelta(seconds=179))
    assert not h.shown
    h.run(frame.FetchFailed("down"), NOW + timedelta(seconds=180))
    assert len(h.shown) == 1


def test_same_directory_concurrent_invocations_serialize(tmp_path):
    opts = frame.Options("http://x", None, tmp_path)
    entered = threading.Event()
    release = threading.Event()
    second_fetch = threading.Event()
    shown = []
    errors = []

    def first_fetch(*args):
        entered.set()
        assert release.wait(5)
        return frame.Fetched(png(), '"v1"', OFF)

    def later_fetch(*args):
        second_fetch.set()
        return frame.Fetched(png(0), '"v2"', OFF)

    def worker(fetch):
        try:
            frame.run(opts, lambda: NOW, fetch, lambda image: shown.append(image.copy()))
        except BaseException as e:
            errors.append(e)

    first = threading.Thread(target=worker, args=(first_fetch,))
    second = threading.Thread(target=worker, args=(later_fetch,))
    first.start()
    assert entered.wait(5)
    second.start()
    try:
        assert not second_fetch.wait(0.1)
    finally:
        release.set()
        first.join(5)
        second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors and second_fetch.is_set() and len(shown) == 1
    assert frame.load_state(tmp_path).etag == '"v1"'


def test_legacy_visible_warning_is_migrated_without_being_removed(tmp_path):
    Image.open(BytesIO(png())).save(tmp_path / "last.png")
    (tmp_path / "stamped").write_text("")
    (tmp_path / "etag").write_text('"old"')
    (tmp_path / "failing_since").write_text((NOW - 2 * HOUR).isoformat())
    h = Harness(tmp_path)
    h.run(frame.FetchFailed("down", NIGHT), NOW.replace(hour=22))
    assert h.state.stamped and h.state.etag == '"old"'
    assert h.state.failing_since == NOW - 2 * HOUR
    assert not (tmp_path / "stamped").exists()


def test_obsolete_quiet_cli_is_rejected(monkeypatch):
    monkeypatch.setenv("FRAME_URL", "http://x")
    with pytest.raises(SystemExit) as error:
        frame.main(["--quiet-hours", "off"])
    assert error.value.code == 2


@pytest.mark.parametrize("contents", ["", "{}", "not json"])
def test_corrupt_history_fails_closed(tmp_path, contents):
    (tmp_path / "state.json").write_text(contents)
    h = Harness(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        h.run()
    assert not h.shown


def test_failure_threshold_no_repeated_stamp_and_unstamped_recovery(tmp_path):
    h = Harness(tmp_path)
    h.run()
    since = NOW + timedelta(minutes=5)
    h.run(frame.FetchFailed("down"), since)
    h.run(frame.FetchFailed("down"), since + HOUR - timedelta(seconds=1))
    assert len(h.shown) == 1 and h.state.failing_since == since
    h.run(frame.NotModified(), since + HOUR)
    assert len(h.shown) == 1 and h.state.failing_since is None
    h.run(frame.FetchFailed("down"), since + 2 * HOUR)
    h.run(frame.FetchFailed("down"), since + 3 * HOUR)
    assert len(h.shown) == 2 and h.state.stamped
    h.run(frame.FetchFailed("down"), since + 4 * HOUR)
    assert len(h.shown) == 2


def test_pending_image_minimum_keeps_old_cache_and_etag_until_draw(tmp_path):
    h = Harness(tmp_path)
    h.run()
    before = (tmp_path / "last.png").read_bytes()
    h.run(frame.Fetched(png(0), '"v2"', OFF), NOW + timedelta(seconds=179))
    assert h.state.etag == '"v1"'
    assert (tmp_path / "last.png").read_bytes() == before
    h.run(frame.Fetched(png(0), '"v2"', OFF), NOW + timedelta(seconds=180))
    assert h.etags[-1] == '"v1"' and h.state.etag == '"v2"'
    assert Image.open(tmp_path / "last.png").getpixel((0, 0)) == 0


def test_missing_policy_cannot_replace_cache_even_during_safety_draw(tmp_path):
    h = Harness(tmp_path)
    Image.open(BytesIO(png())).save(tmp_path / "last.png")
    h.run(frame.Fetched(png(0), '"unapproved"'))
    assert not h.shown
    h.run(frame.Fetched(png(0), '"unapproved"'), NOW + timedelta(seconds=180))
    assert len(h.shown) == 1 and h.shown[0].getpixel((0, 0)) == 255
    assert h.state.etag is None


def test_truncated_http_body_preserves_policy_and_runs_due_cached_maintenance(tmp_path):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class TruncatedFrame(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.send_header("X-Display-Policy", wire(NIGHT))
            self.end_headers()
            self.wfile.write(b"short")
            self.close_connection = True

        def log_message(self, format, *args):
            return

    h = Harness(tmp_path)
    h.run()
    cached = (tmp_path / "last.png").read_bytes()
    due = NOW + frame.MAINTENANCE_AFTER
    with HTTPServer(("127.0.0.1", 0), TruncatedFrame) as server:
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        h.opts.url = f"http://127.0.0.1:{server.server_port}/frame.png"
        try:
            assert frame.run(h.opts, lambda: due, frame.fetch, h.shown.append) == 1
        finally:
            server.shutdown()
            serving.join()
    assert len(h.shown) == 2 and h.shown[-1].size == frame.SIZE
    assert h.state.policy == NIGHT
    assert h.state.last_completed == due and h.state.failing_since == due
    assert h.state.etag == '"v1"'
    assert (tmp_path / "last.png").read_bytes() == cached
