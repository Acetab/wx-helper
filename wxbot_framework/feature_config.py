"""新增后台功能的配置校验；不开启时不创建线程或请求模型。"""
import math


def feature_options(config):
    if not isinstance(config.get("memory", {}), dict) or not isinstance(config.get("hourly_chat", {}), dict):
        raise ValueError("memory 和 hourly_chat 应为JSON对象")
    memory = {"enabled": False, "batch_size": 50, "flush_seconds": 600,
              "seed_file": ".runtime/group_knowledge_seed.json", **config.get("memory", {})}
    hourly = {
        "enabled": False,
        "start_hour": 9,
        "end_hour": 23,
        "evening_start_hour": 18,
        "active_min_minutes": 120,
        "active_max_minutes": 180,
        "evening_min_minutes": 180,
        "evening_max_minutes": 300,
        "activity_window_minutes": 180,
        "daily_limit": 6,
        "cooldown_minutes": 45,
        "stale_candidate_minutes": 15,
        "min_messages": 2,
        "min_speakers": 2,
        "context_limit": 200,
        **config.get("hourly_chat", {}),
    }
    for key, settings in (("memory", memory), ("hourly_chat", hourly)):
        if type(settings["enabled"]) is not bool:
            raise ValueError(key + ".enabled 必须是布尔值")
    numeric = (
        (memory, "batch_size", 1, 200),
        (hourly, "start_hour", 0, 23),
        (hourly, "end_hour", 1, 24),
        (hourly, "evening_start_hour", 0, 24),
        (hourly, "active_min_minutes", 15, 1440),
        (hourly, "active_max_minutes", 15, 1440),
        (hourly, "evening_min_minutes", 15, 1440),
        (hourly, "evening_max_minutes", 15, 1440),
        (hourly, "activity_window_minutes", 1, 1440),
        (hourly, "daily_limit", 1, 24),
        (hourly, "cooldown_minutes", 1, 1440),
        (hourly, "stale_candidate_minutes", 1, 1440),
        (hourly, "min_messages", 1, 100),
        (hourly, "min_speakers", 1, 100),
        (hourly, "context_limit", 1, 5000),
    )
    for settings, key, lower, upper in numeric:
        if type(settings[key]) is not int or not lower <= settings[key] <= upper:
            raise ValueError(f"{key} 应为{lower}–{upper}的整数")
    if hourly["start_hour"] >= hourly["end_hour"]:
        raise ValueError("主动发言 start_hour 必须早于 end_hour")
    # evening_start_hour 允许落在窗口外：窄窗口可能整段都在傍晚或白天，
    # 此时窗口内的所有时刻自然归入同一档，不再因默认值 18 拒绝启动。
    for prefix in ("active", "evening"):
        if hourly[f"{prefix}_min_minutes"] > hourly[f"{prefix}_max_minutes"]:
            raise ValueError(f"{prefix}_min_minutes 不能大于 {prefix}_max_minutes")
    if hourly["min_speakers"] > hourly["min_messages"]:
        raise ValueError("min_speakers 不能大于 min_messages")
    seconds = memory["flush_seconds"]
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 1:
        raise ValueError("memory.flush_seconds 应为至少1秒的有限数值")
    if not isinstance(memory["seed_file"], str) or not memory["seed_file"].strip() or "\0" in memory["seed_file"]:
        raise ValueError("memory.seed_file 应为本地JSON路径")
    hourly = {"enabled": config.get("hourly_chat", {}).get("enabled", False),
              **{key: value for key, value in hourly.items() if key != "enabled"}}
    return memory, hourly
