"""双版本补全 v1.3.0 纯逻辑单测：电影限制 / 类别判定 / 候选过滤 / 冷却键。

不依赖真实的 MoviePilot 运行环境：先把 app.* 打桩，再加载插件模块。
运行：python3 tests/test_units.py
"""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- 打桩
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

    def debug(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


_stub("app")
_stub("app.chain")
_stub("app.chain.download", DownloadChain=object)
_stub("app.chain.search", SearchChain=object)
_stub("app.sdk")
_stub("app.sdk.events", Event=object, eventmanager=_EventManager)
_stub("app.sdk.logging", logger=_Logger())
_stub("app.sdk.plugin", _PluginBase=_FakePluginBase)
_stub("app.schemas")
_stub(
    "app.schemas.types",
    EventType=types.SimpleNamespace(DownloadAdded="download.added"),
    MediaType=types.SimpleNamespace(
        MOVIE=types.SimpleNamespace(value="电影"),
        TV=types.SimpleNamespace(value="电视剧"),
    ),
    MessageType=types.SimpleNamespace(Plugin="Plugin"),
    SystemConfigKey=types.SimpleNamespace(MediaClassificationPolicy="MediaClassificationPolicy"),
)

_spec = importlib.util.spec_from_file_location("dvd_plugin", ROOT / "__init__.py")
plugin_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin_mod)
DualVersionDownloader = plugin_mod.DualVersionDownloader


def _plugin(**config):
    plugin = DualVersionDownloader()
    plugin.init_plugin(config)
    return plugin


def _ctx(title="", pix="", org="", ep=None, mtype="电影"):
    media = types.SimpleNamespace(
        title=title, year="2023", type=mtype, tmdb_id="123", episode_list=ep or []
    )
    meta = types.SimpleNamespace(resource_pix=pix, org_string=org, episode_list=ep or [])
    torrent = types.SimpleNamespace(title=title, size=1024 ** 3, seeders=10, site_name="site")
    return types.SimpleNamespace(media_info=media, meta_info=meta, torrent_info=torrent)


def _cand(name, size_gb=2.0, seeders=10):
    torrent = types.SimpleNamespace(
        title=name, size=int(size_gb * 1024 ** 3), seeders=seeders, site_name="s"
    )
    meta = types.SimpleNamespace(resource_pix="", org_string="")
    return types.SimpleNamespace(torrent_info=torrent, meta_info=meta)


results = []


def check(name, actual, expected):
    ok = actual == expected
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f"：得到 {actual!r}，期望 {expected!r}"))


# ---------------------------------------------------------------- 分辨率判定
plugin = _plugin(enabled=True)
check("4K 判定 2160p", bool(plugin._RE_4K.search("Movie 2023 2160p WEB-DL")), True)
check("4K 判定 UHD", bool(plugin._RE_4K.search("Movie 2023 UHD BluRay")), True)
check("4K 判定 1080p 不命中", bool(plugin._RE_4K.search("Movie 2023 1080p BluRay")), False)
check("1080p 判定", bool(plugin._RE_1080.search("Movie 2023 1080p BluRay")), True)
check("1080i 不算 1080p", bool(plugin._RE_1080.search("Movie 2023 1080i HDTV")), False)
check(
    "_pix_text 合并三段文本",
    plugin._pix_text(types.SimpleNamespace(title="A 2160p"), types.SimpleNamespace(resource_pix="B", org_string="C")),
    "A 2160p B C",
)

# ---------------------------------------------------------------- 候选过滤
cands = [
    _cand("Film 2023 2160p UHD", 20, 100),
    _cand("Film 2023 1080p BluRay", 8, 5),
    _cand("Film 2023 1080p WEB-DL", 4, 50),
    _cand("Film 2023 1080i HDTV", 3, 80),
    _cand("Film 2023 1080p CAM", 1, 90),
    _cand("Film 2023 1080p BluRay", 30, 60),
]
picked = _plugin(enabled=True, exclude_words="CAM")._filter_candidates(cands)
check("过滤后仅 1080p 非排除项", [c.torrent_info.title for c in picked],
      ["Film 2023 1080p BluRay", "Film 2023 1080p WEB-DL", "Film 2023 1080p BluRay"])
check("默认按做种优先排序", picked[0].torrent_info.seeders, 60)
check("排除关键词生效", any("CAM" in c.torrent_info.title for c in picked), False)

picked_size = _plugin(enabled=True, exclude_words="CAM", prefer="size")._filter_candidates(cands)
check("体积优先排序", picked_size[0].torrent_info.size, 4 * 1024 ** 3)

picked_cap = _plugin(enabled=True, exclude_words="CAM", max_size_gb=10)._filter_candidates(cands)
check("体积上限生效", [c.torrent_info.size for c in picked_cap], [4 * 1024 ** 3, 8 * 1024 ** 3])

picked_seed = _plugin(enabled=True, exclude_words="CAM", min_seeders=40)._filter_candidates(cands)
check("做种下限生效", [c.torrent_info.seeders for c in picked_seed], [60, 50])

