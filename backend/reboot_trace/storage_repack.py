from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from .config import RetentionPolicy
from .database import SCHEMA, SCHEMA_VERSION
from .segment_store import FORMAT_VERSION, INTERNAL_SCHEMA, TABLE_KEYS, SegmentStore, _fsync_directory, copy_events, copy_rows, database_ok


STATE_FILE = ".evidence-repack.json"
DATA_TABLES = tuple(TABLE_KEYS)
SAFETY_RESERVE_BYTES = 2 * 1024 * 1024
OLD_LAYOUT_NAMES = (
    "reboot-trace.sqlite3", "reboot-trace.sqlite3-journal", "reboot-trace.sqlite3-wal",
    "reboot-trace.sqlite3-shm", "segments", "segments.json", "evidence", "evidence.json", "quarantine",
)


def _effective_policy(
    *, storage_limit_bytes: int | None, final_count: int | None,
    trend_count: int | None, trend_interval_ms: int | None,
) -> RetentionPolicy:
    configured = RetentionPolicy.from_env()
    return RetentionPolicy(
        storage_limit_bytes=storage_limit_bytes if storage_limit_bytes is not None else configured.storage_limit_bytes,
        sample_interval_ms=configured.sample_interval_ms,
        final_snapshots_per_lifecycle=final_count if final_count is not None else configured.final_snapshots_per_lifecycle,
        trend_snapshots_per_lifecycle=trend_count if trend_count is not None else configured.trend_snapshots_per_lifecycle,
        trend_interval_ms=trend_interval_ms if trend_interval_ms is not None else configured.trend_interval_ms,
    )


def add_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--storage-limit-mib",type=int)
    parser.add_argument("--final-snapshots",type=int)
    parser.add_argument("--trend-snapshots",type=int)
    parser.add_argument("--trend-interval-seconds",type=int)


def policy_overrides(args: argparse.Namespace) -> dict[str, int | None]:
    return {
        "storage_limit_bytes": args.storage_limit_mib * 1024 * 1024 if args.storage_limit_mib is not None else None,
        "final_count": args.final_snapshots,
        "trend_count": args.trend_snapshots,
        "trend_interval_ms": args.trend_interval_seconds * 1000 if args.trend_interval_seconds is not None else None,
    }


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n");handle.flush();os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _sources(data_dir: Path) -> list[Path]:
    active = data_dir / "reboot-trace.sqlite3"
    if not active.is_file():
        raise RuntimeError(f"database does not exist: {active}")
    result = [active]
    segments = data_dir / "segments"
    if segments.is_dir():
        result.extend(sorted(segments.glob("segment-*.sqlite3"), reverse=True))
    return result


def _validate_sources(paths: list[Path]) -> None:
    for path in paths:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok, reason = database_ok(connection, full=True)
            if not ok:
                raise RuntimeError(f"source database failed validation: {path.name}: {reason}")
        finally:
            connection.close()


def _union(paths: list[Path]) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", uri=True)
    connection.row_factory = sqlite3.Row
    sources: list[tuple[str, int]] = []
    for index, path in enumerate(paths):
        alias = f"source{index}"
        connection.execute(f"ATTACH DATABASE ? AS {alias}", (f"file:{path}?mode=ro&immutable=1",))
        sources.append((alias, len(paths) - index))
    for table, keys in TABLE_KEYS.items():
        columns = [str(row[1]) for row in connection.execute(f"PRAGMA {sources[0][0]}.table_info('{table}')")]
        quoted = ",".join(f'"{column}"' for column in columns)
        unions = [f"SELECT {quoted},{priority} AS _priority FROM {alias}.\"{table}\"" for alias, priority in sources]
        partition = ",".join(f'"{key}"' for key in keys)
        connection.execute(
            f'''CREATE TEMP VIEW "{table}" AS SELECT {quoted} FROM (
              SELECT {quoted},row_number() OVER(PARTITION BY {partition} ORDER BY _priority DESC) AS _rn
              FROM ({' UNION ALL '.join(unions)})
            ) WHERE _rn=1'''
        )
    return connection


def _create_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA page_size=4096")
    connection.execute("PRAGMA auto_vacuum=NONE")
    connection.executescript(SCHEMA)
    connection.executescript(INTERNAL_SCHEMA)
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return connection


