"""新增后台功能的配置校验；不开启时不创建线程或请求模型。"""
import math


def feature_options(config):
    if not isinstance(config.get("memory", {}), dict) or not isinstance(config.get("hourly_chat", {}), dict):
        raise ValueError("memory 和 hourly_chat 应为JSON对象")
    memory = {"enabled": False, "batch_size": 50, "flush_seconds": 600,
              "seed_file": ".runtime/group_knowledge_seed.json", **config.get("memory", {})}
    hourly = {"enabled": False, "start_hour": 9, "end_hour": 23, "context_limit": 200,
              **config.get("hourly_chat", {})}
    for key, settings in (("memory", memory), ("hourly_chat", hourly)):
        if type(settings["enabled"]) is not bool:
            raise ValueError(key + ".enabled 必须是布尔值")
    for settings, key, lower, upper in ((memory, "batch_size", 1, 200), (hourly, "start_hour", 0, 23),
                                       (hourly, "end_hour", 0, 23), (hourly, "context_limit", 1, 5000)):
        if type(settings[key]) is not int or not lower <= settings[key] <= upper:
            raise ValueError(f"{key} 应为{lower}—{upper}的整数")
    if hourly["start_hour"] > hourly["end_hour"]:
        raise ValueError("定时发言 start_hour 不能晚于 end_hour")
    seconds = memory["flush_seconds"]
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 1:
        raise ValueError("memory.flush_seconds 应为至少1秒的有限数值")
    if not isinstance(memory["seed_file"], str) or not memory["seed_file"].strip() or "\0" in memory["seed_file"]:
        raise ValueError("memory.seed_file 应为本地JSON路径")
    return memory, hourly
