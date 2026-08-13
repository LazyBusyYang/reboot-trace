from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import uuid

import reboot_trace.identity as identity_module
from reboot_trace.identity import IdentityReader,namespace_evidence


def test_instance_marker_is_created_atomically_and_reused(settings):
    first=IdentityReader(settings).read()
    second=IdentityReader(settings).read()
    assert first.container_instance_id == second.container_instance_id
    assert first.marker_state == "supported"
    assert first.marker_storage == "container_ephemeral_rootfs"
    assert first.namespace_state == "supported"
    assert settings.instance_marker_path.read_text().strip() == first.container_instance_id
    assert settings.instance_marker_path.parent.stat().st_mode & 0o777 == 0o700
    assert settings.instance_marker_path.stat().st_mode & 0o777 == 0o600


def test_pid1_fingerprint_changes_with_start_ticks(settings):
    first=IdentityReader(settings).read()
    stat=(settings.proc_root/"1/stat").read_text()
    fields=stat.split();fields[21]="200"
    (settings.proc_root/"1/stat").write_text(" ".join(fields)+"\n")
    second=IdentityReader(settings).read()
    assert first.container_instance_id == second.container_instance_id
    assert first.fingerprint != second.fingerprint


def test_same_reader_observes_marker_removal_and_replacement(settings):
    reader=IdentityReader(settings)
    first=reader.read()
    settings.instance_marker_path.unlink()
    second=reader.read()
    assert second.container_instance_id != first.container_instance_id
    replacement=str(uuid.uuid4())
    settings.instance_marker_path.write_text(replacement+"\n")
    assert reader.read().container_instance_id == replacement


def test_concurrent_marker_initialization_never_overwrites_winner(settings):
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities=list(pool.map(lambda _:IdentityReader(settings).read(),range(16)))
    values={identity.container_instance_id for identity in identities}
    assert len(values)==1
    assert settings.instance_marker_path.read_text().strip()==next(iter(values))


def test_persistent_marker_mount_degrades_without_blocking(settings):
    mountinfo=settings.proc_root/"1/mountinfo"
    mountinfo.write_text(mountinfo.read_text()+"3 1 8:1 / /run rw - ext4 /dev/run rw\n")
    local=replace(settings,instance_marker_path=settings.data_dir/"marker")
    identity=IdentityReader(local).read()
    assert identity.container_instance_id is None
    assert identity.marker_state == "unsupported"
    assert "expected the container root overlay" in identity.marker_reason
    assert identity.pid1_start_ticks > 0


def test_separate_tmpfs_marker_mount_is_not_accepted(settings,tmp_path):
    marker_root=tmp_path/"tmp"
    marker_path=marker_root/"reboot-trace"/"container-instance-id"
    mountpoint="/"+marker_root.as_posix().lstrip("/")
    mountinfo=settings.proc_root/"1/mountinfo"
    mountinfo.write_text(mountinfo.read_text()+f"5 1 0:5 / {mountpoint} rw - tmpfs tmpfs rw\n")
    identity=IdentityReader(replace(settings,instance_marker_path=marker_path)).read()
    assert identity.container_instance_id is None
    assert identity.marker_state == "unsupported"
    assert identity.marker_fs_type == "tmpfs"
    assert "expected the container root overlay" in identity.marker_reason


def test_namespace_mismatch_disables_confirmed_marker(settings):
    current=settings.proc_root/"self/ns/mnt"
    current.unlink();current.write_text("different namespace")
    identity=IdentityReader(settings).read()
    assert identity.container_instance_id is None
    assert identity.marker_state == "unsupported"
    assert identity.namespace_state == "unsupported"
    assert "different mnt namespaces" in identity.namespace_reason


def deny_pid1_namespace_stat(settings,monkeypatch):
    original=Path.stat
    denied={settings.proc_root/"1/ns/pid",settings.proc_root/"1/ns/mnt"}
    def stat_path(path,*args,**kwargs):
        if path in denied:
            raise PermissionError(13,"permission denied",str(path))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"stat",stat_path)


def test_namespace_permission_fallback_accepts_matching_views(settings,monkeypatch):
    deny_pid1_namespace_stat(settings,monkeypatch)
    evidence=namespace_evidence(settings.proc_root)
    identity=IdentityReader(settings).read()
    assert evidence.aligned is True
    assert evidence.pid_identity and evidence.pid_identity>0
    assert "NSpid and mountinfo" in evidence.reason
    assert evidence.pid_identity==(settings.proc_root/"self/ns/pid").stat().st_ino
    assert identity.marker_state=="supported"
    assert identity.namespace_state=="supported"
    assert identity.pid_namespace_inode==evidence.pid_identity


