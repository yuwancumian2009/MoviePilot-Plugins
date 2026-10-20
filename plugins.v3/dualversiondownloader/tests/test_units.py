"""双版本补全 v1.3.0 纯逻辑单测：电影限制 / 类别判定 / 候选过滤 / 冷却键。

不依赖真实的 MoviePilot 运行环境：先把 app.* 打桩，再加载插件模块。
运行：python3 tests/test_units.py
"""
import importlib.util
import sys
import types
from datetime import datetime, timedelta
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


class _FakeRequestUtils:
    def __init__(self, *a, **k):
        pass

    def get(self, *a, **k):
        return None

    def post(self, *a, **k):
        return None


class _FakeMediaChain:
    def recognize_media(self, **kwargs):
        return None


_stub("app")
_stub("app.chain")
_stub("app.chain.download", DownloadChain=object)
_stub("app.chain.search", SearchChain=object)
_stub("app.chain.media", MediaChain=_FakeMediaChain)
_stub("app.sdk")
_stub("app.sdk.events", Event=object, eventmanager=_EventManager)
_stub("app.sdk.logging", logger=_Logger())
_stub("app.sdk.network", RequestUtils=_FakeRequestUtils)
_stub("app.sdk.plugin", _PluginBase=_FakePluginBase)
_stub("app.db")
_stub("app.db.oper")
_stub("app.db.oper.site", SiteOper=object)
_stub("app.schemas")
_stub("app.schemas.category", ClassificationPolicy=None)
_stub(
    "app.schemas.types",
    EventType=types.SimpleNamespace(DownloadAdded="download.added", TransferComplete="transfer.complete"),
    MediaType=types.SimpleNamespace(
        MOVIE=types.SimpleNamespace(value="电影"),
        TV=types.SimpleNamespace(value="电视剧"),
    ),
    MessageType=types.SimpleNamespace(Plugin="Plugin"),
    SystemConfigKey=types.SimpleNamespace(
        MediaClassificationPolicy="MediaClassificationPolicy",
        Directories="Directories",
        MediaServers="MediaServers",
    ),
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


# ---------------------------------------------------------------- v1.4.0 新增
check("质量识别 BluRay", DualVersionDownloader._quality_of("Film 2023 1080p BluRay x264"), "BluRay")
check("质量识别 Remux", DualVersionDownloader._quality_of("Film 2023 1080p Blu-ray REMUX"), "Remux")
check("质量识别 WEB-DL", DualVersionDownloader._quality_of("Film 2023 1080p WEB-DL H265"), "WEB-DL")
check("质量识别 HDTV", DualVersionDownloader._quality_of("Film 2023 1080p HDTV x264"), "HDTV")
check("编码识别 H265", DualVersionDownloader._codec_of("Film 1080p WEB-DL HEVC"), "H265")
check("编码识别 H264", DualVersionDownloader._codec_of("Film 1080p WEB-DL x264"), "H264")
check("编码识别 AV1", DualVersionDownloader._codec_of("Film 1080p WEB-DL AV1"), "AV1")
check("关键词表拆分", DualVersionDownloader._words("中字, 中语 国语"), ["中字", "中语", "国语"])

q_plugin = _plugin(enabled=True, quality=["WEB-DL"], video_codec=["H265"])
q_cands = [
    _cand("Film 2023 1080p BluRay x265"),
    _cand("Film 2023 1080p WEB-DL H265"),
    _cand("Film 2023 1080p WEB-DL x264"),
]
check("质量+编码筛选", [c.torrent_info.title for c in q_plugin._filter_candidates(q_cands)],
      ["Film 2023 1080p WEB-DL H265"])

kw_plugin = _plugin(enabled=True, require_words="中字,中语")
kw_cands = [
    _cand("Film 2023 1080p WEB-DL 中字"),
    _cand("Film 2023 1080p WEB-DL 中字 中语"),
    _cand("Film 2023 1080p WEB-DL 中语"),
]
check("必须全部命中", [c.torrent_info.title for c in kw_plugin._filter_candidates(kw_cands)],
      ["Film 2023 1080p WEB-DL 中字 中语"])

any_plugin = _plugin(enabled=True, any_words="中字,国语")
check("任选其一命中", [c.torrent_info.title for c in any_plugin._filter_candidates(kw_cands)],
      ["Film 2023 1080p WEB-DL 中字", "Film 2023 1080p WEB-DL 中字 中语"])

size_plugin = _plugin(enabled=True, min_size_gb=5)
size_cands = [_cand("Film A 1080p WEB-DL", size_gb=2.0), _cand("Film B 1080p WEB-DL", size_gb=8.0)]
check("体积下限生效", [c.torrent_info.title for c in size_plugin._filter_candidates(size_cands)],
      ["Film B 1080p WEB-DL"])

check("站点列转 int", DualVersionDownloader._as_site_ids([13, "26", {"value": 39}, "x"]), [13, 26, 39])
check("站点逗号串", DualVersionDownloader._as_site_ids("13, 26"), [13, 26])
check("站点空值", DualVersionDownloader._as_site_ids(None), [])
check("站点去重", DualVersionDownloader._as_site_ids([13, 13, "13"]), [13])

lib_plugin = _plugin(enabled=True, library_enabled=True, library_names=["外语电影"], library_limit=2)
lib_plugin._emby_libraries = lambda: [{"Name": "外语电影", "ItemId": "5693"}]
lib_plugin._emby_movies = lambda pid: [
    {"Name": "阿凡达", "ProductionYear": 2009, "ProviderIds": {"Tmdb": "19995"},
     "Path": "/影视/电影/外语电影/阿凡达 [tmdbid-19995]/阿凡达.2009.UHD.2160p.strm"},
    {"Name": "只有1080", "ProductionYear": 2020, "ProviderIds": {"Tmdb": "111"},
     "Path": "/影视/电影/外语电影/只有1080 [tmdbid-111]/只有1080.2020.1080p.strm"},
    {"Name": "双版本", "ProductionYear": 2010, "ProviderIds": {"Tmdb": "222"},
     "Path": "/影视/电影/外语电影/双版本 [tmdbid-222]/双版本.2160p.strm"},
    {"Name": "双版本", "ProductionYear": 2010, "ProviderIds": {"Tmdb": "222"},
     "Path": "/影视/电影/外语电影/双版本 [tmdbid-222]/双版本.1080p.strm"},
    {"Name": "无标识", "ProductionYear": 2018, "ProviderIds": {},
     "Path": "/影视/电影/外语电影/无标识/无标识.2160p.strm"},
]
lib_groups = lib_plugin._collect_library_movies()
check("库扫描 4K 无 1080 命中", lib_groups["tmdb:19995"]["has4k"], True)
check("库扫描 4K 无 1080 未标记 1080", lib_groups["tmdb:19995"]["has1080"], False)
check("库扫描 1080 不算 4K", lib_groups["tmdb:111"]["has4k"], False)
check("库扫描 双版本归并", (lib_groups["tmdb:222"]["has4k"], lib_groups["tmdb:222"]["has1080"]), (True, True))
check("库扫描 无 TMDB 用目录归并", any(k.startswith("dir:") for k in lib_groups), True)

lib_plugin._library_history = {"tmdb:19995": datetime.now().isoformat(timespec="seconds")}
check("库补齐冷却命中", lib_plugin._library_in_cooldown("tmdb:19995"), True)
check("库补齐冷却未命中", lib_plugin._library_in_cooldown("tmdb:111"), False)

path_plugin = _plugin(enabled=True)
path_plugin.systemconfig = types.SimpleNamespace(get=lambda key, *a, **k: [
    {"download_path": "/其他/", "library_path": "/影视/电影/字幕/", "monitor_type": "downloader"},
    {"download_path": "/视频/qb/电影/", "library_path": "/影视/电影/", "monitor_type": "downloader"},
])
check("自动推断电影下载目录", path_plugin._resolve_library_save_path(), "/视频/qb/电影/")
check("配置优先于自动推断",
      _plugin(enabled=True, library_save_path="/自定义/")._resolve_library_save_path(), "/自定义/")

form_plugin = _plugin(enabled=True)
_, form_defaults = form_plugin.get_form()
check("默认配置含媒体库补齐开关", "library_enabled" in form_defaults, True)
check("默认站点为多选列表", form_defaults.get("sites"), [])
check("默认筛选字段齐全",
      all(k in form_defaults for k in ("quality", "video_codec", "require_words", "any_words", "min_size_gb")), True)


# 类别 ID 与名称不一致时按名称兜底（v1.4.0 修复）
name_plugin = _plugin(enabled=True, categories=["3"])
name_plugin._category_names = {"3": "外语电影"}
name_plugin._categories = ["3"]
name_plugin._resolve_category = lambda _m: ("99", "外语电影")
check("类别ID不符但名称命中", name_plugin._category_pass(object()), (True, "外语电影"))
name_plugin._resolve_category = lambda _m: ("99", "华语电影")
check("类别ID与名称都不符", name_plugin._category_pass(object())[0], False)

svc_plugin = _plugin(enabled=True, library_enabled=True, library_names=["外语电影"], library_cron="0 4 * * *")
_services = svc_plugin.get_service()
check("cron 服务已注册", len(_services), 1)
check("cron 触发器为 CronTrigger", type(_services[0]["trigger"]).__name__, "CronTrigger")
check("非法 cron 不注册", _plugin(enabled=True, library_enabled=True, library_names=["x"], library_cron="bad cron").get_service(), [])

# ---------------------------------------------------------------- 扫描进度 / 并发锁（v1.5.0）
prog = _plugin(enabled=True, library_enabled=True, library_names=["动画电影"])
prog._data["scan_lock"] = {"heartbeat": datetime.now().isoformat(timespec="seconds")}
check("心跳新鲜=运行中", prog._scan_is_running(), True)
prog._data["scan_lock"] = {"heartbeat": "2020-01-01T00:00:00"}
check("心跳过期=空闲", prog._scan_is_running(), False)
prog._data["library_progress"] = {
    "current": "奇迹梦之队（2026）", "index": 2, "total": 5, "downloaded": 1
}
prog._data["scan_lock"] = {"heartbeat": datetime.now().isoformat(timespec="seconds")}
msg = prog.api_scan_progress()["message"]
check("进度文案带片名与 2/5", ("奇迹梦之队" in msg and "2/5" in msg), True)
check("运行中再次点扫描被拒", prog.api_scan()["started"], False)
prog._data["scan_lock"] = {}
prog._scan_running = False
prog._data["library_last"] = {
    "started_at": "2026-10-20T14:14:34", "scanned": 107, "pending": 32,
    "downloaded": 1, "missed": 0, "skipped": 0, "failed": 0,
}
check("空闲时回显上次结果", "库内 107 部" in prog.api_scan_progress()["message"], True)
check("未选媒体库时不启动", _plugin(enabled=True, library_enabled=True).api_scan()["started"], False)
check("未开启补齐时不启动", _plugin(enabled=True, library_enabled=False, library_names=["动画电影"]).api_scan()["started"], False)

# 收尾阶段必须解除「运行中」状态（v1.5.0 实测 bug：done 阶段又续了一次心跳）
done_plugin = _plugin(enabled=True, library_enabled=True, library_names=["动画电影"])
done_plugin._data["scan_lock"] = {"heartbeat": datetime.now().isoformat(timespec="seconds")}
done_plugin._data["library_progress"] = {"stage": "searching", "current": "X"}
check("搜索中=运行中", done_plugin._scan_is_running(), True)
done_plugin._update_scan_progress(
    stage="done", current="", index=0, total=0, downloaded=0, missed=1, failed=0
)
check("done 阶段=空闲", done_plugin._scan_is_running(), False)

# 筛选统计：能区分「站点没有 1080p」和「被规则滤掉」（v1.5.0）
fs = _plugin(enabled=True, require_words="国语,中字")
picked = fs._filter_candidates([_cand("Movie 2024 1080p WEB-DL H264 AAC-GRP")])
check("必须包含多词时全滤", len(picked), 0)
check("统计记录 1080p 候选数", fs._last_filter_stats.get("pix_ok"), 1)
check("统计给出原因", "国语" in str(fs._last_filter_stats.get("reason")), True)
fa = _plugin(enabled=True, any_words="国语,中字")
check("任选关键词可放行", len(fa._filter_candidates([_cand("Movie 2024 1080p WEB-DL 中字 H264")])), 1)

# 关键词匹配口径 + 宽松兜底（v1.5.0）
kw = _plugin(enabled=True, require_words="国语,中字")
cj = _cand("Movie 2023 1080p WEB-DL H264-GRP")
cj.torrent_info.description = "国语中字 / 简繁字幕"
check("副标题里的国语中字能命中", len(kw._filter_candidates([cj])), 1)

strict_zero = _plugin(enabled=True, require_words="国语,中字,粤语")
c2 = _cand("Movie 2023 1080p WEB-DL H264-GRP")
check("严格规则全滤", len(strict_zero._filter_candidates([c2])), 0)
check("宽松兜底放行", len(strict_zero._filter_candidates([c2], strict_words=False)), 1)

ex_plugin = _plugin(enabled=True, exclude_words="CAM", require_words="国语,中字")
c3 = _cand("Movie 2023 1080p CAM-GRP")
check("宽松兜底仍尊重排除词", len(ex_plugin._filter_candidates([c3], strict_words=False)), 0)

# 手动不限量 + 详情页按钮可点（v1.6.0）
man = _plugin(enabled=True, library_enabled=True, library_names=["动画电影"], library_limit=3)
man._scan_limit_override = 0
check("手动补全描述=全部待补", man._scan_limit_desc(), "全部待补")
man._scan_limit_override = None
check("默认按配置上限", man._scan_limit_desc(), "最多 3 部")


def _walk_btns(node, out):
    if isinstance(node, dict):
        if node.get("component") == "VBtn":
            out.append(node)
        for val in node.values():
            _walk_btns(val, out)
    elif isinstance(node, list):
        for val in node:
            _walk_btns(val, out)
    return out


def _walk(node):
    """把渲染树摊平成节点列表。"""
    out = []
    if isinstance(node, dict):
        out.append(node)
        for key in ("content",):
            for child in node.get(key) or []:
                out.extend(_walk(child))
    elif isinstance(node, list):
        for child in node:
            out.extend(_walk(child))
    return out


page = man.get_page()
page_html = "".join(str(item.get("html") or "") for card in page for item in (card.get("content") or [])) + \
    "".join(str(inner.get("html") or "") for card in page for item in (card.get("content") or [])
            for inner in (item.get("content") or []))
check("详情页有原生按钮条（v-html）", page_html.count("<button") >= 4, True)
check("详情页按钮带 onclick fetch", "fetch('/api/v1/plugin/DualVersionDownloader/scan" in page_html, True)
check("详情页有「全部待补」按钮（limit=0）", "scan?limit=0" in page_html, True)
check("详情页有结果回显区 #dv-live", 'id="dv-live"' in page_html, True)
check("详情页不再用 events（宿主点击后不刷新数据）", "events" not in str(page), True)
form_schema, _defaults = man.get_form()
form_btns = _walk_btns(form_schema, [])
check("配置页不再放操作按钮（按钮统一在详情页）", len(form_btns) == 0, True)
_alerts = [n for n in _walk(form_schema) if n.get("component") == "VAlert"]
_long = [a for a in _alerts if len(str(a.get("text") or "")) > 200]
check("配置页不留长篇提示（>200字的 VAlert 已清空）", _long == [], True)
check("配置页保留状态提示（一条短 VAlert）", 1 <= len(_alerts) <= 2, True)
_hints = [n for n in _walk(form_schema) if "hint" in (n.get("props") or {})]
check("必要填写指引（hint）仍保留", len(_hints) >= 3, True)

# ------------------------------------------------- 在途/未命中 两本账
_p2 = _plugin(enabled=True, library_enabled=True, library_names=["动画电影"], library_cooldown_days=7)
_now = datetime.now().isoformat(timespec="seconds")
_p2._library_history = {"tmdb:1": _now}
check("刚补下的算在途（手动全量不重复补）", _p2._library_in_flight("tmdb:1"), True)
_p2._library_missed = {"tmdb:2": _now}
check("未命中的不算在途（手动全量可立刻重试）", _p2._library_in_flight("tmdb:2"), False)
check("未命中的仍进冷却（定时扫描不反复撞站）", _p2._library_in_cooldown("tmdb:2"), True)
_old = (datetime.now() - timedelta(hours=13)).isoformat(timespec="seconds")
_p2._library_history = {"tmdb:3": _old}
check("补下超过 12 小时仍未入库 → 不算在途，会重试", _p2._library_in_flight("tmdb:3"), False)
check("扫描结果里带 in_flight 字段（页面能显示）", "in_flight" in (ROOT / "__init__.py").read_text(encoding="utf-8"), True)

# ------------------------------------------------- 排除词必须按词边界匹配
_cls = type(man)
check("排除词不误伤 DTS 音轨", _cls.hit_word("TS", "the secret life of pets 2 2019 bluray 1080p x264 dts 7.1 4audios-cmct"), False)
check("排除词不误伤 HTC/DTS-HD", _cls.hit_word("TC", "movie 2019 1080p bluray dts-hd ma 5.1 x264"), False)
check("排除词能命中真实 TS 版本", _cls.hit_word("TS", "movie 2019 ts x264 aac"), True)
check("排除词能命中 CAM", _cls.hit_word("CAM", "movie 2026 cam xvid"), True)
check("排除词命中分隔符夹住的 TS", _cls.hit_word("TS", "movie.2019.TS.1080p.mkv"), True)
check("中文关键词按子串匹配", _cls.hit_word("中字", "某某电影 简中字幕 1080p"), True)
check("中文关键词未出现则不命中", _cls.hit_word("国语", "某某电影 简中字幕 1080p"), False)

# ---------------------------------------------------------------- 结果
passed = sum(1 for _, ok in results if ok)
print(f"\n共 {len(results)} 项，通过 {passed} 项，失败 {len(results) - passed} 项")
sys.exit(0 if passed == len(results) else 1)
