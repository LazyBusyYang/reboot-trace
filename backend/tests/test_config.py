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
    for name in ("RT_PROC_ROOT","RT_HOST_PASSWD","RT_INSTANCE_MARKER_PATH","RT_INSTANCE_MARKER_TARGET_PATH","RT_IDENTITY_SCOPE","RT_DATA_DIR"):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv("RT_PROJECT_DIR",str(Path.cwd()))
    settings=Settings.from_env()
    assert settings.proc_root.as_posix()=="/proc"
    assert settings.instance_marker_path.as_posix()=="/run/reboot-trace/container-instance-id"
    assert settings.data_dir==Path.cwd()/"var"/"reboot-trace"
    assert settings.identity_scope=="local_container"


def test_rejects_nonlocal_identity_scope(monkeypatch):
    monkeypatch.setenv("RT_IDENTITY_SCOPE","host")
    with pytest.raises(ValueError,match="local_container"):
        Settings.from_env()


def test_rejects_removed_marker_target_path(monkeypatch):
    monkeypatch.setenv("RT_INSTANCE_MARKER_TARGET_PATH","/different/path")
    with pytest.raises(ValueError,match="was removed"):
        Settings.from_env()
