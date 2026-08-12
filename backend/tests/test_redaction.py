from reboot_trace.redaction import redact_cmdline


def test_redacts_separate_equal_and_uri_credentials():
    value=redact_cmdline(b"tool\0--token\0secret\0--password=hunter2\0https://bob:pw@example/a\0")
    assert "secret" not in value.text and "hunter2" not in value.text and "bob:pw" not in value.text
    assert value.text.count("[REDACTED]") == 3
    assert value.status == "redacted"


def test_truncates_valid_utf8_and_hashes_redacted_full_value():
    value=redact_cmdline(("tool\0"+"你"*100).encode(),32)
    assert len(value.text.encode()) <= 32
    assert value.truncated
    value.text.encode("utf-8")


def test_invalid_utf8_fails_closed_without_echoing_input():
    value=redact_cmdline(b"tool\0--value\0secret\xff")
    assert value.text == "[REDACTION_FAILED]"
    assert value.status == "failed_closed"
