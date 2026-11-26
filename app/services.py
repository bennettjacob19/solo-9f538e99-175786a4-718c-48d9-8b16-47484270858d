"""核心业务：升档核算、分钟补报/更正、汇总维护、档位回退。

关键约束（对应演练要求）：
1. 升档按整段窗口的请求/失败合计核算：漏报、总请求为零、错误率超限都维持原档。
2. 一次更正的“核对”只查按天汇总组，最多 64 组，未命中（该天尚无汇总）也计一组，
   且不查分钟明细；分钟表只在更正所报分钟上做 upsert 写入。
3. 分钟记录、日汇总、档位在同一个事务里一起生效；写入失败整体回滚。
4. 更正使当前档依据的时段变成零请求或超限后，当前档回退，随后新请求只走稳定版，
   直到重新升档成功。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .db import MAX_SUMMARY_GROUPS_PER_CHECK, transaction, utc_now_iso
from .timeutil import (
    ValidationError,
    day_key,
    minute_key,
    parse_nonneg_int,
    span_minutes,
)


# ---------------------------------------------------------------- 窗口合计
def window_totals(conn: sqlite3.Connection, start: datetime, end: datetime) -> dict:
    """整段窗口（含两端）合计与覆盖分钟数。只做 SUM/COUNT 聚合，不取分钟明细行。"""
    lo, hi = minute_key(start), minute_key(end)
    row = conn.execute(
        "SELECT COALESCE(SUM(requests), 0) AS requests, "
        "COALESCE(SUM(failures), 0) AS failures, "
        "COUNT(*) AS present "
        "FROM minute_metrics WHERE minute BETWEEN ? AND ?",
        (lo, hi),
    ).fetchone()
    expected = span_minutes(start, end)
    return {
        "requests": int(row["requests"]),
        "failures": int(row["failures"]),
        "present": int(row["present"]),
        "expected": expected,
        "missing": expected - int(row["present"]),
    }


def over_limit(failures: int, requests: int, limit_bp: int) -> bool:
    """failures/requests > limit_bp/10000；整数交叉相乘，避免浮点误差。"""
    if requests <= 0:
        return False
    return failures * 10000 > limit_bp * requests


# ---------------------------------------------------------------- 动作日志
def _log(conn: sqlite3.Connection, kind: str, accepted: bool, message: str,
         checked_groups: int | None = None, changed_groups: int | None = None,
         missed_groups: int | None = None) -> None:
    conn.execute(
        "INSERT INTO action_log (ts, kind, accepted, message, "
        "checked_groups, changed_groups, missed_groups) VALUES (?,?,?,?,?,?,?)",
        (utc_now_iso(), kind, 1 if accepted else 0, message,
         checked_groups, changed_groups, missed_groups),
    )


def get_state(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM tier_state WHERE id = 1").fetchone()


# ---------------------------------------------------------------- 升档
def submit_ramp(conn: sqlite3.Connection, start: datetime, end: datetime,
                ratio_bp: int, limit_bp: int) -> dict:
    """提交升档：校验窗口并按整段合计核算，失败则维持原档并给出原因。"""
    if start > end:
        raise ValidationError("起始分钟必须不晚于结束分钟")
    if start.year != end.year:
        raise ValidationError("起止分钟必须在同一年内")

    totals = window_totals(conn, start, end)
    reason = None
    if totals["missing"] > 0:
        reason = (f"漏报：窗口应有 {totals['expected']} 个分钟记录，"
                  f"实际只有 {totals['present']} 个，缺 {totals['missing']} 个，维持原档")
    elif totals["requests"] == 0:
        reason = "整段总请求数为零，无法核算错误率，维持原档"
    elif over_limit(totals["failures"], totals["requests"], limit_bp):
        reason = (f"错误率超限：{totals['failures']}/{totals['requests']} "
                  f"高于上限 {limit_bp / 100:.2f}%，维持原档")

    with transaction(conn):
        prev = get_state(conn)
        if reason is None:
            conn.execute(
                "UPDATE tier_state SET active=1, window_start_minute=?, "
                "window_end_minute=?, target_ratio_bp=?, error_limit_bp=?, "
                "updated_at=? WHERE id=1",
                (minute_key(start), minute_key(end), ratio_bp, limit_bp,
                 utc_now_iso()),
            )
            msg = (f"升档成功：窗口 {minute_key(start)} ~ {minute_key(end)}，"
                   f"请求 {totals['requests']}、失败 {totals['failures']}，"
                   f"分流 {ratio_bp / 100:.2f}%、上限 {limit_bp / 100:.2f}%")
            _log(conn, "ramp", True, msg)
            return {"accepted": True, "message": msg, "totals": totals,
                    "previous_active": bool(prev["active"])}
        # 维持原档：除日志外不改任何状态
        _log(conn, "ramp", False, reason)
        return {"accepted": False, "message": reason, "totals": totals,
                "previous_active": bool(prev["active"])}


# ---------------------------------------------------------------- 补报 / 更正
def _read_reported_rows(raw_rows: list) -> dict:
    """解析表单里的分钟行；同一次提交出现重复分钟直接拒绝，要求先合并。"""
    rows: dict[str, tuple[datetime, int, int]] = {}
    for idx, (m, r, f) in enumerate(raw_rows, start=1):
        minute = (m or "").strip()
        req_text = (r or "").strip()
        fail_text = (f or "").strip()
        if not minute and not req_text and not fail_text:
            continue  # 空行忽略
        parsed = _parse_row(minute, req_text, fail_text, idx)
        key = minute_key(parsed[0])
        if key in rows:
            raise ValidationError(f"第 {idx} 行与前面的分钟 {key} 重复，请合并后再提交")
        rows[key] = parsed
    if not rows:
        raise ValidationError("请至少填写一分钟的请求数和失败数")
    return rows


def _parse_row(minute: str, req_text: str, fail_text: str, idx: int):
    from .timeutil import parse_utc_minute
    if not minute:
        raise ValidationError(f"第 {idx} 行缺少分钟")
    dt = parse_utc_minute(minute, f"第 {idx} 行分钟")
    requests = parse_nonneg_int(req_text, f"第 {idx} 行请求数")
    failures = parse_nonneg_int(fail_text, f"第 {idx} 行失败数")
    if failures > requests:
        raise ValidationError(f"第 {idx} 行失败数不能大于请求数")
    return dt, requests, failures


def submit_correction(conn: sqlite3.Connection, raw_rows: list) -> dict:
    """补报或更正若干 UTC 分钟的候选版请求数/失败数。

    流程（单事务）：
    - 涉及的“天”即汇总组；组数 > 64 直接拒绝，一次都不查。
    - 核对：只按天查 daily_summary（未命中也计一组），不扫描分钟明细。
    - 写入：仅对所报分钟做 upsert，并按差值维护日汇总。
    - 若当前档生效且窗口受影响，重新核算合计；零请求或超限则回退当前档。
    """
    rows = _read_reported_rows(raw_rows)
    days = sorted({day_key(dt) for dt, _, _ in rows.values()})
    if len(days) > MAX_SUMMARY_GROUPS_PER_CHECK:
        msg = (f"本次更正跨 {len(days)} 个汇总组（天），"
               f"超过单次核对上限 {MAX_SUMMARY_GROUPS_PER_CHECK} 组，已整体拒绝、未写入")
        with transaction(conn):
            _log(conn, "correction", False, msg, checked_groups=len(days),
                 changed_groups=0, missed_groups=0)
        return {"accepted": False, "message": msg, "checked_groups": len(days),
                "changed_groups": 0, "missed_groups": 0,
                "rollback": None}

    with transaction(conn):
        # --- 核对：只查日汇总组，未命中也计 ---
        checked = 0
        missed = 0
        for day in days:
            checked += 1
            hit = conn.execute(
                "SELECT 1 FROM daily_summary WHERE day = ?", (day,)
            ).fetchone()
            if hit is None:
                missed += 1

        # --- 更正：只读所报分钟的旧明细，按差值更新日汇总 ---
        changed_groups: set[str] = set()
        for key in sorted(rows):
            dt, requests, failures = rows[key]
            day = day_key(dt)
            old = conn.execute(
                "SELECT requests, failures FROM minute_metrics WHERE minute = ?",
                (key,),
            ).fetchone()
            old_req = int(old["requests"]) if old else 0
            old_fail = int(old["failures"]) if old else 0
            conn.execute(
                "INSERT INTO minute_metrics (minute, day, requests, failures) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(minute) DO UPDATE SET requests=excluded.requests, "
                "failures=excluded.failures, day=excluded.day",
                (key, day, requests, failures),
            )
            d_req, d_fail = requests - old_req, failures - old_fail
            conn.execute(
                "INSERT INTO daily_summary (day, requests, failures) VALUES (?,?,?) "
                "ON CONFLICT(day) DO UPDATE SET "
                "requests = requests + excluded.requests, "
                "failures = failures + excluded.failures",
                (day, d_req, d_fail),
            )
            changed_groups.add(day)

        # --- 档位重估：只在当前档窗口与更正分钟有交集时 ---
        state = get_state(conn)
        rollback = None
        if state["active"]:
            start = datetime.strptime(state["window_start_minute"],
                                      "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            end = datetime.strptime(state["window_end_minute"],
                                    "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            affected = any(start <= dt <= end for dt, _, _ in rows.values())
            if affected:
                totals = window_totals(conn, start, end)
                rb_reason = None
                if totals["missing"] > 0:
                    rb_reason = f"更正后窗口出现漏报（缺 {totals['missing']} 分钟）"
                elif totals["requests"] == 0:
                    rb_reason = "更正后当前档依据时段总请求数变为零请求"
                elif over_limit(totals["failures"], totals["requests"],
                                state["error_limit_bp"]):
                    rb_reason = (f"更正后当前档依据时段错误率超限："
                                 f"{totals['failures']}/{totals['requests']} 超过 "
                                 f"{state['error_limit_bp'] / 100:.2f}%")
                if rb_reason:
                    conn.execute(
                        "UPDATE tier_state SET active=0, updated_at=? WHERE id=1",
                        (utc_now_iso(),))
                    rollback = rb_reason

        changed = len(changed_groups)
        if rollback:
            msg = (f"更正已写入（核对 {checked} 组、改动 {changed} 组、"
                   f"未命中 {missed} 组），并触发回退：{rollback}，"
                   "新请求将只走稳定版直到重新升档")
        else:
            msg = (f"更正已写入：核对汇总 {checked} 组、改动 {changed} 组、"
                   f"未命中 {missed} 组，共 {len(rows)} 个分钟")
        _log(conn, "correction", True, msg, checked_groups=checked,
             changed_groups=changed, missed_groups=missed)
        return {"accepted": True, "message": msg,
                "checked_groups": checked, "changed_groups": changed,
                "missed_groups": missed, "rollback": rollback,
                "minutes": len(rows)}


# ---------------------------------------------------------------- 页面辅助
def overview(conn: sqlite3.Connection) -> dict:
    minutes = conn.execute("SELECT COUNT(*) c FROM minute_metrics").fetchone()["c"]
    groups = conn.execute("SELECT COUNT(*) c FROM daily_summary").fetchone()["c"]
    agg = conn.execute(
        "SELECT COALESCE(SUM(requests),0) r, COALESCE(SUM(failures),0) f "
        "FROM daily_summary").fetchone()
    return {"minute_rows": minutes, "summary_groups": groups,
            "total_requests": agg["r"], "total_failures": agg["f"]}


def recent_logs(conn: sqlite3.Connection, limit: int = 12) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def latest_correction(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM action_log WHERE kind='correction' "
        "ORDER BY id DESC LIMIT 1").fetchone()
