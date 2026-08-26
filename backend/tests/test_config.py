from pathlib import Path
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


def test_local_container_identity_defaults(monkeypatch):
    for name in ("RT_PROC_ROOT","RT_HOST_PASSWD","RT_INSTANCE_MARKER_PATH","RT_INSTANCE_MARKER_TARGET_PATH","RT_IDENTITY_SCOPE","RT_REQUIRE_CONTAINER_MARKER","RT_SERVICE_TOKEN","RT_DATA_DIR","RT_FINAL_SNAPSHOTS_PER_LIFECYCLE","RT_TREND_SNAPSHOTS_PER_LIFECYCLE","RT_TREND_INTERVAL_SECONDS"):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv("RT_PROJECT_DIR",str(Path.cwd()))
    settings=Settings.from_env()
    assert settings.proc_root.as_posix()=="/proc"
    assert settings.instance_marker_path.as_posix()=="/run/reboot-trace/container-instance-id"
    assert settings.data_dir==Path.cwd()/"var"/"reboot-trace"
    assert settings.identity_scope=="local_container"
    assert settings.require_container_marker is False
    assert settings.service_token is None
    assert settings.final_snapshots_per_lifecycle == 12
    assert settings.trend_snapshots_per_lifecycle == 12
    assert settings.trend_interval_ms == 300_000


def test_lifecycle_snapshot_retention_validation(monkeypatch):
    monkeypatch.setenv("RT_FINAL_SNAPSHOTS_PER_LIFECYCLE", "0")
    with pytest.raises(ValueError, match="must be >= 1"):
        Settings.from_env()
    monkeypatch.setenv("RT_FINAL_SNAPSHOTS_PER_LIFECYCLE", "12")
    monkeypatch.setenv("RT_TREND_SNAPSHOTS_PER_LIFECYCLE", "0")
    assert Settings.from_env().trend_snapshots_per_lifecycle == 0
    monkeypatch.setenv("RT_TREND_SNAPSHOTS_PER_LIFECYCLE", "12")
    monkeypatch.setenv("RT_SAMPLE_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("RT_TREND_INTERVAL_SECONDS", "5")
    with pytest.raises(ValueError, match="must be >= RT_SAMPLE_INTERVAL_SECONDS"):
        Settings.from_env()


def test_required_container_marker_boolean(monkeypatch):
    monkeypatch.setenv("RT_REQUIRE_CONTAINER_MARKER", "true")
    assert Settings.from_env().require_container_marker is True
    monkeypatch.setenv("RT_REQUIRE_CONTAINER_MARKER", "sometimes")
    with pytest.raises(ValueError,match="must be a boolean"):
        Settings.from_env()


def test_service_token_validation(monkeypatch):
    monkeypatch.setenv("RT_SERVICE_TOKEN", "a"*32)
    assert Settings.from_env().service_token == "a"*32
    monkeypatch.setenv("RT_SERVICE_TOKEN", "too short")
    with pytest.raises(ValueError,match="URL-safe"):
        Settings.from_env()


def test_rejects_nonlocal_identity_scope(monkeypatch):
    monkeypatch.setenv("RT_IDENTITY_SCOPE","host")
    with pytest.raises(ValueError,match="local_container"):
        Settings.from_env()


def test_rejects_removed_marker_target_path(monkeypatch):
    monkeypatch.setenv("RT_INSTANCE_MARKER_TARGET_PATH","/different/path")
    with pytest.raises(ValueError,match="was removed"):
        Settings.from_env()
