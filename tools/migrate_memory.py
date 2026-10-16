"""升级本地配置和数据库，不连接微信、不调用模型、不发送消息。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wxbot_framework.config import ConfigStore
from wxbot_framework.knowledge import KnowledgeStore
from wxbot_framework.members import MemberStore
from wxbot_framework.locking import InstanceLock
from wxbot_framework.maintenance import maintenance_options


def save_config(path, data):
    original = path.read_bytes()
    replacement = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if original == replacement:
        return
    backups = path.parent / ".runtime/backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    (backups / (path.name + "." + stamp + ".bak")).write_bytes(original)
    descriptor, temporary = tempfile.mkstemp(prefix=".memory_config_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(replacement)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate(config_path):
    with InstanceLock(ROOT / ".runtime/bot.lock"):
        return _migrate_locked(config_path)


def _migrate_locked(config_path):
    config_path = Path(config_path).resolve()
    config = ConfigStore(config_path).load()
    members_path = config_path.parent / config.get("members_config_file", "members_config.json")
    members_data = json.loads(members_path.read_text(encoding="utf-8-sig"))
    members_data.update(history_enabled=True, daily_clear=False)
    members_data.setdefault("owner_wxid", None)
    config.setdefault("memory", {"enabled": True, "batch_size": 50, "flush_seconds": 600,
                                 "seed_file": ".runtime/group_knowledge_seed.json"})
    config.setdefault("hourly_chat", {"enabled": True, "start_hour": 9, "end_hour": 23, "context_limit": 200})
    config.setdefault("plugins", {}).setdefault("knowledge", {})["enabled"] = True
    save_config(members_path, members_data)
    save_config(config_path, config)
    members = MemberStore.from_config(config, config_path.parent)
    try:
        members.initialize()
        knowledge = KnowledgeStore(members)
        upgrade = maintenance_options(config)["upgrade_file"]
        knowledge.initialize(config_path.parent / config["memory"]["seed_file"],
                             upgrade_file=config_path.parent / upgrade if upgrade else None)
        return knowledge.status()
    finally:
        members.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "bot_config.json")
    result = migrate(parser.parse_args().config)
    print(json.dumps({"migrated": True, **result}, ensure_ascii=False))
