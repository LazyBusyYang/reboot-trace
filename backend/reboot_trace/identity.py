from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Settings


def _read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return default


def _pid1_start_ticks(proc_root: Path) -> int:
    raw = _read(proc_root / "1/stat")
    close_paren = raw.rfind(")")
    fields = raw[close_paren + 2:].split() if close_paren >= 0 else []
    if len(fields) <= 19:
        raise RuntimeError("cannot read PID 1 start ticks")
    return int(fields[19])


def _pid_namespace_inode(proc_root: Path) -> int:
    try:
        return int((proc_root / "1/ns/pid").stat().st_ino)
    except OSError as exc:
        raise RuntimeError("cannot read PID 1 namespace inode") from exc


def _cgroup_hash(proc_root: Path) -> str:
    lines = sorted(line.strip() for line in _read(proc_root / "1/cgroup").splitlines() if line.strip())
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class MountIdentity:
    mountpoint: str
    fs_type: str
    source: str


def _mount_identity(proc_root: Path, target: Path) -> MountIdentity | None:
    target_text = "/" + str(target).replace("\\", "/").lstrip("/")
    best: tuple[int, MountIdentity] | None = None
    for line in _read(proc_root / "1/mountinfo").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields, tail = left.split(), right.split()
        if len(fields) < 5 or len(tail) < 2:
            continue
        mountpoint = fields[4].replace("\\040", " ")
        if target_text == mountpoint or target_text.startswith(mountpoint.rstrip("/") + "/"):
            value = MountIdentity(mountpoint, tail[0], tail[1])
            candidate = (len(mountpoint), value)
            if best is None or candidate[0] > best[0]:
                best = candidate
    return best[1] if best else None


def _namespace_alignment(proc_root: Path) -> tuple[bool, str | None]:
    try:
        for namespace in ("pid", "mnt"):
            pid1 = (proc_root / "1/ns" / namespace).stat().st_ino
            current = (proc_root / "self/ns" / namespace).stat().st_ino
            if pid1 != current:
                return False, f"backend and PID 1 use different {namespace} namespaces"
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: cannot verify local container namespaces"


def _marker_storage(mount: MountIdentity | None) -> tuple[str | None, str | None]:
    if mount and mount.mountpoint == "/" and mount.fs_type in {"overlay", "fuse-overlayfs"}:
        return "container_ephemeral_rootfs", None
    if mount is None:
        return None, "marker mount is absent from PID 1 mountinfo"
    return None, f"marker mount {mount.mountpoint} uses {mount.fs_type}; expected the container root overlay"


