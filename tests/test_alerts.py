from services.alerts import _signature


def test_signature_collapses_digits():
    a = _signature("[ERROR] Sheet write failed for row 47")
    b = _signature("[ERROR] Sheet write failed for row 912")
    assert a == b


def test_signature_separates_different_messages():
    a = _signature("[ERROR] Sheet write failed for row 47")
    b = _signature("[ERROR] Telegram send failed for row 47")
    assert a != b


def test_signature_is_bounded():
    assert len(_signature("[WARN] " + "x" * 5000)) <= 200


from services.alerts import Throttle


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def test_first_of_a_signature_sends():
    th = Throttle(now_fn=FakeClock())
    assert th.admit("sig-a", "[ERROR] boom") == "SEND"


def test_repeat_within_window_is_suppressed():
    th = Throttle(now_fn=FakeClock())
    th.admit("sig-a", "[ERROR] boom")
    assert th.admit("sig-a", "[ERROR] boom") == "SUPPRESS"
    assert th.admit("sig-a", "[ERROR] boom") == "SUPPRESS"


def test_different_signature_sends_independently():
    th = Throttle(now_fn=FakeClock())
    th.admit("sig-a", "[ERROR] boom")
    assert th.admit("sig-b", "[ERROR] other") == "SEND"


def test_window_expiry_yields_a_summary_with_the_count():
    clock = FakeClock()
    th = Throttle(now_fn=clock, dedupe_window=900.0)
    th.admit("sig-a", "[ERROR] boom")
    for _ in range(47):
        th.admit("sig-a", "[ERROR] boom")
    assert th.due_summaries() == []
    clock.advance(901)
    assert th.due_summaries() == [("[ERROR] boom", 47)]
    assert th.due_summaries() == []


def test_window_expiry_with_no_suppression_yields_nothing():
    clock = FakeClock()
    th = Throttle(now_fn=clock, dedupe_window=900.0)
    th.admit("sig-a", "[ERROR] boom")
    clock.advance(901)
    assert th.due_summaries() == []


def test_signature_resends_after_its_window_expires():
    clock = FakeClock()
    th = Throttle(now_fn=clock, dedupe_window=900.0)
    th.admit("sig-a", "[ERROR] boom")
    clock.advance(901)
    th.due_summaries()
    assert th.admit("sig-a", "[ERROR] boom") == "SEND"


def test_hourly_cap_mutes_once_then_suppresses():
    th = Throttle(now_fn=FakeClock(), dedupe_window=900.0, hourly_cap=3)
    assert th.admit("s1", "x") == "SEND"
    assert th.admit("s2", "x") == "SEND"
    assert th.admit("s3", "x") == "SEND"
    assert th.admit("s4", "x") == "MUTE"
    assert th.admit("s5", "x") == "SUPPRESS"


def test_muted_report_returns_counts_once():
    th = Throttle(now_fn=FakeClock(), dedupe_window=900.0, hourly_cap=2)
    th.admit("s1", "x")
    th.admit("s2", "x")
    th.admit("s3", "x")
    th.admit("s4", "x")
    assert th.muted_report() == (2, 2)
    assert th.muted_report() is None


def test_cap_resets_after_an_hour():
    clock = FakeClock()
    th = Throttle(now_fn=clock, dedupe_window=900.0, hourly_cap=2)
    th.admit("s1", "x")
    th.admit("s2", "x")
    assert th.admit("s3", "x") == "MUTE"
    clock.advance(3601)
    assert th.admit("s4", "x") == "SEND"


import datetime

from services.alerts import (
    TELEGRAM_MAX_CHARS,
    format_alert,
    format_mute_notice,
    who,
)

WHEN = datetime.datetime(2026, 8, 22, 9, 14)


def test_format_puts_level_source_and_time_on_the_first_line():
    out = format_alert("ERROR", "NTUFD", "[ERROR] boom", WHEN)
    first = out.splitlines()[0]
    assert "ERROR" in first
    assert "NTUFD" in first
    assert "09:14" in first


def test_format_uses_distinct_emoji_per_level():
    assert format_alert("ERROR", "N", "x", WHEN).startswith("\U0001F534")
    assert format_alert("WARN", "N", "x", WHEN).startswith("\U0001F7E0")


def test_format_includes_the_message_body():
    out = format_alert("ERROR", "NTUFD", "[ERROR] Sheet write failed", WHEN)
    assert "Sheet write failed" in out


def test_format_escapes_html_so_telegram_does_not_reject_it():
    out = format_alert("ERROR", "NTUFD", "[ERROR] <b>x</b> & co", WHEN)
    assert "&lt;b&gt;" in out
    assert "&amp;" in out


def test_format_adds_a_count_line_only_when_suppressed():
    assert "47 more" in format_alert("ERROR", "N", "x", WHEN, count=47)
    assert "more" not in format_alert("ERROR", "N", "x", WHEN, count=0)


def test_format_includes_context_and_frames_when_given():
    out = format_alert("ERROR", "N", "x", WHEN,
                       context="user 123", frames="a.py:1 in f")
    assert "user 123" in out
    assert "a.py:1 in f" in out


def test_format_truncates_to_the_telegram_limit():
    out = format_alert("ERROR", "N", "y" * 9000, WHEN)
    assert len(out) <= TELEGRAM_MAX_CHARS
    assert "truncated" in out


def test_mute_notice_names_both_counts():
    out = format_mute_notice("NTUFD", 20, 312, WHEN)
    assert "20" in out and "312" in out and "NTUFD" in out


class FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class FakeUpdate:
    def __init__(self, user):
        self.effective_user = user


