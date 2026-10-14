from datetime import datetime, time, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import frame

NOW = datetime(2026, 10, 14, 12, 0)
HOUR = timedelta(minutes=60)


def png(color=1, size=(800, 480)):
    buf = BytesIO()
    Image.new("1", size, color).save(buf, format="PNG")
    return buf.getvalue()


# --- quiet hours ---------------------------------------------------------


def test_parse_quiet_hours_off_disables():
    assert frame.parse_quiet_hours("off") is None
    assert frame.parse_quiet_hours(" OFF ") is None


def test_parse_quiet_hours_window():
    assert frame.parse_quiet_hours("23:00-06:00") == (time(23, 0), time(6, 0))


@pytest.mark.parametrize("spec", ["", "23:00", "25:00-06:00", "ab-cd"])
def test_parse_quiet_hours_rejects_garbage(spec):
    with pytest.raises(ValueError):
        frame.parse_quiet_hours(spec)


@pytest.mark.parametrize(
    "t, expected",
    [
        (time(22, 59), False),
        (time(23, 0), True),
        (time(0, 0), True),
        (time(5, 59), True),
        (time(6, 0), False),
        (time(12, 0), False),
    ],
)
def test_quiet_window_wraps_midnight(t, expected):
    assert frame.in_quiet_hours(t, (time(23, 0), time(6, 0))) is expected


def test_quiet_window_same_day():
    window = (time(0, 0), time(23, 59))
    assert frame.in_quiet_hours(time(12, 0), window)
    assert not frame.in_quiet_hours(time(23, 59), window)


def test_quiet_off_is_never_quiet():
    assert frame.in_quiet_hours(time(3, 0), None) is False


# --- decide --------------------------------------------------------------


def state(**kw):
    base = dict(etag=None, failing_since=None, stamped=False, has_last=True)
    base.update(kw)
    return frame.State(**base)


def test_fetched_draws_and_clears_failure():
    d = frame.decide(NOW, state(failing_since=NOW - 2 * HOUR), frame.Fetched(b"", '"e"'))
    assert (d.action, d.failing_since, d.exit_code) == ("draw", None, 0)


def test_not_modified_is_unchanged():
    d = frame.decide(NOW, state(failing_since=NOW - HOUR), frame.NotModified())
    assert (d.action, d.failing_since, d.exit_code) == ("unchanged", None, 0)


def test_first_failure_records_time_without_stamp():
    d = frame.decide(NOW, state(), frame.FetchFailed("boom"))
    assert (d.action, d.failing_since, d.exit_code) == ("fail", NOW, 1)


def test_failure_under_an_hour_does_not_stamp():
    since = NOW - HOUR + timedelta(seconds=1)
    d = frame.decide(NOW, state(failing_since=since), frame.FetchFailed("boom"))
    assert (d.action, d.failing_since) == ("fail", since)


def test_failure_after_an_hour_stamps():
    since = NOW - HOUR
    d = frame.decide(NOW, state(failing_since=since), frame.FetchFailed("boom"))
    assert (d.action, d.failing_since, d.exit_code) == ("stamp", since, 1)


def test_already_stamped_does_not_stamp_again():
    d = frame.decide(NOW, state(failing_since=NOW - 3 * HOUR, stamped=True), frame.FetchFailed("x"))
    assert d.action == "fail"


def test_no_last_image_cannot_stamp():
    d = frame.decide(NOW, state(failing_since=NOW - 3 * HOUR, has_last=False), frame.FetchFailed("x"))
    assert d.action == "fail"


def test_not_modified_while_stamped_redraws_last():
    d = frame.decide(NOW, state(stamped=True, failing_since=NOW - 2 * HOUR), frame.NotModified())
    assert (d.action, d.failing_since, d.exit_code) == ("redraw_last", None, 0)


def test_stamped_state_ignores_etag():
    assert frame.request_etag(state(etag='"e"')) == '"e"'
    assert frame.request_etag(state(etag='"e"', stamped=True)) is None


# --- run (state files, fake fetch/show) ----------------------------------


class Harness:
    def __init__(self, tmp_path, results, quiet="off"):
        self.dir = Path(tmp_path)
        self.results = list(results)
        self.sent_etags = []
        self.shown = []
        self.opts = frame.Options(url="http://x/frame.png", ca_file=None, state_dir=self.dir,
                                  quiet=frame.parse_quiet_hours(quiet))

    def fetch(self, url, etag, ca_file):
        self.sent_etags.append(etag)
        return self.results.pop(0)

    def show(self, img):
        self.shown.append(img.copy())

    def run(self, now):
        return frame.run(self.opts, now, self.fetch, self.show)


def test_run_success_saves_last_and_etag(tmp_path):
    h = Harness(tmp_path, [frame.Fetched(png(), '"v1"')])
    assert h.run(NOW) == 0
    assert len(h.shown) == 1 and h.shown[0].mode == "1"
    assert (h.dir / "etag").read_text() == '"v1"'
    assert (h.dir / "last.png").exists()


def test_run_wrong_size_exits_2_without_state(tmp_path):
    h = Harness(tmp_path, [frame.Fetched(png(size=(10, 10)), '"v1"')])
    assert h.run(NOW) == 2
    assert h.shown == []
    assert not (h.dir / "etag").exists()


def test_run_offline_stamps_after_an_hour(tmp_path):
    h = Harness(tmp_path, [frame.Fetched(png(), '"v1"'), frame.FetchFailed("down"),
                           frame.FetchFailed("down"), frame.FetchFailed("down")])
    h.run(NOW)
    assert h.run(NOW + timedelta(minutes=5)) == 1
    assert (h.dir / "failing_since").exists()
    assert len(h.shown) == 1
    assert h.run(NOW + timedelta(minutes=65)) == 1
    assert len(h.shown) == 2 and (h.dir / "stamped").exists()
    stamped = h.shown[1]
    assert stamped.getpixel((5, 475)) == 0  # black box bottom-left
    assert stamped.getpixel((795, 5)) == 255  # rest untouched
    h.run(NOW + timedelta(minutes=70))
    assert len(h.shown) == 2  # no second stamp


def test_run_quiet_hours_neither_fetches_nor_stamps(tmp_path):
    h = Harness(tmp_path, [], quiet="00:00-23:59")
    (h.dir / "failing_since").write_text((NOW - 5 * HOUR).isoformat())
    png_path = h.dir / "last.png"
    Image.open(BytesIO(png())).save(png_path)
    assert h.run(NOW) == 0
    assert h.sent_etags == [] and h.shown == []
    assert not (h.dir / "stamped").exists()


def test_run_success_after_stamp_forces_redraw_and_clears(tmp_path):
    h = Harness(tmp_path, [frame.Fetched(png(), '"v1"')])
    (h.dir / "etag").write_text('"v1"')
    (h.dir / "stamped").write_text("")
    (h.dir / "failing_since").write_text((NOW - 2 * HOUR).isoformat())
    assert h.run(NOW) == 0
    assert h.sent_etags == [None]
    assert len(h.shown) == 1
    assert not (h.dir / "stamped").exists()
    assert not (h.dir / "failing_since").exists()


def test_run_unchanged_clears_failure(tmp_path):
    h = Harness(tmp_path, [frame.NotModified()])
    (h.dir / "etag").write_text('"v1"')
    (h.dir / "failing_since").write_text((NOW - HOUR / 2).isoformat())
    assert h.run(NOW) == 0
    assert h.sent_etags == ['"v1"'] and h.shown == []
    assert not (h.dir / "failing_since").exists()
