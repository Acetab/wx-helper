"""机器人停止后安全卸载当前目录残留的桥接，不退出微信、不发送消息。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wxbot_framework.config import ConfigStore
from wxbot_framework.locking import InstanceLock


def cleanup_loaded(pid, expected_dlls, list_modules, remove):
    loaded = list_modules(pid)
    selected = []
    # 先核对全部路径，不能卸载其他目录或其他实例使用的同名模块。
    for dll in expected_dlls:
        module = loaded.get(dll.name.lower())
        if module:
            if Path(module[1]).resolve() != dll.resolve():
                raise RuntimeError("发现其他目录的同名桥接，请先退出对应的旧机器人")
            selected.append(dll)
    removed = []
    for dll in selected:
        result = remove("--remove", pid=pid, bridge_path=dll, preserve_report=False)
        if result["code"] or not result.get("dll_unloaded"):
            raise RuntimeError(f"桥接尚忙或未完成卸载（返回码 {result['code']}），保留模块，请稍后重试")
        removed.append(dll.name)
    return {"pid": pid, "removed": removed, "clean": not any(
        dll.name.lower() in list_modules(pid) for dll in expected_dlls)}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "bot_config.json")
    args = parser.parse_args()
    try:
        config = ConfigStore(args.config).load()
        account = config.get("wechat_account")
        if not account or account == "wxid_example_robot":
            raise ValueError("请先配置实际机器人账号，恢复工具不会猜测目标微信")
        with InstanceLock(ROOT / ".runtime/bot.lock"):
            sys.path.insert(0, str(ROOT / "native"))
            from run_selftest import DLL, TEXT_DLL, modules, run
            from text_service import find_account_process, inspect_account
            pid = config.get("wechat_pid") or find_account_process(account)
            if inspect_account(pid) != account:
                raise RuntimeError("目标进程登录的不是配置中的机器人账号，未做清理")
            result = cleanup_loaded(pid, (DLL, TEXT_DLL), modules, run)
        if not result["clean"]:
            raise RuntimeError("桥接仍在加载，请检查是否有其他机器人启动")
        print("桥接残留清理完成；微信保持运行，可重新启动机器人。")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(f"清理未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
