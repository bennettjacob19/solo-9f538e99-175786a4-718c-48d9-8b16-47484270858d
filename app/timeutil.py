"""UTC 分钟解析与展示辅助。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MINUTE_FMT = "%Y-%m-%dT%H:%M"
DAY_FMT = "%Y-%m-%d"


class ValidationError(ValueError):
    """页面输入校验失败，消息直接提示给用户。"""


def parse_utc_minute(value: str, field: str) -> datetime:
    """把 <input type="datetime-local"> 的值解析为 UTC 朴素分钟（秒以下截零）。"""
    if value is None:
        raise ValidationError(f"{field}不能为空")
    text = value.strip().replace(" ", "T")
    if not text:
        raise ValidationError(f"{field}不能为空")
    # 只接受分钟精度，拒绝带秒/时区的奇怪输入
    if len(text) != 16 or text[4] != "-" or text[7] != "-" or text[10] != "T":
        raise ValidationError(f"{field}必须是 UTC 分钟，格式 YYYY-MM-DDTHH:MM")
    try:
        dt = datetime.strptime(text, MINUTE_FMT)
    except ValueError as exc:
        raise ValidationError(f"{field}不是合法的 UTC 分钟：{text}") from exc
    return dt.replace(tzinfo=timezone.utc, second=0, microsecond=0)


def minute_key(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(MINUTE_FMT)


def day_key(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(DAY_FMT)


def parse_nonneg_int(value: str, field: str) -> int:
    if value is None or str(value).strip() == "":
        raise ValidationError(f"{field}不能为空")
    try:
        n = int(str(value).strip())
    except ValueError as exc:
        raise ValidationError(f"{field}必须是非负整数") from exc
    if n < 0:
        raise ValidationError(f"{field}必须是非负整数")
    return n


def parse_ratio_bp(value: str, field: str) -> int:
    """目标分流比例，单位 %，保留两位小数；内部转基点整数（0.01% 为 1bp）。"""
    if value is None or str(value).strip() == "":
        raise ValidationError(f"{field}不能为空")
    text = str(value).strip().rstrip("%")
    try:
        bp = round(float(text) * 100)
    except ValueError as exc:
        raise ValidationError(f"{field}必须是 0~100 的百分数") from exc
    if not (1 <= bp <= 10000):
        raise ValidationError(f"{field}必须在 0.01% ~ 100% 之间")
    return bp


def parse_limit_bp(value: str, field: str) -> int:
    """错误率上限，单位 %，保留两位小数。允许 0%（任何失败都超限）。"""
    if value is None or str(value).strip() == "":
        raise ValidationError(f"{field}不能为空")
    text = str(value).strip().rstrip("%")
    try:
        bp = round(float(text) * 100)
    except ValueError as exc:
        raise ValidationError(f"{field}必须是 0~100 的百分数") from exc
    if not (0 <= bp <= 10000):
        raise ValidationError(f"{field}必须在 0% ~ 100% 之间")
    return bp


def bp_to_percent(bp: int | None) -> str:
    if bp is None:
        return "-"
    return f"{bp / 100:.2f}%"


def enumerate_minutes(start: datetime, end: datetime):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(minutes=1)


def span_minutes(start: datetime, end: datetime) -> int:
    """含两端的分钟数，支持全年（闰年 366 天 = 527040）。"""
    return int((end - start).total_seconds() // 60) + 1
