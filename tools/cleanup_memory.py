"""人物与公共记忆清理：先备份、可回滚，默认只报告，加 --apply 才写入。

用法：
    python tools/cleanup_memory.py --config bot_config.json            # 只报告
    python tools/cleanup_memory.py --config bot_config.json --apply    # 执行清理

清理规则：
1. 无人绑定、没有任何记忆条目、也没有人工修订记录的自动人物档案 → 隔离（可恢复，资料不删除）。
2. 群公共记忆里明确的回复格式要求、一次性机器人测试、纯功能反馈 → 停用（原文与来源保留）。
   混合条目只停用被判定为功能反馈的部分，群文化内容不受影响。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wxbot_framework.config import ConfigStore
from wxbot_framework.database import backup_database
from wxbot_framework.knowledge import KnowledgeStore
from wxbot_framework.locking import InstanceLock
from wxbot_framework.maintenance import maintenance_options
from wxbot_framework.members import MemberStore

# 只有整条都在讲机器人功能时才清理；群文化内容不会命中。
POLLUTION_RULES = (
    ("回复格式要求", ("只回两个字", "只回2个字", "回复格式", "输出的格式", "不要用json", "不要用 json")),
    ("一次性机器人测试", ("测试机器人", "机器人测试", "test bot", "试试它")),
    ("纯功能反馈", ("没人味", "语气生硬", "像客服", "认人不全", "认不全", "不认识我", "识别不了",
                    "机器人目前", "机器人暂时", "建议大伙先写记忆")),
)


def classify_pollution(content):
    text = (content or "").casefold()
    for reason, keywords in POLLUTION_RULES:
        if any(keyword in text for keyword in keywords):
            return reason
    return None


def scan(knowledge):
    db = knowledge._db()
    account = knowledge.account
    empty_people = [dict(row) for row in db.execute("""
        SELECT e.* FROM knowledge_entities e WHERE e.account=? AND e.entity_key LIKE 'person:%'
        AND e.wxid IS NULL AND e.status='active'
        AND NOT EXISTS (SELECT 1 FROM knowledge_items i WHERE i.entity_id=e.id)
        AND NOT EXISTS (SELECT 1 FROM knowledge_entity_edits ed WHERE ed.entity_id=e.id
                        AND ed.account=e.account AND ed.room=e.room)
        ORDER BY e.id""", (account,))]
    polluted = []
    for row in db.execute("""
        SELECT i.id,i.room,i.slot,i.kind,i.content,i.manual,i.status,e.entity_key
        FROM knowledge_items i JOIN knowledge_entities e ON e.id=i.entity_id
        WHERE i.account=? AND i.status='active' AND e.entity_key LIKE 'group:%'
        ORDER BY i.id""", (account,)):
        if row["manual"]:
            continue
        reason = classify_pollution(row["content"])
        if reason:
            polluted.append((dict(row), reason))
    return empty_people, polluted


def apply(knowledge, empty_people, polluted):
    db = knowledge._db()
    with knowledge._lock, db:
        for entity in empty_people:
            db.execute("UPDATE knowledge_entities SET status='quarantined' WHERE id=?", (entity["id"],))
            db.execute("""INSERT INTO knowledge_entity_edits
                (account,room,entity_id,editor,action,previous,replacement,edited_at) VALUES(?,?,?,?,?,?,?,?)""",
                (knowledge.account, entity["room"], entity["id"], "cleanup-tool", "quarantine",
                 json.dumps(entity, ensure_ascii=False), json.dumps({"status": "quarantined"}, ensure_ascii=False),
                 knowledge.now()))
        for item, reason in polluted:
            db.execute("UPDATE knowledge_items SET status='forgotten',manual=1,revision=revision+1,updated_at=? WHERE id=?",
                       (knowledge.now(), item["id"]))
            db.execute("INSERT INTO knowledge_edits(item_id,editor,action,previous,replacement,edited_at) VALUES(?,?,?,?,?,?)",
                       (item["id"], "cleanup-tool", "forget-pollution", item["content"],
                        f"[{reason}] {item['content']}", knowledge.now()))


def cleanup(config_path, *, dry_run=True):
    with InstanceLock(ROOT / ".runtime/bot.lock"):
        config = ConfigStore(Path(config_path).resolve()).load()
        directory = Path(config_path).resolve().parent
        members = MemberStore.from_config(config, directory)
        if members is None:
            raise SystemExit("配置未启用成员目录，无法清理记忆库")
        try:
            members.initialize()
            knowledge = KnowledgeStore(members)
            memory = config.get("memory") or {}
            seed = memory.get("seed_file") or ".runtime/group_knowledge_seed.json"
            upgrade = maintenance_options(config)["upgrade_file"]
            knowledge.initialize(directory / seed, upgrade_file=directory / upgrade if upgrade else None)
            empty_people, polluted = scan(knowledge)
            if not dry_run:
                backup_database(knowledge._db(), members.path, "before-cleanup")
                apply(knowledge, empty_people, polluted)
            return {"dry_run": dry_run,
                    "quarantine": [(e["id"], e["name"]) for e in empty_people],
                    "disable": [(item["id"], item["slot"], reason) for item, reason in polluted],
                    "status": knowledge.status()}
        finally:
            members.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "bot_config.json")
    parser.add_argument("--apply", action="store_true", help="执行清理；默认只报告")
    arguments = parser.parse_args()
    print(json.dumps(cleanup(arguments.config, dry_run=not arguments.apply), ensure_ascii=False, indent=2))
