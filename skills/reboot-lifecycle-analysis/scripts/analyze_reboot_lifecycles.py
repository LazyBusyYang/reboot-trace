#!/usr/bin/env python3
"""只读查询 Reboot Trace 生命周期并生成重启分析与通知草稿。"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SCHEMA = """
CREATE TABLE IF NOT EXISTS reboot_lifecycle_reports (
    backend_url TEXT NOT NULL,
    lifecycle_key TEXT NOT NULL,
    machine_name TEXT NOT NULL,
    boot_id TEXT,
    termination TEXT,
    final_snapshot_id INTEGER,
    reboot_time_ms INTEGER,
    candidate_pids_json TEXT NOT NULL,
    analyzed_at_ms INTEGER NOT NULL,
    PRIMARY KEY (backend_url, lifecycle_key)
)
"""


@dataclass
class LifecycleAnalysis:
    lifecycle: dict[str, Any]
    final: dict[str, Any]
    snapshot: dict[str, Any]
    reboot_time: str
    candidates: list[dict[str, Any]]


def api_base(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("后端地址必须使用 HTTP 或 HTTPS 协议")
    if not parsed.hostname:
        raise ValueError("后端地址必须包含主机名")
    if parsed.username or parsed.password:
        raise ValueError("后端地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("后端地址不能包含 query 或 fragment")

    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("后端地址端口无效") from exc

    if parsed.path not in {"", "/", "/api/v1", "/api/v1/"}:
        raise ValueError("后端地址路径只能为空或 /api/v1")

    return urlunsplit((parsed.scheme, parsed.netloc, "/api/v1", "", ""))


def get_json(base: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
    url = f"{base}{path}" + (f"?{query}" if query else "")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "codex-reboot-lifecycle-analysis/1"})
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        detail = safe_display_text(error.read().decode("utf-8", errors="replace")[:400])
        raise RuntimeError(f"GET {path} returned HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"GET {path} failed: {safe_display_text(error.reason)}") from error


def list_lifecycles(base: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = get_json(base, "/lifecycles", {"cursor": cursor})
        items.extend(page.get("items", []))
        cursor = page.get("next_cursor")
        if not cursor:
            return items


def open_database(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.execute(SCHEMA)
    connection.commit()
    return connection


def reported_lifecycle_keys(connection: sqlite3.Connection, base: str) -> set[str]:
    rows = connection.execute(
        "SELECT lifecycle_key FROM reboot_lifecycle_reports WHERE backend_url = ?",
        (base,),
    )
    return {str(row[0]) for row in rows}


def record_lifecycles(
    connection: sqlite3.Connection,
    base: str,
    machine_name: str,
    analyses: list[LifecycleAnalysis],
) -> list[LifecycleAnalysis]:
    inserted: list[LifecycleAnalysis] = []
    try:
        with connection:
            for analysis in analyses:
                lifecycle = analysis.lifecycle
                key = lifecycle.get("lifecycle_key") or lifecycle.get("boot_id")
                if not key:
                    continue
                result = connection.execute(
                    """
                    INSERT OR IGNORE INTO reboot_lifecycle_reports (
                        backend_url, lifecycle_key, machine_name, boot_id, termination,
                        final_snapshot_id, reboot_time_ms, candidate_pids_json, analyzed_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        base,
                        str(key),
                        machine_name,
                        lifecycle.get("boot_id"),
                        lifecycle.get("termination"),
                        analysis.snapshot.get("id"),
                        analysis.final.get("last_persisted_at_ms") or lifecycle.get("ended_at_ms"),
                        json.dumps([candidate.get("pid") for candidate in analysis.candidates], ensure_ascii=False),
                        int(time.time() * 1000),
                    ),
                )
                if result.rowcount == 1:
                    inserted.append(analysis)
    except sqlite3.Error as error:
        raise RuntimeError(f"写入 SQLite 去重记录失败：{error}") from error
    return inserted


