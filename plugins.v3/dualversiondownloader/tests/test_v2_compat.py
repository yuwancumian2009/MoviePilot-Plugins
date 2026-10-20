"""双版本补全 v2 兼容自检：把 app.* 按 MoviePilot v2 的结构打桩，再加载同一份插件代码。

验证点：
1. 在 v2 模块布局下能成功导入（走 v2 分支，IS_V3=False）
2. 电影识别调用的是 v2 签名（source/tmdbid），不是 v3 的 media_source/media_id
3. 详情页按钮在 v2 用 events 点击（v2 前端点击后会重新拉取页面数据）
4. 插件接口仍声明 auth=bear，v2 前端带 JWT 也能调
5. 配置页不含长篇提示、按钮不下发到配置页

运行：python3 tests/test_v2_compat.py
"""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

results = []


def check(name, got, want):
    ok = got == want
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"：得到 {got!r}，期望 {want!r}"))


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _FakePluginBase:
    def __init__(self):
        self._data = {}
        self.messages = []
        self.systemconfig = _FakeSystemConfig()

    def get_data(self, key=None, plugin_id=None):
        return self._data.get(key)

    def save_data(self, key, value, plugin_id=None):
        self._data[key] = value

    def update_config(self, config):
        self._config = config

    def post_message(self, **kwargs):
        self.messages.append(kwargs)


class _FakeSystemConfig:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)


class _EventManager:
    @staticmethod
    def register(*_args, **_kwargs):
        def deco(func):
            return func
        return deco


class _Logger:
    def info(self, *a, **k):
        pass

    debug = info
    warn = info
    warning = info
    error = info


CALLS = []


class _FakeMediaChain:
    """记录 recognize_media 的调用参数，用来确认走的是 v2 签名。"""

    def recognize_media(self, **kwargs):
        CALLS.append(kwargs)
        return types.SimpleNamespace(
            title="测试电影", year="2019", type=types.SimpleNamespace(value="电影"), tmdb_id="27205"
        )


# ---- v2 的模块布局（注意：没有 app.sdk.*，也没有 app.db.oper）
_stub("app")
_stub("app.plugins", _PluginBase=_FakePluginBase)
_stub("app.log", logger=_Logger())
_stub("app.core")
_stub("app.core.event", Event=object, eventmanager=_EventManager)
_stub("app.utils")
_stub("app.utils.http", RequestUtils=lambda *a, **k: None)
_stub("app.chain")
_stub("app.chain.download", DownloadChain=object)
_stub("app.chain.search", SearchChain=object)
_stub("app.chain.media", MediaChain=_FakeMediaChain)
_stub("app.db")
_stub("app.db.site_oper", SiteOper=object)
_stub("app.schemas")
_stub(
    "app.schemas.types",
    EventType=types.SimpleNamespace(DownloadAdded="download.added"),
    MediaType=types.SimpleNamespace(MOVIE=types.SimpleNamespace(value="电影")),
    NotificationType=types.SimpleNamespace(Plugin="插件", Download="资源下载"),
    SystemConfigKey=types.SimpleNamespace(Directories="Directories", MediaServers="MediaServers"),
)

_spec = importlib.util.spec_from_file_location("dvd_plugin_v2", ROOT / "__init__.py")
plugin_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin_mod)

check("v2 布局下导入成功且判定为非 v3", plugin_mod.IS_V3, False)
check("模块级未导入 app.sdk", any(m.startswith("app.sdk") for m in sys.modules), False)

# ---------------------------------------------------------------- 识别签名
plugin_mod.recognize_movie("27205")
check("识别调用次数", len(CALLS), 1)
call = CALLS[0] if CALLS else {}
check("v2 识别参数字段", sorted(call.keys()), ["mtype", "source", "tmdbid"])
check("v2 识别 source=themoviedb", call.get("source"), "themoviedb")
check("v2 识别 tmdbid 为整数", call.get("tmdbid"), 27205)

# ---------------------------------------------------------------- 页面 / 表单
plugin = plugin_mod.DualVersionDownloader()
plugin.init_plugin({"enabled": True, "library_enabled": True, "library_names": ["华语电影"], "library_cron": "0 4 * * *"})

apis = plugin.get_api()
check("接口声明 auth=bear", all(a.get("auth") == "bear" for a in apis), True)

page = plugin.get_page()
page_str = str(page)
check("v2 详情页用 events 点击", "events" in page_str, True)
check("v2 详情页不再用原生 fetch 按钮", "fetch('/api/v1" in page_str, False)
check("v2 详情页有 4 个操作按钮", page_str.count("'events'"), 4)
check("v2 事件接口指向插件路径", "/plugin/DualVersionDownloader/scan" in page_str, True)
check("v2 含「全部待补」入口", "limit=0" in page_str, True)


def _walk(node):
    out = []
    if isinstance(node, dict):
        out.append(node)
        for child in node.get("content") or []:
            out.extend(_walk(child))
    elif isinstance(node, list):
        for child in node:
            out.extend(_walk(child))
    return out


form, defaults = plugin.get_form()
nodes = _walk(form)
alerts = [n for n in nodes if n.get("component") == "VAlert"]
check("配置页无长篇提示", [a for a in alerts if len(str(a.get("text") or "")) > 200], [])
check("配置页不下发按钮", [n for n in nodes if n.get("component") == "VBtn"], [])
check("配置页保留必要 hint", len([n for n in nodes if "hint" in (n.get("props") or {})]) >= 3, True)
check("默认配置含媒体库补齐项", "library_names" in defaults and "library_cron" in defaults, True)

passed = sum(1 for _, ok in results if ok)
print(f"\n共 {len(results)} 项，通过 {passed} 项，失败 {len(results) - passed} 项")
sys.exit(0 if passed == len(results) else 1)
