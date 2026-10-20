"""冷却期与媒体库刷新相关的最小断言（不需要 MoviePilot 运行时）。"""
import ast
import io
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "__init__.py")
TREE = ast.parse(io.open(SRC, encoding="utf-8").read())
CODE = io.open(SRC, encoding="utf-8").read()


def _func(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"缺少方法 {name}")


results = []


def check(desc, cond):
    results.append((desc, bool(cond)))


# 1. 手动「全部待补」必须忽略冷却
start = _func("_start_scan")
seg = ast.get_source_segment(CODE, start) or ""
check("手动全量扫描无视冷却期", "_scan_ignore_cooldown" in seg and "<= 0" in seg)

# 2. pending 过滤区分手动/自动
worker = _func("_library_scan_worker")
seg = ast.get_source_segment(CODE, worker) or ""
check("pending 过滤使用 ignore_cooldown", "ignore_cooldown" in seg)
check("冷却被跳过时给出提示", "冷却期" in seg)

# 3. 补下记录 + 刷新链路
check("补下成功记录 hash", "_remember_library_hash(" in CODE)
check("转移完成事件已注册", "EventType.TransferComplete" in CODE)
check("刷新媒体库实现存在", "_refresh_emby_libraries" in CODE and "Items/{item_id}/Refresh" in CODE)
check("扫描结束兜底刷新", "_schedule_library_refresh(delay=180" in CODE)
check("刷新开关默认开启", '"refresh_media": True' in CODE)
check("配置读取 refresh_media", '_config.get("refresh_media"' in CODE)

# 4. 表单里有开关
form = _func("get_form")
seg = ast.get_source_segment(CODE, form) or ""
check("配置页有刷新开关", '"refresh_media"' in seg)

passed = sum(1 for _, ok in results if ok)
for desc, ok in results:
    print(("PASS " if ok else "FAIL ") + desc)
print(f"\n共 {len(results)} 项，通过 {passed} 项，失败 {len(results) - passed} 项")
raise SystemExit(0 if passed == len(results) else 1)