def test_who_renders_handle_and_id():
    assert who(FakeUpdate(FakeUser(1505249420, "weiyin"))) == "@weiyin (1505249420)"


def test_who_falls_back_to_the_bare_id():
    assert who(FakeUpdate(FakeUser(1505249420))) == "1505249420"


def test_who_never_raises_on_a_junk_update():
    assert who(None) == "unknown user"
    assert who(object()) == "unknown user"


import io

from services.alerts import _SENDING, AlertTee


def make_tee():
    sink = io.StringIO()
    seen = []
    tee = AlertTee(sink, ("ERROR", "WARN"), ("[transient]",),
                   lambda level, line: seen.append((level, line)))
    return tee, sink, seen


def test_tee_passes_everything_through_unchanged():
    tee, sink, _ = make_tee()
    tee.write("[ERROR] boom\n")
    tee.write("ordinary output\n")
    assert sink.getvalue() == "[ERROR] boom\nordinary output\n"


def test_tee_emits_matching_lines():
    tee, _, seen = make_tee()
    tee.write("[ERROR] boom\n")
    assert seen == [("ERROR", "[ERROR] boom")]


def test_tee_ignores_non_matching_lines():
    tee, _, seen = make_tee()
    tee.write("[INFO] hello\n")
    tee.write("bare line\n")
    assert seen == []


def test_tee_honours_the_ignore_list():
    tee, _, seen = make_tee()
    tee.write("[ERROR][transient] NetworkError: read timeout\n")
    assert seen == []


def test_tee_reassembles_lines_split_across_writes():
    tee, _, seen = make_tee()
    tee.write("[WARN] split ")
    tee.write("message\n")
    assert seen == [("WARN", "[WARN] split message")]


def test_tee_handles_several_lines_in_one_write():
    tee, _, seen = make_tee()
    tee.write("[ERROR] one\n[WARN] two\n")
    assert seen == [("ERROR", "[ERROR] one"), ("WARN", "[WARN] two")]


def test_tee_does_not_capture_while_the_sending_guard_is_set():
    tee, sink, seen = make_tee()
    _SENDING.active = True
    try:
        tee.write("[ERROR] boom\n")
    finally:
        _SENDING.active = False
    assert seen == []
    assert sink.getvalue() == "[ERROR] boom\n"


def test_tee_never_lets_an_emit_failure_escape():
    sink = io.StringIO()

    def exploding_emit(level, line):
        raise RuntimeError("emit is broken")

    tee = AlertTee(sink, ("ERROR",), (), exploding_emit)
    tee.write("[ERROR] boom\n")
    assert sink.getvalue() == "[ERROR] boom\n"


import services.alerts as alerts_mod
from services.alerts import _deliver


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "body"


class FakeSession:
    def __init__(self, status_code):
        self._status_code = status_code
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return FakeResponse(self._status_code)


def test_deliver_reports_success():
    session = FakeSession(200)
    assert _deliver("tok", "42", "hi", session=session) == (True, False)
    assert "tok" in session.calls[0][0]
    assert session.calls[0][1]["chat_id"] == "42"


def test_deliver_marks_403_as_permanent():
    assert _deliver("tok", "42", "hi", session=FakeSession(403)) == (False, True)


def test_deliver_marks_401_as_permanent():
    assert _deliver("tok", "42", "hi", session=FakeSession(401)) == (False, True)


def test_deliver_marks_500_as_retryable():
    assert _deliver("tok", "42", "hi", session=FakeSession(500)) == (False, False)


def test_deliver_survives_a_thrown_transport_error():
    class Exploding:
        def post(self, url, json=None, timeout=None):
            raise OSError("network down")

    assert _deliver("tok", "42", "hi", session=Exploding()) == (False, False)


def test_install_noops_without_a_chat_id(monkeypatch):
    monkeypatch.delenv("ALERT_CHAT_ID", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "tok")
    monkeypatch.setattr(alerts_mod, "_installed", False)
    assert alerts_mod.install_alerts() is False


def test_install_noops_without_any_token(monkeypatch):
    monkeypatch.setenv("ALERT_CHAT_ID", "42")
    monkeypatch.delenv("ALERT_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setattr(alerts_mod, "_installed", False)
    assert alerts_mod.install_alerts() is False


def test_alert_before_install_is_a_silent_noop():
    alerts_mod.alert("[ERROR] nobody is listening")


from services.alerts import _pick_richest


def test_pick_richest_prefers_the_entry_carrying_context_and_frames():
    plain = ("ERROR", "[ERROR] boom", None, None)
    rich = ("ERROR", "[ERROR] boom", "user 1", "a.py:1")
    assert _pick_richest([plain, rich]) == [rich]


def test_pick_richest_keeps_first_seen_order():
    a = ("ERROR", "[ERROR] a", None, None)
    b = ("WARN", "[WARN] b", None, None)
    assert _pick_richest([a, b]) == [a, b]


def test_pick_richest_prefers_context_over_nothing():
    plain = ("ERROR", "[ERROR] boom", None, None)
    ctx = ("ERROR", "[ERROR] boom", "user 1", None)
    assert _pick_richest([plain, ctx]) == [ctx]


def test_pick_richest_leaves_distinct_signatures_alone():
    a = ("ERROR", "[ERROR] row 1 failed", None, None)
    b = ("ERROR", "[ERROR] totally different", "user 1", None)
    assert _pick_richest([a, b]) == [a, b]


def test_pick_richest_groups_by_signature_not_exact_text():
    a = ("ERROR", "[ERROR] row 1 failed", None, None)
    b = ("ERROR", "[ERROR] row 2 failed", "user 1", None)
    assert _pick_richest([a, b]) == [b]