def fmt_time(milliseconds: Any, timezone: ZoneInfo) -> str:
    if not isinstance(milliseconds, (int, float)):
        return "未知时间"
    return datetime.fromtimestamp(milliseconds / 1000, timezone).strftime("%Y-%m-%d %H:%M:%S")


def fmt_bytes(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "未知"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.2f} {unit}" if unit != "B" else f"{amount:.0f} B"
        amount /= 1024
    return str(value)


def safe_display_text(value: Any) -> str:
    """Render backend-originated text without terminal controls or hidden formatting."""
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    safe: list[str] = []
    for character in text:
        if character == "\n":
            safe.append(character)
        elif character == "\t":
            safe.append("    ")
        elif unicodedata.category(character).startswith("C"):
            continue
        else:
            safe.append(character)
    return "".join(safe)


def command_text(process: dict[str, Any]) -> str:
    return safe_display_text(process.get("cmdline_redacted") or process.get("comm") or "[命令未采集]")


def markdown_command_block(command: str) -> str:
    longest_backtick_run = max((len(run) for run in re.findall(r"`+", command)), default=0)
    fence = "`" * max(3, longest_backtick_run + 1)
    return f"{fence}bash\n{command}\n{fence}"


def machine_life_duration(lifecycles: list[dict[str, Any]]) -> str | None:
    active = [lifecycle for lifecycle in lifecycles if lifecycle.get("termination") == "active"]
    if not active:
        return None
    current = max(active, key=lambda lifecycle: int(lifecycle.get("last_seen_at_ms") or 0))
    started = current.get("started_at_ms")
    last_seen = current.get("last_seen_at_ms")
    if not isinstance(started, (int, float)) or not isinstance(last_seen, (int, float)):
        return None
    total_hours = max(0, int((last_seen - started) // 3_600_000))
    return f"{total_hours // 24}天{total_hours % 24}小时"


def alive_message(machine_name: str, lifecycles: list[dict[str, Any]]) -> str:
    duration = machine_life_duration(lifecycles)
    if duration is None:
        return f"当前未发现 {machine_name} 正在存活的生命周期，无法生成存活时长提示。"
    return f"真棒，{machine_name} 这台开发机又活过了 {duration}，感谢大家。"


def inferred_owner(process: dict[str, Any]) -> tuple[str, bool]:
    raw_username = str(process.get("username") or "unknown")
    username = safe_display_text(raw_username)
    if raw_username != "root":
        return username, False
    command = str(process.get("cmdline_redacted") or "")
    matches = re.findall(r"/mnt/(?:aigc/users/|aigc/|afs/)([A-Za-z0-9._-]+)", command)
    return (safe_display_text(matches[0]), True) if matches else (username, False)


def rank_map(snapshot: dict[str, Any], dimension: str) -> list[dict[str, Any]]:
    top = snapshot.get("top", {})
    return [item for item in top.get(dimension, []) if isinstance(item, dict)]


def has_final_snapshot(lifecycle: dict[str, Any]) -> bool:
    full_count = lifecycle.get("full_snapshot_count")
    if isinstance(full_count, (int, float)):
        return full_count > 0
    return lifecycle.get("retention_state") != "summary_only" and int(lifecycle.get("snapshot_count") or 0) > 0


def choose_candidates(snapshot: dict[str, Any], include_all: bool) -> list[dict[str, Any]]:
    cpu = rank_map(snapshot, "cpu")
    rss = rank_map(snapshot, "rss")
    if not cpu and not rss:
        return []
    ordered: list[dict[str, Any]] = []
    for item in cpu[:3] + rss[:3]:
        if item and item.get("pid") not in {current.get("pid") for current in ordered}:
            ordered.append(item)
    if include_all:
        return ordered
    # 同时位居两个排序前列的进程最明确；否则优先选取高 CPU 的进程。
    both = next((item for item in ordered if item in cpu[:3] and item in rss[:3]), None)
    return [both or (cpu[0] if cpu else rss[0])]


def evidence_limits(lifecycle: dict[str, Any], final: dict[str, Any]) -> str:
    limits: list[str] = []
    if lifecycle.get("termination") == "unclean_or_unknown":
        limits.append("生命周期以非正常或未知方式结束")
    evidence_gap = final.get("evidence_gap_ms")
    if isinstance(evidence_gap, (int, float)):
        limits.append(f"最终证据缺口为 {int(evidence_gap)} ms")
    return "；".join(limits)


def assessment(lifecycle: dict[str, Any], final: dict[str, Any], snapshot: dict[str, Any], candidate: dict[str, Any]) -> str:
    system = snapshot.get("system", {})
    oom_delta = system.get("oom_kill_delta")
    available = system.get("memory_available_bytes")
    total = system.get("memory_total_bytes")
    cpu = candidate.get("cpu_percent")
    if isinstance(oom_delta, (int, float)) and oom_delta > 0:
        result = f"检测到 OOM kill 增量 {oom_delta}，内存耗尽是直接证据；该进程是高资源候选。"
    elif isinstance(available, (int, float)) and isinstance(total, (int, float)) and total > 0 and available / total < 0.05:
        result = "可用内存不足总内存的 5%，存在明显内存压力；该进程是高资源候选。"
    elif isinstance(cpu, (int, float)) and cpu >= 100:
        result = "未发现本快照内的 OOM 增量，但该进程占用超过一个逻辑核，是最明确的高 CPU 候选。"
    else:
        result = "快照没有直接证明重启根因；该进程在最终快照的资源排序中最突出，应作为优先排查对象。"
    limits = evidence_limits(lifecycle, final)
    return result + (" 证据限制：" + limits + "。" if limits else "")


def notification(machine: str, items: list[tuple[str, dict[str, Any]]]) -> str:
    parts = [f"请注意，刚刚观察到 {machine} 这台开发机经历了以下重启，重启前存在高负载任务："]
    for index, (reboot_time, candidate) in enumerate(items, 1):
        owner, inferred = inferred_owner(candidate)
        owner_text = f"{owner}（路径推断）" if inferred else owner
        command = command_text(candidate)
        cpu = candidate.get("cpu_percent")
        cpu_text = f"{cpu:.2f}%" if isinstance(cpu, (int, float)) else "未知"
        parts.append(
            f"\n{index}. 疑似关联人员：{owner_text}\n"
            f"   重启时间：{reboot_time}\n"
            "   命令：\n"
            f"{markdown_command_block(command)}\n"
            f"   资源使用：CPU {cpu_text}，内存 {fmt_bytes(candidate.get('rss_bytes'))}"
        )
    parts.append(
        f"\n为了避免开发机重启断联影响到其他使用者，请尽量使用其他CCI进行开发和程序运行，只将 {machine} 作为ssh连接的跳板机使用。"
        "关于可用于开发的CCI，可以联系主管或同项目下的其他同事获取，建议同一个项目下多个同事共用CCI，保护跳板机安全。"
    )
    return "\n".join(parts)


def print_process(label: str, process: dict[str, Any]) -> None:
    owner, inferred = inferred_owner(process)
    owner_text = f"{owner}（路径推断）" if inferred else owner
    print(
        f"  {label}: PID {process.get('pid')} | 用户 {safe_display_text(process.get('username') or 'unknown')}"
        f" | 关联 {owner_text} | CPU {process.get('cpu_percent', 'unknown')}%"
        f" | RSS {fmt_bytes(process.get('rss_bytes'))}\n"
        f"    命令: {json.dumps(command_text(process), ensure_ascii=False)}"
    )


def print_analysis(
    analysis: LifecycleAnalysis,
    machine_name: str,
) -> None:
    lifecycle = analysis.lifecycle
    snapshot = analysis.snapshot
    candidates = analysis.candidates
    key = safe_display_text(lifecycle.get("lifecycle_key") or lifecycle.get("boot_id") or "unknown")
    print(f"\n生命周期 {key} | {lifecycle.get('termination')} | 最终快照 {snapshot.get('id')} @ {analysis.reboot_time}")
    if not candidates:
        limits = evidence_limits(lifecycle, analysis.final)
        print("结论: 最终快照未包含可用于排序的进程数据。" + (f" 证据限制：{limits}。" if limits else ""))
        return
    print(f"结论: {assessment(lifecycle, analysis.final, snapshot, candidates[0])}")
    for label, processes in (("CPU 前三", rank_map(snapshot, "cpu")[:3]), ("内存前三", rank_map(snapshot, "rss")[:3])):
        print(label + ":")
        for index, process in enumerate(processes, 1):
                print_process(f"#{index}", process)


def analyze_lifecycles(
    base: str,
    lifecycles: list[dict[str, Any]],
    timezone: ZoneInfo,
    include_all: bool,
) -> tuple[list[LifecycleAnalysis], list[str]]:
    analyses: list[LifecycleAnalysis] = []
    failures: list[str] = []
    for lifecycle in lifecycles:
        key = lifecycle.get("lifecycle_key") or lifecycle.get("boot_id")
        if not key:
            failures.append("发现缺少 lifecycle_key 和 boot_id 的生命周期，已跳过。")
            continue
        try:
            final = get_json(base, f"/lifecycles/{key}/final", {"count": 1})
        except RuntimeError as error:
            failures.append(f"生命周期 {key} 未完成最终快照读取：{error}")
            continue
        snapshots = final.get("snapshots", [])
        if not snapshots:
            failures.append(f"生命周期 {key} 没有可读取的最终完整快照，未写入数据库。")
            continue
        snapshot = snapshots[0]
        analyses.append(
            LifecycleAnalysis(
                lifecycle=lifecycle,
                final=final,
                snapshot=snapshot,
                reboot_time=fmt_time(final.get("last_persisted_at_ms") or lifecycle.get("ended_at_ms"), timezone),
                candidates=choose_candidates(snapshot, include_all),
            )
        )
    return analyses, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", required=True, help="单个 Reboot Trace 后端 URL，支持 HTTP 或 HTTPS，可带或不带 /api/v1")
    parser.add_argument("--machine-name", required=True, help="通知中使用的开发机名称")
    parser.add_argument("--timezone", default="Asia/Shanghai", help="时间展示所使用的 IANA 时区，默认 Asia/Shanghai")
    parser.add_argument("--all-candidates", action="store_true", help="为 CPU/RSS 排名前列的所有候选生成通知，而非仅最突出的一项")
    parser.add_argument("--sqlite-db", help="可选的 SQLite 数据库路径；传入后按后端地址和生命周期去重并持久化播报记录")
    args = parser.parse_args()
    timezone = ZoneInfo(args.timezone)
    base = api_base(args.backend_url)
    connection = open_database(args.sqlite_db) if args.sqlite_db else None
    try:
        lifecycles = list_lifecycles(base)
        qualifying = [
            lifecycle
            for lifecycle in lifecycles
            if lifecycle.get("termination") != "active" and has_final_snapshot(lifecycle)
        ]
        known = reported_lifecycle_keys(connection, base) if connection else set()
        pending = [
            lifecycle
            for lifecycle in qualifying
            if str(lifecycle.get("lifecycle_key") or lifecycle.get("boot_id") or "") not in known
        ]
        if not pending:
            print(alive_message(args.machine_name, lifecycles))
            return 0

        analyses, failures = analyze_lifecycles(base, pending, timezone, args.all_candidates)
        delivered = record_lifecycles(connection, base, args.machine_name, analyses) if connection else analyses
        notifications = [(analysis.reboot_time, candidate) for analysis in delivered for candidate in analysis.candidates]
        for analysis in delivered:
            print_analysis(analysis, args.machine_name)
        if notifications:
            print("\n通知消息:\n" + notification(args.machine_name, notifications))
        elif not failures:
            print(alive_message(args.machine_name, lifecycles))
        if failures:
            print("\n以下生命周期未完成分析，将在下一次任务中重试：")
            for failure in failures:
                print(f"- {failure}")
    finally:
        if connection:
            connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"错误: {error}", file=sys.stderr)
        raise SystemExit(1)