def _materialize_union(source: sqlite3.Connection, path: Path) -> None:
    target=sqlite3.connect(path)
    try:
        target.executescript(SCHEMA)
        target.execute("PRAGMA foreign_keys=OFF")
        for table in DATA_TABLES:
            columns=[str(row[1]) for row in source.execute(f"PRAGMA table_info('{table}')")]
            quoted=",".join(f'"{column}"' for column in columns)
            placeholders=",".join("?" for _ in columns)
            rows=source.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
            if rows:target.executemany(f'INSERT INTO "{table}"({quoted}) VALUES({placeholders})',rows)
        target.commit()
    finally:
        target.close()


def _selected_ids(source: sqlite3.Connection, lifecycle_id: int, final_count: int, trend_count: int, trend_interval_ms: int) -> tuple[list[int], list[int]]:
    rows = source.execute(
        """SELECT id,captured_at_ms,detail_level,persistence_state FROM snapshot
           WHERE lifecycle_id=? ORDER BY captured_at_ms DESC,id DESC""",
        (lifecycle_id,),
    ).fetchall()
    full = [
        int(row["id"]) for row in rows
        if row["detail_level"] in {"full", "final"} and row["persistence_state"] == "normal"
    ][:final_count]
    full_set = set(full);buckets: dict[int,int] = {}
    newest_ms=int(rows[0]["captured_at_ms"]) if rows else 0
    trend_cutoff_ms=newest_ms-trend_count*trend_interval_ms
    for row in rows:
        snapshot_id=int(row["id"])
        if snapshot_id in full_set: continue
        if trend_count==0 or int(row["captured_at_ms"])<trend_cutoff_ms:continue
        buckets.setdefault(int(row["captured_at_ms"])//trend_interval_ms,snapshot_id)
    return full,list(buckets.values())[:trend_count]


def _copy_snapshots(target: sqlite3.Connection, full: list[int], trend: list[int]) -> None:
    ids=full+trend
    if not ids:return
    placeholders=",".join("?" for _ in ids)
    copy_rows(target,"source","snapshot",where=f"id IN ({placeholders})",params=ids)
    copy_rows(target,"source","system_sample",where=f"snapshot_id IN ({placeholders})",params=ids)
    if full:
        full_placeholders=",".join("?" for _ in full)
        for table in ("process_sample","process_rank","user_sample"):
            copy_rows(target,"source",table,where=f"snapshot_id IN ({full_placeholders})",params=full)
    if trend:
        trend_placeholders=",".join("?" for _ in trend)
        target.execute(f"UPDATE snapshot SET detail_level='summary_only',persistence_state='system_only' WHERE id IN ({trend_placeholders})",trend)


def _summary(source_json: str, termination: str, full: int, trend: int, final_target: int) -> str:
    value=json.loads(source_json or "{}")
    state="complete" if termination=="active" else "final_complete" if full>=final_target else "final_partial" if full>=2 else "final_minimum" if full==1 else "summary_only"
    value.update(retention_state=state,full_snapshot_count=full,trend_snapshot_count=trend)
    return json.dumps(value,separators=(",", ":"))


def _build_layout(source: sqlite3.Connection, stage: Path, *, final_count: int, trend_count: int, trend_interval_ms: int) -> dict[str,int]:
    evidence_dir=stage/"evidence";evidence_dir.mkdir()
    (stage/"quarantine").mkdir()
    active=_create_database(stage/"reboot-trace.sqlite3")
    _materialize_union(source,stage/"union.sqlite3")
    union_uri=(stage/"union.sqlite3").as_uri()+"?mode=ro&immutable=1"
    active.execute("ATTACH DATABASE ? AS source",(union_uri,))
    active.execute("BEGIN IMMEDIATE")
    copy_rows(active,"source","host")
    copy_rows(active,"source","lifecycle")
    current=source.execute("SELECT id FROM lifecycle WHERE termination='active' ORDER BY id DESC LIMIT 1").fetchone()
    current_id=int(current[0]) if current else None
    if current_id is not None:
        copy_rows(active,"source","service_session",where="lifecycle_id=?",params=(current_id,))
        full,trend=_selected_ids(source,current_id,final_count,trend_count,trend_interval_ms)
        _copy_snapshots(active,full,trend)
        copy_events(active,"source",full+trend,lifecycle_id=current_id)
    now=int(time.time()*1000)
    active.execute("INSERT INTO storage_meta VALUES(1,?,?, 'active',?,NULL,NULL,NULL)",(FORMAT_VERSION,1,now))
    for table in ("lifecycle","service_session","snapshot","event"):
        next_id=source.execute(f"SELECT COALESCE(max(id),0)+1 FROM {table}").fetchone()[0]
        active.execute("INSERT INTO storage_counter VALUES(?,?)",(table,next_id))
    counts={"capsules":0,"summary_only":0}
    lifecycles=source.execute("SELECT * FROM lifecycle ORDER BY id").fetchall()
    for lifecycle in lifecycles:
        lifecycle_id=int(lifecycle["id"])
        full,trend=_selected_ids(source,lifecycle_id,final_count,trend_count,trend_interval_ms)
        active.execute("UPDATE lifecycle SET summary_json=? WHERE id=?",(_summary(lifecycle["summary_json"],lifecycle["termination"],len(full),len(trend),final_count),lifecycle_id))
        if lifecycle_id==current_id or not full+trend:
            if lifecycle_id!=current_id:counts["summary_only"]+=1
            continue
        key=str(lifecycle["lifecycle_key"]);path=evidence_dir/SegmentStore.capsule_name(lifecycle_id,key)
        capsule=_create_database(path);capsule.execute("ATTACH DATABASE ? AS source",(union_uri,));capsule.execute("BEGIN IMMEDIATE")
        copy_rows(capsule,"source","host",where="id=?",params=(lifecycle["host_id"],))
        copy_rows(capsule,"source","lifecycle",where="id=?",params=(lifecycle_id,))
        capsule.execute("UPDATE lifecycle SET summary_json=? WHERE id=?",(_summary(lifecycle["summary_json"],lifecycle["termination"],len(full),len(trend),final_count),lifecycle_id))
        copy_rows(capsule,"source","service_session",where="lifecycle_id=?",params=(lifecycle_id,))
        _copy_snapshots(capsule,full,trend);copy_events(capsule,"source",full+trend,lifecycle_id=lifecycle_id)
        capsule.execute("INSERT INTO storage_meta VALUES(1,?,?, 'evidence',?,?,?,?)",(FORMAT_VERSION,lifecycle_id,now,now,lifecycle_id,key))
        for table in ("lifecycle","service_session","snapshot","event"):
            capsule.execute("INSERT INTO storage_counter VALUES(?,?)",(table,source.execute(f"SELECT COALESCE(max(id),0)+1 FROM {table}").fetchone()[0]))
        capsule.execute("COMMIT");capsule.execute("DETACH DATABASE source");capsule.execute("PRAGMA journal_mode=DELETE")
        ok,reason=database_ok(capsule,full=True);capsule.close()
        if not ok:raise RuntimeError(f"repacked capsule failed validation: {reason}")
        path.chmod(0o444);counts["capsules"]+=1
    active.execute("COMMIT");active.execute("DETACH DATABASE source");active.execute("PRAGMA journal_mode=DELETE")
    ok,reason=database_ok(active,full=True);active.close()
    if not ok:raise RuntimeError(f"repacked active database failed validation: {reason}")
    (stage/"union.sqlite3").unlink()
    store=SegmentStore(stage,1<<60);store.rebuild_manifest()
    return counts


def _backup(data_dir: Path, backup_root: Path) -> Path:
    stamp=time.strftime("%Y%m%d-%H%M%S")
    destination=backup_root/f"reboot-trace-evidence-repack-{stamp}"
    destination.mkdir(parents=True,exist_ok=False)
    hashes:dict[str,str]={}
    for name in ("reboot-trace.sqlite3","reboot-trace.sqlite3-journal","reboot-trace.sqlite3-wal","reboot-trace.sqlite3-shm","segments.json","evidence.json","host-id"):
        source=data_dir/name
        if source.is_file():
            shutil.copy2(source,destination/name);hashes[name]=hashlib.sha256((destination/name).read_bytes()).hexdigest()
    for directory_name in ("segments","evidence","quarantine"):
        source=data_dir/directory_name
        if source.is_dir():
            shutil.copytree(source,destination/directory_name)
            for path in (destination/directory_name).rglob("*"):
                if path.is_file():hashes[str(path.relative_to(destination))]=hashlib.sha256(path.read_bytes()).hexdigest()
    (destination/"sha256.json").write_text(json.dumps(hashes,sort_keys=True,indent=2)+"\n")
    return destination


def _install_checkpoint(phase: str) -> None:
    """Test hook for simulating a process failure between atomic install phases."""


def _safe_child(data_dir: Path, name: object, prefix: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or not name.startswith(prefix):
        raise RuntimeError("invalid interrupted repack state")
    return data_dir / name


def _resume_install(data_dir: Path, state: dict[str, object]) -> None:
    state_path=data_dir/STATE_FILE
    stage=_safe_child(data_dir,state.get("stage"),".evidence-repack-")
    old=_safe_child(data_dir,state.get("old"),".evidence-repack-old-")
    phase=str(state.get("phase","prepared"))
    if phase == "prepared":
        old.mkdir(exist_ok=True)
        for name in OLD_LAYOUT_NAMES:
            source=data_dir/name
            destination=old/name
            if source.exists() and destination.exists():
                raise RuntimeError(f"ambiguous interrupted repack state for {name}")
            if source.exists():os.replace(source,destination)
        state["phase"]="old_moved";_write_json(state_path,state)
        _fsync_directory(data_dir);_install_checkpoint("old_moved")
        phase="old_moved"
    if phase == "old_moved":
        for name in ("evidence","quarantine","evidence.json","reboot-trace.sqlite3"):
            source=stage/name;destination=data_dir/name
            if source.exists() and destination.exists():
                raise RuntimeError(f"ambiguous interrupted repack installation for {name}")
            if source.exists():os.replace(source,destination)
            elif not destination.exists():raise RuntimeError(f"interrupted repack is missing {name}")
        state["phase"]="installed";_write_json(state_path,state)
        _fsync_directory(data_dir);_install_checkpoint("installed")
        phase="installed"
    if phase != "installed":raise RuntimeError("invalid interrupted repack phase")
    if SegmentStore.format_version(data_dir/"reboot-trace.sqlite3")!=FORMAT_VERSION:
        raise RuntimeError("installed active database is not format v2")
    validation=sqlite3.connect(f"file:{data_dir/'reboot-trace.sqlite3'}?mode=ro",uri=True)
    try:
        ok,reason=database_ok(validation,full=True)
        if not ok:raise RuntimeError(f"installed active database failed validation: {reason}")
    finally:validation.close()
    store=SegmentStore(data_dir,1<<60);store.rebuild_manifest()
    shutil.rmtree(old);shutil.rmtree(stage,ignore_errors=True);state_path.unlink();_fsync_directory(data_dir)


def _install(data_dir: Path, stage: Path, backup: Path) -> None:
    state_path=data_dir/STATE_FILE
    old=data_dir/f".evidence-repack-old-{uuid.uuid4().hex}"
    state={"stage":stage.name,"old":old.name,"backup_dir":str(backup),"phase":"prepared"}
    _write_json(state_path,state);_install_checkpoint("prepared")
    _resume_install(data_dir,state)


def _preserved_root_bytes(data_dir: Path) -> int:
    replaced = set(OLD_LAYOUT_NAMES) | {STATE_FILE, f"{STATE_FILE}.tmp"}
    return sum(path.stat().st_size for path in data_dir.iterdir() if path.is_file() and path.name not in replaced)


def repack(
    data_dir: Path, backup_root: Path, *, dry_run: bool = False,
    storage_limit_bytes: int | None = None, final_count: int | None = None,
    trend_count: int | None = None, trend_interval_ms: int | None = None,
) -> dict[str,object]:
    data_dir=data_dir.resolve();backup_root=backup_root.resolve()
    if _inside(backup_root,data_dir):raise RuntimeError("backup directory must be outside RT_DATA_DIR")
    state_path=data_dir/STATE_FILE
    if state_path.exists():
        state=json.loads(state_path.read_text(encoding="utf-8"))
        _resume_install(data_dir,state)
        return {"dry_run":False,"resumed":True,"backup_dir":state.get("backup_dir")}
    active_path=data_dir/"reboot-trace.sqlite3"
    if SegmentStore.format_version(active_path)==FORMAT_VERSION:
        raise RuntimeError("storage is already format v2; evidence repack is not required")
    policy=_effective_policy(
        storage_limit_bytes=storage_limit_bytes,final_count=final_count,
        trend_count=trend_count,trend_interval_ms=trend_interval_ms,
    )
    paths=_sources(data_dir);_validate_sources(paths)
    source_format=SegmentStore.format_version(paths[0])
    guard=sqlite3.connect(paths[0],timeout=0,isolation_level=None)
    try:guard.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as exc:
        guard.close();raise RuntimeError("database is in use; stop the service before repack") from exc
    source=_union(paths)
    rows={table:int(source.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) for table in DATA_TABLES}
    source_bytes=sum(path.stat().st_size for path in paths)
    required=source_bytes+SAFETY_RESERVE_BYTES
    result={
        "dry_run":dry_run,"source_format":source_format,"source_bytes":source_bytes,"rows":rows,
        "required_free_bytes":required,
        "policy":{
            "storage_limit_bytes":policy.storage_limit_bytes,
            "final_snapshots_per_lifecycle":policy.final_snapshots_per_lifecycle,
            "trend_snapshots_per_lifecycle":policy.trend_snapshots_per_lifecycle,
            "trend_interval_ms":policy.trend_interval_ms,
        },
    }
    if shutil.disk_usage(data_dir).free<required:
        source.close();guard.execute("ROLLBACK");guard.close();raise RuntimeError("insufficient filesystem space for evidence repack")
    stage=data_dir/f".evidence-repack-{uuid.uuid4().hex}";stage.mkdir()
    source_open=True;guard_open=True
    try:
        result.update(_build_layout(
            source,stage,
            final_count=policy.final_snapshots_per_lifecycle,
            trend_count=policy.trend_snapshots_per_lifecycle,
            trend_interval_ms=policy.trend_interval_ms,
        ))
        stage_bytes=SegmentStore(stage,policy.storage_limit_bytes).managed_bytes()+_preserved_root_bytes(data_dir)
        budget_required=stage_bytes+SAFETY_RESERVE_BYTES
        within_budget=budget_required<=policy.storage_limit_bytes
        result.update(
            estimated_output_bytes=stage_bytes,
            budget_required_bytes=budget_required,
            budget_headroom_bytes=policy.storage_limit_bytes-budget_required,
            within_storage_budget=within_budget,
        )
        if dry_run:
            source.close();source_open=False
            guard.execute("ROLLBACK");guard.close();guard_open=False
            shutil.rmtree(stage,ignore_errors=True)
            return result
        if not within_budget:
            raise RuntimeError("repacked layout exceeds RT_STORAGE_LIMIT_MIB after the 2 MiB safety reserve")
        backup_root.mkdir(parents=True,exist_ok=True)
        if shutil.disk_usage(backup_root).free<source_bytes+SAFETY_RESERVE_BYTES:
            raise RuntimeError("insufficient backup filesystem space for evidence repack")
        backup=_backup(data_dir,backup_root);result["backup_dir"]=str(backup)
        source.close();source_open=False
        guard.execute("ROLLBACK");guard.close();guard_open=False
        _install(data_dir,stage,backup)
        result["resumed"]=False
        return result
    except Exception:
        if source_open:source.close()
        if guard_open:
            if guard.in_transaction:guard.execute("ROLLBACK")
            guard.close()
        if not state_path.exists():shutil.rmtree(stage,ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description="Repack stopped reboot-trace storage into format-v2 evidence capsules")
    parser.add_argument("--data-dir",type=Path,required=True);parser.add_argument("--backup-dir",type=Path,required=True);parser.add_argument("--dry-run",action="store_true")
    add_policy_arguments(parser)
    args=parser.parse_args(argv)
    try:print(json.dumps(repack(args.data_dir,args.backup_dir,dry_run=args.dry_run,**policy_overrides(args)),ensure_ascii=False,sort_keys=True))
    except Exception as exc:print(f"repack failed: {exc}",file=sys.stderr);return 1
    return 0


if __name__=="__main__":raise SystemExit(main())