# ---------------------------------------------------------------- 配置解析
check("_as_list 逗号串", DualVersionDownloader._as_list("a, b ,c"), ["a", "b", "c"])
check("_as_list 字典列表", DualVersionDownloader._as_list([{"value": "x"}, {"id": "y"}, "z"]), ["x", "y", "z"])
check("_as_list 空值", DualVersionDownloader._as_list(None), [])

# ---------------------------------------------------------------- 媒体键与冷却
check("媒体键优先 source:media_id",
      DualVersionDownloader._media_key(types.SimpleNamespace(
          media_source=types.SimpleNamespace(value="themoviedb"), media_id="872585", tmdb_id="872585")),
      "themoviedb:872585")
check("媒体键回退 tmdb",
      DualVersionDownloader._media_key(types.SimpleNamespace(tmdb_id="872585")), "tmdb:872585")
check("媒体键缺失返回 None",
      DualVersionDownloader._media_key(types.SimpleNamespace()), None)

cooldown_plugin = _plugin(enabled=True, cooldown_days=7)
cooldown_plugin._history = {"tmdb:1": "2026-10-20T00:00:00"}
check("冷却期内命中", cooldown_plugin._in_cooldown("tmdb:1"), True)
check("未记录不算冷却", cooldown_plugin._in_cooldown("tmdb:2"), False)
check("冷却关闭后不拦截", _plugin(enabled=True, cooldown_days=0)._in_cooldown("tmdb:1"), False)

# ---------------------------------------------------------------- 类别判定
cat_plugin = _plugin(enabled=True, categories=["legacy.movie.aaa"])
cat_plugin._category_names = {"legacy.movie.aaa": "动画电影", "legacy.movie.bbb": "华语电影"}
cat_plugin._resolve_category = lambda media: ("legacy.movie.aaa", "动画电影")
check("类别命中", cat_plugin._category_pass(object()), (True, "动画电影"))
cat_plugin._resolve_category = lambda media: ("legacy.movie.bbb", "华语电影")
check("类别不符", cat_plugin._category_pass(object()), (False, "华语电影"))
cat_plugin._resolve_category = lambda media: (None, "外语电影")
check("回退按类别名匹配", cat_plugin._category_pass(object()), (False, "外语电影"))
cat_plugin._resolve_category = lambda media: (None, "动画电影")
check("回退名称命中", cat_plugin._category_pass(object()), (True, "动画电影"))
check("未选类别全部通过", _plugin(enabled=True)._category_pass(object()), (True, ""))
check("类别展示名", cat_plugin._category_label(["legacy.movie.aaa"]), "动画电影")
check("未选类别展示", cat_plugin._category_label([]), "全部电影")

# ---------------------------------------------------------------- 分类策略载荷兼容
state_payload = {"active": {"categories": [{"id": "legacy.movie.a", "name": "动画电影"}],
                            "rules": []}, "history": [{"revision": 1}]}
check("策略载荷解包 active", DualVersionDownloader._unwrap_policy_payload(state_payload),
      {"categories": [{"id": "legacy.movie.a", "name": "动画电影"}], "rules": []})
check("策略载荷直接本体", DualVersionDownloader._unwrap_policy_payload({"categories": []}), {"categories": []})
check("策略载荷字符串", DualVersionDownloader._unwrap_policy_payload('{"categories": []}'), {"categories": []})
check("策略载荷空值", DualVersionDownloader._unwrap_policy_payload(None), None)

# ---------------------------------------------------------------- 仅电影 / 表单
form, defaults = _plugin(enabled=True).get_form()
check("默认配置无剧集字段", sorted(k for k in defaults if "tv" in k or "episode" in k), [])
check("默认配置含 categories", "categories" in defaults, True)

tv_ctx = _ctx(title="Show 2023 2160p", mtype="电视剧")
tv_plugin = _plugin(enabled=True)
tv_plugin.on_download_added(types.SimpleNamespace(event_data={
    "context": tv_ctx, "source": "Subscribe|x", "hash": "abc"}))
check("剧集事件被忽略", tv_plugin._history, {})

movie_plugin = _plugin(enabled=True, only_subscribe=True)
movie_ctx = _ctx(title="Film 2023 2160p")
movie_plugin._complete = lambda *a, **k: None
movie_plugin.on_download_added(types.SimpleNamespace(event_data={
    "context": movie_ctx, "source": "Subscribe|x", "hash": "def"}))
check("电影 4K 订阅事件入库", list(movie_plugin._history.keys()), ["tmdb:123"])

plain_plugin = _plugin(enabled=True)
plain_clean = _plugin(enabled=True)
plain_clean.on_download_added(types.SimpleNamespace(event_data={
    "context": _ctx(title="Film 2023 2160p"), "source": "Search|manual", "hash": "ghi"}))
check("非订阅来源被忽略", plain_clean._history, {})

p1080 = _plugin(enabled=True)
p1080.on_download_added(types.SimpleNamespace(event_data={
    "context": _ctx(title="Film 2023 1080p"), "source": "Subscribe|x", "hash": "jkl"}))
check("1080p 下载不处理", p1080._history, {})

# ---------------------------------------------------------------- 结果
passed = sum(1 for _, ok in results if ok)
print(f"\n共 {len(results)} 项，通过 {passed} 项，失败 {len(results) - passed} 项")
sys.exit(0 if passed == len(results) else 1)
