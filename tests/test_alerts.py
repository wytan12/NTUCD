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
