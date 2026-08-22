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