def _open_marker(path: Path, flags: int, mode: int = 0o600) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    before = None
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise OSError("marker symlink is forbidden")
    except FileNotFoundError:
        pass
    fd = os.open(path, flags | nofollow, mode)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("marker is not a regular file")
        if opened.st_uid != os.geteuid():
            raise OSError("marker owner does not match the effective user")
        if stat.S_IMODE(opened.st_mode) & 0o022:
            raise OSError("marker must not be writable by group or other users")
        if before is not None and not os.path.samestat(before, opened):
            raise OSError("marker changed during secure open")
        after = path.lstat()
        if stat.S_ISLNK(after.st_mode) or not os.path.samestat(after, opened):
            raise OSError("marker changed during secure open")
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_marker(path: Path) -> str:
    fd = _open_marker(path, os.O_RDONLY)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("marker is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while total <= 128:
            chunk = os.read(fd, min(129 - total, 128))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > 128:
            raise ValueError("marker is too large")
        return str(uuid.UUID(b"".join(chunks).decode("utf-8").strip()))
    finally:
        os.close(fd)


def _create_marker(path: Path, value: str) -> None:
    fd = _open_marker(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("marker is not a regular file")
        os.write(fd, (value + "\n").encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _load_or_create_marker(settings: Settings, namespaces_aligned: bool, namespace_reason: str | None) -> tuple[str | None, str, str | None, str | None, MountIdentity | None]:
    configured = settings.instance_marker_path
    try:
        if configured.parent.is_symlink():
            raise OSError("marker parent symlink is forbidden")
        configured.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = configured.parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise OSError("marker parent is not a directory")
        if parent_stat.st_uid != os.geteuid():
            raise OSError("marker parent owner does not match the effective user")
        if stat.S_IMODE(parent_stat.st_mode) != 0o700:
            raise OSError("marker parent permissions must be 0700")
        real_parent = configured.parent.resolve(strict=True)
        path = real_parent / configured.name
    except (OSError, RuntimeError) as exc:
        return None, "temporarily_unavailable", f"{type(exc).__name__}: marker parent unavailable: {exc}", None, None
    mount = _mount_identity(settings.proc_root, path)
    storage, storage_reason = _marker_storage(mount)
    if settings.identity_scope != "local_container":
        return None, "unsupported", f"unsupported identity scope: {settings.identity_scope}", storage, mount
    if not namespaces_aligned:
        return None, "unsupported", namespace_reason, storage, mount
    if storage is None:
        return None, "unsupported", storage_reason, None, mount
    try:
        contended = False
        try:
            value = str(uuid.uuid4())
            _create_marker(path, value)
        except FileExistsError:
            contended = True
        attempts = 20 if contended else 1
        for attempt in range(attempts):
            try:
                marker = _read_marker(path)
                return marker, "supported", None, storage, mount
            except (ValueError, FileNotFoundError):
                if attempt + 1 == attempts:
                    raise
                time.sleep(0.005)
        raise RuntimeError("marker initialization did not complete")
    except (OSError, PermissionError, ValueError) as exc:
        return None, "temporarily_unavailable", f"{type(exc).__name__}: marker unavailable: {exc}", storage, mount


@dataclass(frozen=True, slots=True)
class ContainerIdentity:
    container_instance_id: str | None
    pid1_start_ticks: int
    pid_namespace_inode: int
    cgroup_hash: str
    kernel_boot_id: str
    marker_state: str
    marker_reason: str | None
    identity_scope: str
    marker_storage: str | None
    marker_mountpoint: str | None
    marker_fs_type: str | None
    namespace_state: str
    namespace_reason: str | None

    @property
    def fingerprint(self) -> str:
        payload = f"{self.pid1_start_ticks}:{self.pid_namespace_inode}:{self.cgroup_hash}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def preferred_key(self) -> str:
        return self.container_instance_id or f"pid1-{self.fingerprint[:32]}"

    def as_dict(self) -> dict[str, object]:
        return asdict(self) | {"fingerprint": self.fingerprint, "preferred_key": self.preferred_key}


class IdentityReader:
    def __init__(self, settings: Settings):
        self.settings = settings

    def read(self) -> ContainerIdentity:
        namespaces_aligned, namespace_reason = _namespace_alignment(self.settings.proc_root)
        marker, marker_state, marker_reason, marker_storage, mount = _load_or_create_marker(
            self.settings, namespaces_aligned, namespace_reason
        )
        boot_id = _read(self.settings.proc_root / "sys/kernel/random/boot_id").strip()
        if not boot_id:
            raise RuntimeError("cannot read kernel boot ID")
        return ContainerIdentity(
            container_instance_id=marker,
            pid1_start_ticks=_pid1_start_ticks(self.settings.proc_root),
            pid_namespace_inode=_pid_namespace_inode(self.settings.proc_root),
            cgroup_hash=_cgroup_hash(self.settings.proc_root),
            kernel_boot_id=boot_id,
            marker_state=marker_state,
            marker_reason=marker_reason,
            identity_scope=self.settings.identity_scope,
            marker_storage=marker_storage,
            marker_mountpoint=mount.mountpoint if mount else None,
            marker_fs_type=mount.fs_type if mount else None,
            namespace_state="supported" if namespaces_aligned else "unsupported",
            namespace_reason=namespace_reason,
        )


def identity_json(identity: ContainerIdentity) -> str:
    return json.dumps(identity.as_dict(), separators=(",", ":"), sort_keys=True)
