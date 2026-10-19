"""统一 UTC 存储与本地时间展示。"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
DEFAULT_DISPLAY_TIMEZONE = "Asia/Shanghai"
BEIJING = ZoneInfo("Asia/Shanghai")


def now_utc():
    return datetime.now(UTC)


def iso_utc(value=None):
    """把时间规范化为带 Z 的 UTC 文本；旧的无时区输入按 UTC 解释。"""
    if isinstance(value, str):
        value = parse_utc(value)
    elif isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min, tzinfo=UTC)
    if value is None:
        value = now_utc()
    if not isinstance(value, datetime):
        raise TypeError("时间应为 datetime、日期或 ISO 8601 文本")
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value):
    """解析新旧混合时间；无时区的历史值按 UTC，日期型旧值从当天零点解释。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min, tzinfo=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("时间文本不能为空")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.combine(date.fromisoformat(text), time.min, tzinfo=UTC)
            except ValueError:
                raise ValueError(f"无效的 ISO 8601 时间：{value!r}") from None
    else:
        raise TypeError("时间应为 datetime、日期或 ISO 8601 文本")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_display_timezone(name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("display_timezone 应为 IANA 时区名称")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"未知时区：{name}") from None


def display_timezone(name=None):
    return validate_display_timezone(name or DEFAULT_DISPLAY_TIMEZONE)


def format_local(value, timezone_name=None, *, pattern="%Y-%m-%d %H:%M"):
    zone = display_timezone(timezone_name)
    return parse_utc(value).astimezone(zone).strftime(pattern)


def beijing_day(value=None):
    return parse_utc(value).astimezone(BEIJING).date().isoformat()
