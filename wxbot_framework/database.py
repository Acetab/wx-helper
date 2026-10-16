"""迁移前用 SQLite 在线备份 API 保存一致的本地副本。"""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def backup_before_migration(db, path):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables or ("chat_buffers" in tables and
                      "context_floor" in {row[1] for row in db.execute("PRAGMA table_info(chat_buffers)")}):
        return None
    return backup_database(db, path, "before-memory")


def backup_database(db, path, label="manual"):
    folder = Path(path).parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = folder / (Path(path).stem + "-" + label + "-" + stamp + ".sqlite3")
    with closing(sqlite3.connect(target)) as output:
        db.backup(output)
    return target
