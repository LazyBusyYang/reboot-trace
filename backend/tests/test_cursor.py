import pytest
from fastapi import HTTPException
from reboot_trace.api import _cursor,_decode_cursor


def test_cursor_is_opaque_and_bound_to_scope():
    encoded=_cursor(42,"scope-a")
    assert encoded != "42"
    assert _decode_cursor(encoded,"scope-a") == 42
    with pytest.raises(HTTPException) as error: _decode_cursor(encoded,"scope-b")
    assert error.value.detail == "INVALID_CURSOR"