def test_namespace_permission_fallback_rejects_nspid_depth_mismatch(settings,monkeypatch):
    (settings.proc_root/"self/status").write_text("Pid:\t123\nNSpid:\t1000\t123\n")
    deny_pid1_namespace_stat(settings,monkeypatch)
    evidence=namespace_evidence(settings.proc_root)
    assert evidence.aligned is False
    assert "incompatible NSpid chains" in evidence.reason
    assert IdentityReader(settings).read().marker_state=="unsupported"


def test_namespace_permission_fallback_rejects_different_mount_views(settings,monkeypatch):
    mountinfo=settings.proc_root/"self/mountinfo"
    mountinfo.write_text(mountinfo.read_text().replace("overlay overlay","overlay different-overlay"))
    deny_pid1_namespace_stat(settings,monkeypatch)
    evidence=namespace_evidence(settings.proc_root)
    assert evidence.aligned is False
    assert "mountinfo views differ" in evidence.reason


def test_namespace_nonpermission_error_remains_fail_closed(settings,monkeypatch):
    original=Path.stat
    target=settings.proc_root/"1/ns/pid"
    def stat_path(path,*args,**kwargs):
        if path==target:
            raise OSError(5,"I/O error",str(path))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,"stat",stat_path)
    evidence=namespace_evidence(settings.proc_root)
    assert evidence.aligned is False
    assert "OSError" in evidence.reason


def test_marker_parent_symlink_to_persistent_mount_is_rejected(settings,monkeypatch):
    parent=settings.instance_marker_path.parent
    parent.mkdir(parents=True,exist_ok=True)
    parent.rmdir()
    try:
        os.symlink(settings.data_dir,parent,target_is_directory=True)
    except OSError:
        parent.mkdir()
        original=Path.resolve
        monkeypatch.setattr(Path,"resolve",lambda self,strict=False:settings.data_dir if self==parent else original(self,strict=strict))
    identity=IdentityReader(settings).read()
    assert identity.container_instance_id is None
    assert identity.marker_state == "temporarily_unavailable"
    assert identity.container_instance_id is None
    assert "marker parent unavailable" in identity.marker_reason


def test_marker_file_symlink_is_not_followed(settings,monkeypatch):
    parent=settings.instance_marker_path.parent
    parent.mkdir(parents=True,exist_ok=True)
    target=settings.data_dir/"persisted-marker"
    target.write_text(str(uuid.uuid4())+"\n")
    try:
        os.symlink(target,settings.instance_marker_path)
    except OSError:
        original=identity_module._open_marker
        monkeypatch.setattr(identity_module,"_open_marker",lambda path,flags,mode=0o600: (_ for _ in ()).throw(OSError("marker symlink is forbidden")) if path==settings.instance_marker_path else original(path,flags,mode))
    identity=IdentityReader(settings).read()
    assert identity.container_instance_id is None
    assert identity.marker_state == "temporarily_unavailable"
    assert identity.marker_storage == "container_ephemeral_rootfs"
    assert "marker unavailable" in identity.marker_reason


def test_marker_parent_permissions_must_be_private(settings):
    parent=settings.instance_marker_path.parent
    parent.mkdir(parents=True,exist_ok=True)
    parent.chmod(0o750)
    identity=IdentityReader(settings).read()
    assert identity.container_instance_id is None
    assert identity.marker_state == "temporarily_unavailable"
    assert "permissions must be 0700" in identity.marker_reason


def test_marker_owner_must_match_effective_user(settings,monkeypatch):
    first=IdentityReader(settings).read()
    assert first.marker_state == "supported"
    monkeypatch.setattr(identity_module.os,"geteuid",lambda:settings.instance_marker_path.stat().st_uid+1)
    identity=IdentityReader(settings).read()
    assert identity.container_instance_id is None
    assert identity.marker_state == "temporarily_unavailable"
    assert "owner does not match" in identity.marker_reason


def test_existing_marker_must_not_be_group_writable(settings):
    first=IdentityReader(settings).read()
    assert first.marker_state == "supported"
    settings.instance_marker_path.chmod(0o620)
    identity=IdentityReader(settings).read()
    assert identity.container_instance_id is None
    assert identity.marker_state == "temporarily_unavailable"
    assert "writable by group or other" in identity.marker_reason
