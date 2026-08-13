from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

import pytest

from reboot_trace.process_control import (
    ProcessIdentityMismatch,
    capture,
    terminate,
    terminate_legacy,
    validate,
    validate_http,
    validate_legacy,
)


def fake_backend(tmp_path: Path, port: int, token: str = "t"*32, managed: bool = True):
    pid=123
    proc=tmp_path/"proc";process=proc/str(pid);process.mkdir(parents=True)
    fields=["S"]+["0"]*18+["100"]
    (process/"stat").write_text(f"{pid} (uvicorn worker) "+" ".join(fields)+"\n")
    arguments=["python","uvicorn","reboot_trace.main:app","--host","0.0.0.0","--port",str(port)]
    (process/"cmdline").write_bytes("\0".join(arguments).encode()+b"\0")
    data=(tmp_path/"data").resolve();marker=(tmp_path/"marker/container-instance-id").resolve()
    data.mkdir();marker.parent.mkdir()
    environment={
        "RT_DATA_DIR":str(data),
        "RT_INSTANCE_MARKER_PATH":str(marker),
        "RT_IDENTITY_SCOPE":"local_container",
        "RT_REQUIRE_CONTAINER_MARKER":"true",
    }
    if managed:
        environment["RT_SERVICE_TOKEN"]=token
    (process/"environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key,value in environment.items())+b"\0")
    return pid,proc,data,marker


def remove_on_signal(monkeypatch,proc_root:Path,pid:int):
    calls=[]
    def kill(observed_pid,signal):
        calls.append((observed_pid,signal))
        shutil.rmtree(proc_root/str(pid))
    monkeypatch.setattr(os,"kill",kill)
    return calls


def test_capture_validate_and_identity_checked_terminate(tmp_path,monkeypatch):
    state_file=tmp_path/"backend.pid";pid,proc,data,marker=fake_backend(tmp_path,32123)
    state=capture(state_file,pid,"t"*32,str(data),str(marker),32123,proc)
    assert state.pid==pid and state.start_ticks==100
    assert json.loads(state_file.read_text())["service_token"]=="t"*32
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert validate(state_file,str(data),str(marker),32123,proc).pid==pid
    calls=remove_on_signal(monkeypatch,proc,pid)
    terminate(state_file,str(data),str(marker),32123,proc,timeout=0.2)
    assert calls and calls[0][0]==pid


def test_start_ticks_mismatch_refuses_to_signal(tmp_path,monkeypatch):
    state_file=tmp_path/"backend.pid";pid,proc,data,marker=fake_backend(tmp_path,32124)
    capture(state_file,pid,"t"*32,str(data),str(marker),32124,proc)
    value=json.loads(state_file.read_text());value["start_ticks"]+=1
    state_file.write_text(json.dumps(value))
    calls=[];monkeypatch.setattr(os,"kill",lambda *args:calls.append(args))
    with pytest.raises(ProcessIdentityMismatch,match="start ticks changed"):
        terminate(state_file,str(data),str(marker),32124,proc,timeout=0.1)
    assert calls==[]


def test_instance_configuration_mismatch_refuses_state(tmp_path):
    state_file=tmp_path/"backend.pid";pid,proc,data,marker=fake_backend(tmp_path,32125)
    capture(state_file,pid,"t"*32,str(data),str(marker),32125,proc)
    with pytest.raises(ProcessIdentityMismatch,match="different instance"):
        validate(state_file,str(tmp_path/"other-data"),str(marker),32125,proc)


def test_process_state_permissions_must_remain_private(tmp_path):
    state_file=tmp_path/"backend.pid";pid,proc,data,marker=fake_backend(tmp_path,32129)
    capture(state_file,pid,"t"*32,str(data),str(marker),32129,proc)
    state_file.chmod(0o644)
    with pytest.raises(ProcessIdentityMismatch,match="permissions are unsafe"):
        validate(state_file,str(data),str(marker),32129,proc)


def test_legacy_process_can_only_be_stopped_after_exact_validation(tmp_path,monkeypatch):
    pid,proc,data,marker=fake_backend(tmp_path,32126,managed=False)
    assert validate_legacy(pid,str(data),32126,proc)==pid
    calls=[];monkeypatch.setattr(os,"kill",lambda *args:calls.append(args))
    with pytest.raises(ProcessIdentityMismatch,match="data directory"):
        terminate_legacy(pid,str(tmp_path/"other"),32126,proc,timeout=0.1)
    assert calls==[]
    calls=remove_on_signal(monkeypatch,proc,pid)
    terminate_legacy(pid,str(data),32126,proc,timeout=0.2)
    assert calls and calls[0][0]==pid


def test_http_validation_rejects_an_old_service_token(tmp_path,monkeypatch):
    token="n"*32;port=32127;state_file=tmp_path/"backend.pid"
    pid,proc,data,marker=fake_backend(tmp_path,port,token)
    body=json.dumps({
        "schema_version":3,"host_id":"host-test","lifecycle_key":"lifecycle-test",
        "identity":{"identity_scope":"local_container","namespace_state":"supported","marker_storage":"container_ephemeral_rootfs"},
        "capabilities":{"container_identity":{"state":"supported"}},
        "storage":{"persistence_capability":"operator_verification_required","data_path":str(data)},
    }).encode()
    class Response(io.BytesIO):
        headers={"X-Reboot-Trace-Service-Token":"old-service-token-000000000"}
        def __enter__(self): return self
        def __exit__(self,*args): self.close()
    monkeypatch.setattr("reboot_trace.process_control.urllib.request.urlopen",lambda *args,**kwargs:Response(body))
    capture(state_file,pid,token,str(data),str(marker),port,proc)
    with pytest.raises(ProcessIdentityMismatch,match="different service process"):
        validate_http(state_file,str(data),str(marker),port,proc,attempts=1)


def test_http_validation_accepts_the_captured_service_process(tmp_path,monkeypatch):
    token="c"*32;port=32128;state_file=tmp_path/"backend.pid"
    pid,proc,data,marker=fake_backend(tmp_path,port,token)
    body=json.dumps({
        "schema_version":3,"host_id":"host-test","lifecycle_key":"lifecycle-test",
        "identity":{"identity_scope":"local_container","namespace_state":"supported","marker_storage":"container_ephemeral_rootfs"},
        "capabilities":{"container_identity":{"state":"supported"}},
        "storage":{"persistence_capability":"operator_verification_required","data_path":str(data)},
    }).encode()
    class Response(io.BytesIO):
        headers={"X-Reboot-Trace-Service-Token":token}
        def __enter__(self): return self
        def __exit__(self,*args): self.close()
    monkeypatch.setattr("reboot_trace.process_control.urllib.request.urlopen",lambda *args,**kwargs:Response(body))
    capture(state_file,pid,token,str(data),str(marker),port,proc)
    assert validate_http(state_file,str(data),str(marker),port,proc,attempts=1)["host_id"]=="host-test"
