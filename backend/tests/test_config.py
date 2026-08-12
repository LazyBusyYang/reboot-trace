import pytest
from reboot_trace.config import Settings


def test_rejects_wildcard_and_non_origin_cors(monkeypatch):
    monkeypatch.setenv("RT_CORS_ORIGINS","*")
    with pytest.raises(ValueError): Settings.from_env()
    monkeypatch.setenv("RT_CORS_ORIGINS","https://frontend.example/path")
    with pytest.raises(ValueError): Settings.from_env()


def test_rejects_resource_amplifying_top_n(monkeypatch):
    monkeypatch.setenv("RT_PROCESS_TOP_N","501")
    with pytest.raises(ValueError): Settings.from_env()
