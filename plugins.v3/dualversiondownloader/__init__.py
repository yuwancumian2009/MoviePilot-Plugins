import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.chain.download import DownloadChain
from app.chain.search import SearchChain

# ---------------------------------------------------------------------------
# 版本适配层：同一份代码同时支持 MoviePilot v3 与 v2
#   v3：SDK 收在 app.sdk.*（plugin / logging / events / network）
#   v2：沿用 app.plugins / app.log / app.core.event / app.utils.http
# ---------------------------------------------------------------------------
try:  # MoviePilot v3
    from app.sdk.events import Event, eventmanager
    from app.sdk.logging import logger
    from app.sdk.network import RequestUtils
    from app.sdk.plugin import _PluginBase
    from app.schemas.types import EventType, MediaType, MessageType, SystemConfigKey

    IS_V3 = True
except ImportError:  # MoviePilot v2
    from app.core.event import Event, eventmanager
    from app.log import logger
    from app.utils.http import RequestUtils
    from app.plugins import _PluginBase
    from app.schemas.types import EventType, MediaType, SystemConfigKey
    from app.schemas.types import NotificationType as MessageType

    IS_V3 = False

try:  # 站点数据操作：v3 归类到 app.db.oper
    from app.db.oper.site import SiteOper
except ImportError:  # v2
    from app.db.site_oper import SiteOper

try:  # 分类策略为 v3 内建能力，v2 无此模块时降级运行
    from app.schemas.category import ClassificationPolicy
except Exception:  # noqa: BLE001
    ClassificationPolicy = None  # type: ignore[assignment]


def recognize_movie(tmdb_id: Any) -> Any:
    """按 TMDB ID 识别电影：v3 用 media_source/media_id，v2 用 source/tmdbid。"""
    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return None
    from app.chain.media import MediaChain

    if IS_V3:
        from app.schemas.types import MediaSource

        return MediaChain().recognize_media(
            mtype=MediaType.MOVIE, media_source=MediaSource.TMDB, media_id=tmdb_id
        )
    media_id = int(tmdb_id) if tmdb_id.isdigit() else tmdb_id
    return MediaChain().recognize_media(
        mtype=MediaType.MOVIE, source="themoviedb", tmdbid=media_id
    )


class DualVersionDownloader(_PluginBase):
    """
    双版本补全（仅电影）。

    监听下载添加事件：当电影以 4K 版本开始下载时，按同一媒体身份再搜索一个 1080p
    版本并下载到与原任务相同的目录；原任务为 1080p 时不做任何处理。
    支持按媒体类别（动画电影 / 华语电影 / 外语电影 …）限定生效范围。
    """

    # 插件名称
    plugin_name = "双版本补全"
    # 插件描述
    plugin_desc = "电影订阅下载 4K 版本时，自动补一个 1080p 版本到同一目录；媒体库缺 1080p 的老片可手动/定时补齐（兼容 MP v2 与 v3）"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/movie.png"
    # 插件版本
    plugin_version = "1.7.9"
    version = "1.4.0"
    # 插件作者
    plugin_author = "yuwancumian"
    # 作者主页
    author_url = "https://github.com/yuwancumian2009/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "DualVersionDownloader_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 手动全量扫描时，「刚补下」多久内不再重复补（小时）
    # 115 上传 + strm 生成 + Emby 索引经常超过 12 小时，窗口取宽一些避免重复补下
    _DEFAULT_INFLIGHT_HOURS = 24

    # 4K / 1080p 判定
    _RE_4K = re.compile(r"(?i)(\b4k\b|2160[pi]|x2160|\buhd\b)")
    _RE_1080 = re.compile(r"(?i)(1080p|x1080)")
    # 库里「已有 1080 版本」的宽松判定：1080p / 1080i / 1920x1080 / "…1080.WEB-DL"（缺 p 的命名）
    _RE_1080_ANY = re.compile(r"(?i)(1080[pi]?(?![0-9])|x1080)")

    # 候选筛选：质量 / 视频编码（按顺序优先匹配）
    _QUALITY_RULES = (
        ("Remux", re.compile(r"(?i)remux")),
        ("BluRay", re.compile(r"(?i)(blu-?ray|bdrip|bd-?rip|\bbd\b)")),
        ("WEB-DL", re.compile(r"(?i)(web-?dl|webrip|\bweb\b)")),
        ("HDTV", re.compile(r"(?i)hdtv")),
    )
    _CODEC_RULES = (
        ("H265", re.compile(r"(?i)(h\.?265|x265|hevc)")),
        ("H264", re.compile(r"(?i)(h\.?264|x264|\bavc\b)")),
        ("AV1", re.compile(r"(?i)\bav1\b")),
    )
    # Emby 请求字段
    _EMBY_FIELDS = "Path,ProviderIds,ProductionYear,DateCreated,Name,MediaSources"

    # 类别缓存有效期
    _CATEGORY_CACHE_HOURS = 12
    # 分类策略在内存中的复用时长
    _POLICY_TTL_MINUTES = 30

    def __init__(self) -> None:
        super().__init__()
        self._enabled: bool = False
        self._notify_enabled: bool = True
        self._dry_run: bool = False
        self._only_subscribe: bool = True
        self._target_pix: str = "1080p"
        self._max_size_gb: float = 0.0
        self._min_seeders: int = 0
        self._prefer: str = "seeders"
        self._sites: str = ""
        self._cooldown_days: int = 7
        self._exclude_words: str = "CAM,TS,枪版,TC"
        self._require_words: str = ""
        self._any_words: str = ""
        self._quality: List[str] = []
        self._codec: List[str] = []
        self._min_size_gb: float = 0.0
        self._sites_selected: List[int] = []
        self._categories: List[str] = []
        # 媒体库补齐
        self._library_enabled: bool = False
        self._library_cron: str = ""
        self._library_names: List[str] = []
        self._library_server: str = ""
        self._library_save_path: str = ""
        self._library_downloader: str = ""
        self._library_limit: int = 3
        self._library_cooldown_days: int = 30
        self._library_history: Dict[str, str] = {}
        self._library_missed: Dict[str, str] = {}
        self._inflight_hours: float = self._DEFAULT_INFLIGHT_HOURS
        self._scan_running: bool = False
        self._scan_limit_override: Optional[int] = None
        self._scan_ignore_cooldown: bool = False
        self._refresh_media: bool = True
        self._library_hashes: Dict[str, str] = {}
        self._refresh_scheduled_at: float = 0.0
        self._last_filter_stats: Dict[str, Any] = {}
        self._relaxed_used: bool = False
        self._strict_reason: str = ""
        self._scan_lock = threading.Lock()
        self._http_client: Optional[Any] = None
        self._category_names: Dict[str, str] = {}
        self._config: Dict[str, Any] = {}
        self._history: Dict[str, str] = {}
        self._hashes: Dict[str, str] = {}
        self._policy_cache: Optional[Any] = None
        self._policy_cache_at: Optional[datetime] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[dict] = None) -> None:
        if isinstance(config, dict) and isinstance(config.get("config"), dict):
            config = config["config"]
        self._config = config or {}

        self._enabled = bool(self._config.get("enabled", False))
        self._notify_enabled = bool(self._config.get("notify", True))
        self._dry_run = bool(self._config.get("dry_run", False))
        self._only_subscribe = bool(self._config.get("only_subscribe", True))
        self._target_pix = str(self._config.get("target_pix") or "1080p").strip()
        self._prefer = str(self._config.get("prefer") or "seeders").strip()
        self._sites_selected = self._as_site_ids(self._config.get("sites"))
        self._sites = ",".join(str(x) for x in self._sites_selected)
        self._exclude_words = str(self._config.get("exclude_words") or "").strip()
        self._require_words = str(self._config.get("require_words") or "").strip()
        self._any_words = str(self._config.get("any_words") or "").strip()
        self._quality = self._as_list(self._config.get("quality"))
        self._codec = self._as_list(self._config.get("video_codec"))
        self._categories = self._as_list(self._config.get("categories"))

        # 媒体库补齐
        self._library_enabled = bool(self._config.get("library_enabled", False))
        self._library_cron = str(self._config.get("library_cron") or "").strip()
        self._library_names = self._as_list(self._config.get("library_names"))
        self._library_server = str(self._config.get("library_server") or "").strip()
        self._library_save_path = str(self._config.get("library_save_path") or "").strip()
        self._library_downloader = str(self._config.get("library_downloader") or "").strip()
        try:
            self._library_limit = max(0, int(self._config.get("library_limit") or 3))
        except (TypeError, ValueError):
            self._library_limit = 3
        try:
            self._library_cooldown_days = int(self._config.get("library_cooldown_days", 30) or 0)
        except (TypeError, ValueError):
            self._library_cooldown_days = 0
        self._refresh_media = bool(self._config.get("refresh_media", True))

        try:
            self._max_size_gb = float(self._config.get("max_size_gb") or 0)
        except (TypeError, ValueError):
            self._max_size_gb = 0.0
        try:
            self._min_size_gb = float(self._config.get("min_size_gb") or 0)
        except (TypeError, ValueError):
            self._min_size_gb = 0.0
        try:
            self._min_seeders = int(self._config.get("min_seeders") or 0)
        except (TypeError, ValueError):
            self._min_seeders = 0
        try:
            self._cooldown_days = int(self._config.get("cooldown_days") or 0)
        except (TypeError, ValueError):
            self._cooldown_days = 0

        history = self.get_data("history")
        self._history = history if isinstance(history, dict) else {}
        hashes = self.get_data("hashes")
        self._hashes = hashes if isinstance(hashes, dict) else {}
        lib_history = self.get_data("library_history")
        self._library_history = lib_history if isinstance(lib_history, dict) else {}
        miss_history = self.get_data("library_missed")
        self._library_missed = miss_history if isinstance(miss_history, dict) else {}
        try:
            self._inflight_hours = float(self._config.get("inflight_hours", self._DEFAULT_INFLIGHT_HOURS) or 0)
        except (TypeError, ValueError):
            self._inflight_hours = self._DEFAULT_INFLIGHT_HOURS
        lib_hashes = self.get_data("library_hashes")
        self._library_hashes = lib_hashes if isinstance(lib_hashes, dict) else {}
        self._prune_history()

        self._policy_cache = None
        self._policy_cache_at = None
        self._category_names = {item["value"]: item["name"] for item in (self._category_options() or [])}

        logger.info(
            f"双版本补全：初始化完成，enabled={self._enabled}，target={self._target_pix}，"
            f"dry_run={self._dry_run}，only_subscribe={self._only_subscribe}，"
            f"类别={self._category_label(self._categories)}(仅电影)，"
            f"max_size_gb={self._max_size_gb}，min_seeders={self._min_seeders}，"
            f"prefer={self._prefer}，cooldown_days={self._cooldown_days}，"
            f"筛选(质量={self._quality or '不限'}，编码={self._codec or '不限'}，"
            f"必含={self._require_words or '-'}，任选={self._any_words or '-'})，"
            f"站点={len(self._sites_selected) or '全部'}，"
            f"库补齐={self._library_enabled}"
            f"({self._library_label() or '未选库'}，每次{self._library_limit}部，"
            f"补下后刷新库={self._refresh_media})"
        )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        """无需后台服务，保留空实现。"""
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """媒体库补齐的定时扫描（默认每天执行，可从配置改 cron）。"""
        if not (self._library_enabled and self._library_names and self._library_cron):
            return []
        try:
            from apscheduler.triggers.cron import CronTrigger

            trigger = CronTrigger.from_crontab(self._library_cron.strip())
        except Exception as err:  # noqa: BLE001
            logger.error(f"双版本补全：cron 表达式无效（{self._library_cron}）：{err}")
            return []
        return [
            {
                "id": "DualVersionLibraryScan",
                "name": "媒体库4K补齐",
                "trigger": trigger,
                "func": self._scheduled_library_scan,
                "kwargs": {},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/categories",
                "endpoint": self.api_categories,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取媒体库类别列表",
                "description": "读取 MoviePilot 分类策略中启用的电影类别（如动画电影/华语电影/外语电影）",
            },
            {
                "path": "/probe",
                "endpoint": self.api_probe,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "试算一部电影的类别",
                "description": "传 tmdb_id，返回该片命中的媒体类别及是否在当前生效范围内（不下载）",
            },
            {
                "path": "/sites",
                "endpoint": self.api_sites,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取站点下拉选项",
                "description": "返回 MoviePilot 已启用站点（id + 名称），供配置页多选使用",
            },
            {
                "path": "/libraries",
                "endpoint": self.api_libraries,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取媒体库下拉选项",
                "description": "返回 Emby/Jellyfin 中的电影类媒体库名称，供媒体库补齐配置使用",
            },
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "立即执行媒体库补齐扫描",
                "description": "扫描所选媒体库中只有 4K 没有 1080p 的电影并补下 1080p（后台执行）",
            },
            {
                "path": "/scan_status",
                "endpoint": self.api_scan_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询扫描状态",
                "description": "返回是否运行中、上次扫描结果",
            },
            {
                "path": "/scan_progress",
                "endpoint": self.api_scan_progress,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "查询当前扫描进度",
                "description": "返回是否运行中、本轮第几部/共几部、当前正在搜索的影片与已补下数量",
            },
            {
                "path": "/reset_history",
                "endpoint": self.api_reset_history,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "清空补齐重试记录",
                "description": "清空媒体库补齐的冷却记录，让已跳过的影片重新参与扫描",
            },
        ]

    def api_categories(self, refresh: bool = False) -> Dict[str, Any]:
        """给前端/脚本用的类别清单接口。"""
        return {"movie": self._category_options(force=refresh)}

    def api_probe(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """试算类别：用于验证分类链路，不产生任何下载。"""
        payload = payload or {}
        tmdb_id = str(payload.get("tmdb_id") or payload.get("tmdbid") or "").strip()
        if not tmdb_id:
            return {"ok": False, "message": "缺少 tmdb_id"}
        try:
            mediainfo = recognize_movie(tmdb_id)
        except Exception as err:  # noqa: BLE001
            logger.error(f"双版本补全：试算识别失败：{err}")
            return {"ok": False, "message": f"识别失败：{err}"}
        if not mediainfo:
            return {"ok": False, "message": "TMDB 未识别到该媒体"}
        category_id, category_name = self._resolve_category(mediainfo)
        matched, detail = self._category_pass(mediainfo)
        return {
            "ok": bool(category_id),
            "title": getattr(mediainfo, "title", None),
            "year": getattr(mediainfo, "year", None),
            "category_id": category_id,
            "category_name": category_name,
            "selected": list(self._categories),
            "matched": matched,
            "detail": detail,
        }

    # ------------------------------------------------------------------
    # 站点 / 媒体库选项
    # ------------------------------------------------------------------

    def api_sites(self) -> Dict[str, Any]:
        """站点下拉选项（id -> 名称）。"""
        items = self._site_options()
        return {"items": items, "count": len(items), "selected": self._sites_selected}

    def api_libraries(self) -> Dict[str, Any]:
        """媒体库下拉选项（只列电影库）。"""
        items = self._library_options()
        return {"items": items, "count": len(items), "selected": self._library_names}

    def api_scan(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """立即触发一次媒体库补齐扫描（后台线程）。limit=0 表示本轮不限数量。"""
        if not self._library_names:
            return {"started": False, "running": self._scan_is_running(), "message": "未选择要扫描的媒体库"}
        if not self._library_enabled:
            return {
                "started": False,
                "running": self._scan_is_running(),
                "message": "配置里还没打开「启用媒体库补齐」开关，先打开并保存再点扫描",
            }
        if self._scan_is_running():
            prog = self.get_data("library_progress") or {}
            cur = prog.get("current") or ""
            idx, total = prog.get("index") or 0, prog.get("total") or 0
            if prog.get("stage") == "done":
                where = "正在收尾"
            elif cur and total:
                where = f"{cur}（第 {idx}/{total} 部）"
            else:
                where = "正在读取媒体库"
            return {
                "started": False,
                "running": True,
                "message": f"扫描已在运行中：{where}，完成或失败后会写入扫描记录",
            }
        started = self._start_scan(limit)
        libs = "、".join(self._library_names)
        return {
            "started": started,
            "running": True,
            "message": (
                f"已开始扫描 {libs}（{self._scan_limit_desc()}）。"
                "站点有流控，单部可能要等 1~3 分钟；点「查看扫描进度」可看当前进度，完成后会通知。"
                if started
                else "启动失败，请稍后再试"
            ),
        }

    def api_scan_status(self) -> Dict[str, Any]:
        """扫描状态与上次结果。"""
        return {
            "running": self._scan_is_running(),
            "progress": self.get_data("library_progress") or {},
            "enabled": self._library_enabled,
            "libraries": self._library_names,
            "limit": self._library_limit,
            "dry_run": self._dry_run,
            "history": len(self._library_history or {}),
            "last": self.get_data("library_last") or {},
        }

    def api_scan_progress(self) -> Dict[str, Any]:
        """当前扫描进度（按钮「查看扫描进度」用，message 直接给人看）。"""
        prog = self.get_data("library_progress") or {}
        last = self.get_data("library_last") or {}
        running = self._scan_is_running()
        if running:
            msg = (
                f"扫描中：{prog.get('current') or '正在读取媒体库'}"
                f"（第 {prog.get('index') or 0}/{prog.get('total') or 0} 部）"
                f"｜已补下 {prog.get('downloaded') or 0} 部"
                f"｜未命中 {prog.get('missed') or 0} 部"
                f"｜失败 {prog.get('failed') or 0} 部"
                f"｜更新于 {str(prog.get('updated_at') or '').replace('T', ' ')}"
            )
        elif last:
            msg = self._last_scan_text(with_details=False) + "｜点「手动补全一轮」可再跑一轮"
        else:
            msg = "还没有扫描记录，点「手动补全一轮（全部待补）」开始"
        return {"running": running, "progress": prog, "last": last, "message": msg}

    def _library_status_text(self) -> str:
        """配置页顶部那行状态：运行中显示实时进度，空闲显示上次结果。"""
        if self._scan_is_running():
            prog = self.get_data("library_progress") or {}
            cur = prog.get("current") or "正在读取媒体库"
            idx, total = prog.get("index") or 0, prog.get("total") or 0
            tail = f"（第 {idx}/{total} 部）" if cur and total else ""
            return (
                f"正在扫描：{cur}{tail}"
                f"｜已补下 {prog.get('downloaded') or 0} 部"
                f"｜失败 {prog.get('failed') or 0}"
            )
        # 配置页只留一行摘要，逐部明细在「查看数据」页看
        return self._last_scan_text(with_details=False) + "｜明细见「查看数据」"

    def _last_scan_text(self, with_details: bool = True) -> str:
        """上次扫描结果的一句话摘要（页面/接口共用）。"""
        last = self.get_data("library_last") or {}
        if not last:
            return "上次扫描：暂无记录，点「手动补全一轮」开始（首次可能较慢）"
        started = str(last.get("started_at") or "").replace("T", " ")
        err = last.get("error")
        base = (
            f"上次扫描 {started}：库内 {last.get('scanned') or 0} 部，"
            f"只有 4K 无 1080p {last.get('pending') or 0} 部，"
            f"补下 {last.get('downloaded') or 0}，未命中 {last.get('missed') or 0}，"
            f"跳过 {last.get('skipped') or 0}，失败 {last.get('failed') or 0}"
        )
        if last.get("in_flight"):
            base += f"｜另有 {last.get('in_flight')} 部刚补下、正在下载或等待入库"
        if err:
            base += f"（出错：{err}）"
        if with_details:
            details = [str(d) for d in (last.get("details") or [])[:3]]
            if details:
                base += "｜" + "；".join(details)
        return base

    def _scan_is_running(self) -> bool:
        """扫描是否在跑：本实例线程标记 + 数据层心跳（防止配置重建后重复启动）。"""
        if self._scan_running:
            return True
        prog = self.get_data("library_progress") or {}
        if prog.get("stage") == "done":
            # 该轮已收尾，即使残留心跳也不再视为运行中
            return False
        hb = str((self.get_data("scan_lock") or {}).get("heartbeat") or "")
        if not hb:
            return False
        try:
            age = (datetime.now() - datetime.fromisoformat(hb)).total_seconds()
        except Exception:  # noqa: BLE001
            return False
        return 0 <= age < 1800

    def _touch_scan_lock(self) -> None:
        """写扫描心跳（每部影片更新一次，超过 30 分钟无心跳视为已失效）。"""
        try:
            self.save_data("scan_lock", {"heartbeat": datetime.now().isoformat(timespec="seconds")})
        except Exception as err:  # noqa: BLE001
            logger.debug(f"双版本补全：写扫描心跳失败：{err}")

    def _update_scan_progress(self, **fields: Any) -> None:
        """更新本轮扫描进度，供「查看扫描进度」与仪表板读取。"""
        prog = dict(self.get_data("library_progress") or {})
        prog.update({k: v for k, v in fields.items() if v is not None})
        prog["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            self.save_data("library_progress", prog)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"双版本补全：写扫描进度失败：{err}")
        if fields.get("stage") != "done":
            self._touch_scan_lock()

    def api_reset_history(self) -> Dict[str, Any]:
        """
        清空媒体库补齐的重试冷却记录
        """
        old = len(self._library_history)
        self._library_history = {}
        self.save_data("library_history", {})
        return {"cleared": old, "message": f"已清空 {old} 条补齐记录"}

    def _site_options(self) -> List[dict]:
        try:
            sites = SiteOper().list_active()
        except Exception as err:  # noqa: BLE001
            logger.warn(f"双版本补全：读取站点列表失败：{err}")
            return []
        items = []
        for site in sites or []:
            sid = getattr(site, "id", None)
            name = getattr(site, "name", None) or str(sid or "")
            if sid is None:
                continue
            items.append({"title": str(name), "value": int(sid)})
        return items

    def _library_options(self) -> List[dict]:
        items: List[dict] = []
        for library in self._emby_libraries():
            collection_type = str(library.get("CollectionType") or "").lower()
            if collection_type and collection_type != "movies":
                continue
            name = str(library.get("Name") or "").strip()
            if name:
                items.append({"title": name, "value": name})
        return items

    def _server_options(self) -> List[dict]:
        """配置页可选的媒体服务器（Emby/Jellyfin）。"""
        try:
            servers = self.systemconfig.get(SystemConfigKey.MediaServers) or []
        except Exception:  # noqa: BLE001
            return []
        if isinstance(servers, dict):
            servers = list(servers.values())
        items = []
        for server in servers:
            if not isinstance(server, dict) or not server.get("enabled"):
                continue
            if str(server.get("type") or "").lower() not in ("emby", "jellyfin"):
                continue
            name = str(server.get("name") or "").strip()
            if name:
                items.append({"title": name, "value": name})
        return items

    def _library_label(self) -> str:
        return "、".join(self._library_names) if self._library_names else ""

    # ------------------------------------------------------------------
    # 媒体服务器（Emby / Jellyfin）只读客户端
    # ------------------------------------------------------------------

    def _http(self) -> Any:
        if self._http_client is None:
            self._http_client = RequestUtils(content_type="application/json")
        return self._http_client

    def _media_server_conf(self) -> Tuple[Optional[str], Optional[str], str]:
        """返回 (host, api_key, 路径前缀)；优先配置中指定的服务器名。"""
        try:
            servers = self.systemconfig.get(SystemConfigKey.MediaServers) or []
        except Exception as err:  # noqa: BLE001
            logger.warn(f"双版本补全：读取媒体服务器配置失败：{err}")
            return None, None, ""
        if isinstance(servers, dict):
            servers = list(servers.values())
        for server in servers:
            if not isinstance(server, dict) or not server.get("enabled"):
                continue
            server_type = str(server.get("type") or "").lower()
            if server_type not in ("emby", "jellyfin"):
                continue
            if self._library_server and str(server.get("name") or "") != self._library_server:
                continue
            conf = server.get("config") or {}
            host = str(conf.get("host") or "").strip().rstrip("/")
            api_key = str(conf.get("apikey") or "").strip()
            if not (host and api_key):
                continue
            prefix = "emby" if server_type == "emby" else ""
            return host, api_key, prefix
        return None, None, ""

    def _media_get(self, path: str, **params: Any) -> Optional[Any]:
        host, api_key, prefix = self._media_server_conf()
        if not host:
            return None
        url = f"{host}/{prefix}/{path}" if prefix else f"{host}/{path}"
        params["api_key"] = api_key
        try:
            response = self._http().get_res(url=url, params=params, timeout=60)
        except Exception as err:  # noqa: BLE001
            logger.warn(f"双版本补全：请求媒体服务器失败（{path}）：{err}")
            return None
        if response is None:
            return None
        if int(getattr(response, "status_code", 0) or 0) >= 300:
            logger.warn(f"双版本补全：媒体服务器返回 HTTP {getattr(response, 'status_code', '?')}（{path}）")
            return None
        try:
            return response.json()
        except Exception:  # noqa: BLE001
            return None

    def _emby_libraries(self) -> List[dict]:
        data = self._media_get("Library/VirtualFolders")
        return [item for item in (data or []) if isinstance(item, dict)]

    def _emby_movies(self, parent_id: str) -> Any:
        """按分页遍历媒体库中的电影（生成器）。"""
        start, page_size = 0, 500
        while True:
            data = self._media_get(
                "Items",
                Recursive="true",
                IncludeItemTypes="Movie",
                ParentId=parent_id,
                Fields=self._EMBY_FIELDS,
                StartIndex=start,
                Limit=page_size,
                EnableTotalRecordCount="false",
                ImageTypeLimit=0,
            )
            items = [item for item in ((data or {}).get("Items") or []) if isinstance(item, dict)]
            for item in items:
                yield item
            if len(items) < page_size:
                return
            start += page_size

    # ------------------------------------------------------------------
    # 媒体库补齐：扫描 + 补下
    # ------------------------------------------------------------------

    def _scan_limit_desc(self) -> str:
        """本轮处理上限的人话说明。"""
        limit = self._library_limit if self._scan_limit_override is None else int(self._scan_limit_override)
        return "全部待补" if limit <= 0 else f"最多 {limit} 部"

    def _start_scan(self, limit: Optional[int] = None) -> bool:
        with self._scan_lock:
            if self._scan_is_running():
                return False
            self._scan_running = True
            self._scan_limit_override = limit
            # 手动「全部待补」视为补历史欠账：不受冷却期限制
            self._scan_ignore_cooldown = limit is not None and int(limit) <= 0
            self._touch_scan_lock()
        if self._notify_enabled:
            try:
                self._notify(
                    f"媒体库补齐开始：{self._library_label() or '未选库'}"
                    f"（{self._scan_limit_desc()}）。受站点流控影响单部约 1~3 分钟，完成后会再通知。"
                )
            except Exception as err:  # noqa: BLE001
                logger.debug(f"双版本补全：发送开始通知失败：{err}")
        threading.Thread(
            target=self._library_scan_worker, name="DualVersionLibraryScan", daemon=True
        ).start()
        return True

    def _scheduled_library_scan(self) -> None:
        """定时任务入口。"""
        if not (self._library_enabled and self._library_names):
            return
        if not self._start_scan():
            logger.info("双版本补全：上一轮媒体库补齐仍在运行，跳过本轮")

    def _collect_library_movies(self) -> Dict[str, Dict[str, Any]]:
        """按 TMDB 归并媒体库条目，标记是否已有 4K / 1080p。"""
        library_map = {
            str(item.get("Name") or ""): str(item.get("ItemId") or item.get("Id") or "")
            for item in self._emby_libraries()
            if item.get("Name")
        }
        groups: Dict[str, Dict[str, Any]] = {}
        for name in self._library_names:
            parent_id = library_map.get(name)
            if not parent_id:
                logger.warn(f"双版本补全：媒体库「{name}」不存在，跳过")
                continue
            count = 0
            for item in self._emby_movies(parent_id):
                count += 1
                path = str(item.get("Path") or "")
                if not path:
                    continue
                ids = item.get("ProviderIds") or {}
                tmdb_id = str(ids.get("Tmdb") or "").strip()
                key = f"tmdb:{tmdb_id}" if tmdb_id else f"dir:{os.path.dirname(path)}"
                group = groups.get(key)
                if group is None:
                    group = {
                        "key": key,
                        "tmdb": tmdb_id,
                        "title": str(item.get("Name") or tmdb_id or key),
                        "year": int(item.get("ProductionYear") or 0),
                        "has4k": False,
                        "has1080": False,
                        "library": name,
                        "path": path,
                    }
                    groups[key] = group
                if self._RE_4K.search(path):
                    group["has4k"] = True
                if self._RE_1080_ANY.search(path):
                    group["has1080"] = True
            logger.info(f"双版本补全：媒体库「{name}」共 {count} 个电影条目")
        return groups

    def _library_in_cooldown(self, key: str) -> bool:
        if self._library_cooldown_days <= 0:
            return False
        last = (self._library_history or {}).get(key) or (self._library_missed or {}).get(key)
        if not last:
            return False
        try:
            return datetime.now() - datetime.fromisoformat(last) < timedelta(days=self._library_cooldown_days)
        except ValueError:
            return False

    def _library_mark_miss(self, key: str) -> None:
        """未命中（站点没有合格资源）单独记账：不算在途，手动全量可立刻重试。"""
        with self._lock:
            # 注意：不能清掉「在途」记录。补下成功后的下一轮若临时搜不到候选（站点流控/超时），
            # 一旦清掉在途账，这部片会被当成没补过而再次下载，导致同一部影片下两份 1080p。
            self._library_missed[key] = datetime.now().isoformat(timespec="seconds")
            self._prune_library_history()
            self.save_data("library_history", self._library_history)
            self.save_data("library_missed", self._library_missed)

    def _library_in_flight(self, key: str) -> bool:
        """补下后一段时间内、库里还没出现 1080p —— 视为在下载/入库途中（窗口可配，0=不保护）。"""
        if self._inflight_hours <= 0:
            return False
        last = (self._library_history or {}).get(key)
        if not last:
            return False
        # 未命中记录不再覆盖在途状态：补下过的片子在窗口内一律算在途（窗口到点自动过期重试）
        try:
            return datetime.now() - datetime.fromisoformat(last) < timedelta(hours=self._inflight_hours)
        except ValueError:
            return False

    def _library_paths_now(self, tmdb_id: str, title: str = "") -> List[str]:
        """按 TMDB 现查媒体库条目路径（绕开扫描开始时的快照）。

        用于补下前的二次确认：上一轮的 1080p 可能已经入库，但 Emby 还没完成索引，
        扫描快照里看不到，直接下载就会下重复。
        """
        tmdb_id = str(tmdb_id or "").strip()
        if not tmdb_id:
            return []
        paths: List[str] = []
        library_map = {
            str(item.get("Name") or ""): str(item.get("ItemId") or item.get("Id") or "")
            for item in self._emby_libraries()
            if item.get("Name")
        }
        for name in self._library_names:
            parent_id = library_map.get(name)
            if not parent_id:
                continue
            items: List[dict] = []
            try:
                data = self._media_get(
                    "Items",
                    Recursive="true",
                    IncludeItemTypes="Movie",
                    ParentId=parent_id,
                    Fields="Path,ProviderIds",
                    AnyProviderIdEquals=f"tmdb.{tmdb_id}",
                    Limit=50,
                    EnableTotalRecordCount="false",
                    ImageTypeLimit=0,
                )
                items = [i for i in ((data or {}).get("Items") or []) if isinstance(i, dict)]
            except Exception as err:  # noqa: BLE001
                logger.debug(f"双版本补全：按 TMDB 复查媒体库失败（{err}），退回标题搜索")
            if not items and title:
                try:
                    data = self._media_get(
                        "Items",
                        Recursive="true",
                        IncludeItemTypes="Movie",
                        ParentId=parent_id,
                        Fields="Path,ProviderIds",
                        SearchTerm=title,
                        Limit=50,
                        EnableTotalRecordCount="false",
                        ImageTypeLimit=0,
                    )
                    items = [i for i in ((data or {}).get("Items") or []) if isinstance(i, dict)]
                except Exception as err:  # noqa: BLE001
                    logger.debug(f"双版本补全：按标题复查媒体库失败（{err}）")
                    continue
            for item in items:
                ids = item.get("ProviderIds") or {}
                item_tmdb = str(ids.get("Tmdb") or "").strip()
                if item_tmdb and item_tmdb != tmdb_id:
                    continue
                path = str(item.get("Path") or "")
                if path:
                    paths.append(path)
        return paths

    def _library_now_has_target(self, tmdb_id: str, title: str = "") -> str:
        """现查库中是否已有目标分辨率条目，返回命中路径（无则空串）。"""
        for path in self._library_paths_now(tmdb_id, title):
            if self._RE_1080_ANY.search(path):
                return path
        return ""

    def _library_mark(self, key: str) -> None:
        with self._lock:
            self._library_history[key] = datetime.now().isoformat(timespec="seconds")
            self._prune_library_history()
            self.save_data("library_history", self._library_history)

    def _prune_library_history(self) -> None:
        limit = datetime.now() - timedelta(days=365)
        cleaned: Dict[str, str] = {}
        for key, value in (self._library_history or {}).items():
            try:
                if datetime.fromisoformat(str(value)) >= limit:
                    cleaned[str(key)] = str(value)
            except ValueError:
                continue
        if len(cleaned) > 2000:
            newest = sorted(cleaned.items(), key=lambda kv: kv[1], reverse=True)[:2000]
            cleaned = dict(newest)
        self._library_history = cleaned
        cleaned_miss: Dict[str, str] = {}
        for key, value in (self._library_missed or {}).items():
            try:
                if datetime.fromisoformat(str(value)) >= limit:
                    cleaned_miss[str(key)] = str(value)
            except ValueError:
                continue
        if len(cleaned_miss) > 2000:
            newest = sorted(cleaned_miss.items(), key=lambda kv: kv[1], reverse=True)[:2000]
            cleaned_miss = dict(newest)
        self._library_missed = cleaned_miss

    def _remember_library_hash(self, download_hash: str, tmdb_id: str) -> None:
        """记住本插件补下过的任务，转移完成时用它触发媒体库刷新。"""
        digest = str(download_hash or "").strip().lower()
        if not digest:
            return
        with self._lock:
            self._library_hashes[digest] = str(tmdb_id or "")
            if len(self._library_hashes) > 500:
                for stale in list(self._library_hashes)[: len(self._library_hashes) - 500]:
                    self._library_hashes.pop(stale, None)
            self.save_data("library_hashes", self._library_hashes)

    def _is_library_hash(self, download_hash: str) -> bool:
        digest = str(download_hash or "").strip().lower()
        return bool(digest) and digest in (self._library_hashes or {})

    def _schedule_library_refresh(self, delay: int = 120, reason: str = "") -> None:
        """延时刷新媒体库。

        strm 库的链路是「下载完成 → MoviePilot 转移到网盘 → strm 工具生成 .strm → Emby 索引」，
        最后一步不会自己发生，必须调一次媒体库刷新，新版本才会出现在库中。
        """

        now = time.time()
        with self._lock:
            if now - self._refresh_scheduled_at < 600:
                return
            self._refresh_scheduled_at = now

        def _worker() -> None:
            time.sleep(max(10, int(delay)))
            self._refresh_emby_libraries(reason=reason)
            # strm 工具生成 .strm 有延迟，第一次刷新可能还看不到，5 分钟后再补一次
            time.sleep(300)
            self._refresh_emby_libraries(reason=f"{reason}(二次)")

        threading.Thread(target=_worker, name="DualVersionRefresh", daemon=True).start()

    def _media_post(self, path: str, **params: Any) -> bool:
        host, api_key, prefix = self._media_server_conf()
        if not host:
            return False
        url = f"{host}/{prefix}/{path}" if prefix else f"{host}/{path}"
        params["api_key"] = api_key
        try:
            response = self._http().post_res(url=url, params=params, timeout=60)
        except Exception as err:  # noqa: BLE001
            logger.warn(f"双版本补全：POST 媒体服务器失败（{path}）：{err}")
            return False
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 300:
            logger.warn(f"双版本补全：刷新媒体库返回 HTTP {status}（{path}）")
            return False
        return True

    def _refresh_emby_libraries(self, reason: str = "") -> int:
        """刷新配置的媒体库，让新补下的 1080p 立刻被 Emby 索引。"""
        if not self._library_names:
            return 0
        libraries = {
            str(item.get("Name") or ""): str(item.get("ItemId") or item.get("Id") or "")
            for item in self._emby_libraries()
            if item.get("Name")
        }
        done = 0
        for name in self._library_names:
            item_id = libraries.get(name)
            if not item_id:
                logger.warn(f"双版本补全：刷新媒体库时找不到「{name}」")
                continue
            ok = self._media_post(
                f"Items/{item_id}/Refresh",
                Recursive="true",
                ImageRefreshMode="Default",
                MetadataRefreshMode="Default",
                ReplaceAllImages="false",
            )
            if ok:
                done += 1
        if done:
            logger.info(f"双版本补全：已触发刷新 {done} 个媒体库（{reason or '补下完成'}）")
        return done

    def _resolve_library_save_path(self) -> Optional[str]:
        """1080p 的下载目录：优先配置，其次按目录设置里「电影」目录自动推断。"""
        if self._library_save_path:
            return self._library_save_path
        try:
            entries = self.systemconfig.get(SystemConfigKey.Directories) or []
        except Exception as err:  # noqa: BLE001
            logger.warn(f"双版本补全：读取目录设置失败：{err}")
            return None
        if isinstance(entries, dict):
            entries = list(entries.values())
        fallback = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            download_path = str(entry.get("download_path") or "").strip()
            library_path = str(entry.get("library_path") or "").rstrip("/")
            if not download_path or not library_path.endswith("/电影"):
                continue
            if str(entry.get("monitor_type") or "") == "downloader":
                return download_path
            fallback = fallback or download_path
        return fallback

    def _library_scan_worker(self) -> None:
        """扫描媒体库并补下 1080p。"""
        started_at = datetime.now().isoformat(timespec="seconds")
        summary: Dict[str, Any] = {
            "started_at": started_at,
            "libraries": list(self._library_names),
            "scanned": 0,
            "pending": 0,
            "downloaded": 0,
            "skipped": 0,
            "missed": 0,
            "failed": 0,
            "details": [],
            "finished_at": None,
        }
        details: List[str] = summary["details"]
        try:
            limit = self._library_limit if self._scan_limit_override is None else int(self._scan_limit_override)
            logger.info(f"双版本补全：开始扫描媒体库 {self._library_label()}（{self._scan_limit_desc()}）")
            groups = self._collect_library_movies()
            summary["scanned"] = len(groups)
            ignore_cooldown = bool(self._scan_ignore_cooldown)
            skipped_cooldown = 0
            in_flight = 0
            pending = []
            for group in groups.values():
                if not group.get("has4k") or group.get("has1080"):
                    continue
                key = group.get("key") or ""
                if ignore_cooldown:
                    # 手动全量：不认冷却期，但「刚补下、还在下载/入库」的不重复补
                    if self._library_in_flight(key):
                        in_flight += 1
                        continue
                elif self._library_in_cooldown(key):
                    skipped_cooldown += 1
                    continue
                pending.append(group)
            if in_flight:
                logger.info(
                    f"双版本补全：另有 {in_flight} 部刚补下、仍在下载或等待入库，"
                    f"本轮跳过（超过 {self._inflight_hours:g} 小时仍缺 1080p 会自动重试）"
                )
            if skipped_cooldown:
                logger.info(
                    f"双版本补全：另有 {skipped_cooldown} 部仍在冷却期（{self._library_cooldown_days} 天）内，"
                    "点「手动补全一轮（全部待补）」可强制重试"
                )
            summary["in_flight"] = in_flight
            summary["pending"] = len(pending)
            pending.sort(key=lambda g: (-int(g.get("year") or 0), str(g.get("title") or "")))
            plan = pending if limit <= 0 else pending[:limit]
            logger.info(
                f"双版本补全：库内 {len(groups)} 部，只有 4K 无 1080p 的 {len(pending)} 部，"
                f"本轮处理 {len(plan)} 部（{self._scan_limit_desc()}）"
            )
            total = len(plan)
            self._update_scan_progress(
                started_at=started_at,
                stage="searching",
                index=0,
                total=total,
                current="",
                downloaded=0,
                missed=0,
                failed=0,
            )
            for idx, group in enumerate(plan, 1):
                title = str(group.get("title") or group.get("tmdb") or "")
                year = str(group.get("year") or "")
                self._update_scan_progress(
                    stage="searching",
                    index=idx,
                    total=total,
                    current=f"{title}（{year}）" if year else title,
                    downloaded=summary["downloaded"],
                    missed=summary["missed"],
                    failed=summary["failed"],
                )
                status, message = self._backfill_library_movie(group)
                if status == "ok":
                    summary["downloaded"] += 1
                elif status == "miss":
                    summary["missed"] += 1
                elif status == "fail":
                    summary["failed"] += 1
                else:
                    summary["skipped"] += 1
                details.append(f"{message}")
                self._update_scan_progress(
                    stage="searching",
                    index=idx,
                    total=total,
                    current=f"{title}（{year}）" if year else title,
                    downloaded=summary["downloaded"],
                    missed=summary["missed"],
                    failed=summary["failed"],
                )
                time.sleep(3)
        except Exception as err:  # noqa: BLE001
            logger.error(f"双版本补全：媒体库扫描出错：{err}", exc_info=True)
            summary["error"] = str(err)
            details.append(f"扫描出错：{err}")
        finally:
            summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._scan_running = False
            self._update_scan_progress(
                stage="done",
                current="",
                index=summary.get("downloaded", 0),
                total=0,
                downloaded=summary["downloaded"],
                missed=summary["missed"],
                failed=summary["failed"],
            )
            try:
                self.save_data("scan_lock", {})
            except Exception:  # noqa: BLE001
                pass
            try:
                self.save_data("library_last", summary)
            except Exception:  # noqa: BLE001
                pass
            if self._refresh_media and summary["downloaded"]:
                self._schedule_library_refresh(delay=180, reason="扫描结束")
            logger.info(
                f"双版本补全：媒体库扫描结束，扫描 {summary['scanned']} 部，待补 {summary['pending']} 部，"
                f"补下成功 {summary['downloaded']}，未命中 {summary['missed']}，"
                f"跳过 {summary['skipped']}，失败 {summary['failed']}"
            )
            if self._notify_enabled and (
                summary["pending"] or summary["downloaded"] or summary["failed"]
            ):
                head = (
                    f"媒体库补齐完成：本轮补下 {summary['downloaded']} 部"
                    f"（待补 {summary['pending']}，未命中 {summary['missed']}，失败 {summary['failed']}）"
                )
                if not summary["downloaded"] and not summary["failed"]:
                    head += "｜本轮没有可用 1080p 源"
                tail = "\n".join(details[:5])
                self._notify(head + ("\n" + tail if tail else ""), ok=not summary["failed"])

    def _backfill_library_movie(self, group: Dict[str, Any]) -> Tuple[str, str]:
        """为单部影片补下 1080p，返回 (状态, 说明)。"""
        key = str(group.get("key") or "")
        tmdb_id = str(group.get("tmdb") or "").strip()
        name = str(group.get("title") or tmdb_id)
        if not tmdb_id:
            return "skip", f"{name}：无 TMDB 标识，跳过"
        # 补下前先现查一遍媒体库：扫描快照可能早于上一轮入库（Emby 索引滞后），照快照下会重复
        early_hit = self._library_now_has_target(tmdb_id, name)
        if early_hit:
            self._library_mark(key)
            logger.info(
                f"双版本补全[库补齐]：{name} 库中已有 {self._target_pix}"
                f"（{os.path.basename(early_hit)}），跳过"
            )
            return "skip", f"{name}：库中已有 {self._target_pix}（{os.path.basename(early_hit)}），跳过"
        try:
            mediainfo = recognize_movie(tmdb_id)
        except Exception as err:  # noqa: BLE001
            return "fail", f"{name}：识别失败（{err}）"
        if not mediainfo:
            return "skip", f"{name}：TMDB 未识别到，跳过"

        head = f"{getattr(mediainfo, 'title', name) or name}（{getattr(mediainfo, 'year', '') or ''}）"
        if self._categories:
            passed, category_name = self._category_pass(mediainfo)
            if not passed:
                return "skip", f"{head}：类别 {category_name} 不在生效范围"

        try:
            candidates = self._search_candidates(mediainfo)
        except Exception as err:  # noqa: BLE001
            return "fail", f"{head}：搜索出错（{err}）"
        if not candidates:
            self._library_mark_miss(key)
            stats = self._last_filter_stats or {}
            if stats.get("pix_ok"):
                why = (
                    f"搜索到 {stats.get('total', 0)} 个候选，其中 {stats['pix_ok']} 个是 "
                    f"{self._target_pix}，但都被筛选规则滤掉（{stats.get('reason') or '规则不符'}）"
                )
                example = "；".join(stats.get("examples") or [])
                if example:
                    why += f"，例如：{example}"
                logger.info(f"双版本补全[库补齐]：{head} {why}")
                return "miss", f"{head}：{why}"
            self._library_mark_miss(key)
            return "miss", f"{head}：站点没有 {self._target_pix} 资源（共 {stats.get('total', 0)} 个候选）"

        best = candidates[0]
        torrent = getattr(best, "torrent_info", None)
        best_title = getattr(torrent, "title", "") or ""
        site_name = getattr(torrent, "site_name", "") or ""
        size_gb = round((getattr(torrent, "size", 0) or 0) / 1024 ** 3, 2)
        save_path = self._resolve_library_save_path()

        if self._dry_run:
            logger.info(
                f"双版本补全[预演·库补齐]：{head} 将补下 {best_title}"
                f"（{site_name}，{size_gb}GB，目录 {save_path or '默认'}）"
            )
            return "dry", f"{head}：预演命中 {best_title}（{size_gb}GB）"

        # 下载前再复核一次：从搜到候选到真正提交可能已过了几分钟
        late_hit = self._library_now_has_target(tmdb_id, name)
        if late_hit:
            self._library_mark(key)
            logger.info(
                f"双版本补全[库补齐]：{head} 提交前复核发现库中已有 {self._target_pix}"
                f"（{os.path.basename(late_hit)}），跳过"
            )
            return "skip", f"{head}：库中已有 {self._target_pix}（{os.path.basename(late_hit)}），跳过"

        try:
            download_hash = DownloadChain().download_single(
                context=best,
                save_path=save_path,
                downloader=self._library_downloader or None,
                source="Plugin|双版本补全",
            )
        except Exception as err:  # noqa: BLE001
            return "fail", f"{head}：下载提交出错（{err}）"
        if not download_hash:
            return "fail", f"{head}：下载器未接受任务（{best_title}）"
        # MP 对已在下载器里的重复种子会直接返回旧 hash 且不算失败，这里识别出来当在途处理
        if self._is_library_hash(str(download_hash).lower()):
            self._library_mark(key)
            logger.info(
                f"双版本补全[库补齐]：{head} 下载器中已有同一任务（{best_title}），视为在途，跳过"
            )
            return "skip", f"{head}：下载器中已有同一任务（{best_title}），跳过"
        self._library_mark(key)
        self._remember_library_hash(download_hash, tmdb_id)
        relaxed_note = f"，严格规则未命中（{self._strict_reason}）已放宽" if self._relaxed_used else ""
        logger.info(
            f"双版本补全[库补齐]：{head} 已补下 {best_title}"
            f"（{site_name}，{size_gb}GB，目录 {save_path or '默认'}，hash={download_hash}{relaxed_note}）"
        )
        return "ok", f"{head}：已补下 {best_title}（{size_gb}GB{relaxed_note}）"

    @staticmethod
    def _as_site_ids(raw: Any) -> List[int]:
        """站点配置兼容：列表/逗号串 -> int 列表。"""
        values = []
        if raw is None:
            return values
        if isinstance(raw, str):
            values = [x for x in re.split(r"[,\s]+", raw) if x.strip()]
        elif isinstance(raw, (list, tuple, set)):
            for item in raw:
                if isinstance(item, dict):
                    item = item.get("value") or item.get("id")
                if item is not None:
                    values.append(str(item).strip())
        result = []
        for value in values:
            try:
                number = int(str(value).strip())
            except (TypeError, ValueError):
                continue
            if number not in result:
                result.append(number)
        return result

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    # ------------------------------------------------------------------
    # 配置与页面
    # ------------------------------------------------------------------

    @staticmethod
    def _click_script(path: str, limit: Optional[int] = None) -> str:
        """拼一段 JS：带登录态直接调插件接口，并把返回结果弹窗显示。

        MP v3 的配置页由 FormRender 渲染，它**不支持 events**（点击不发任何请求），
        只支持 props 里以源码字符串形式给出的 onClick，所以配置页按钮走这条路。
        """
        query = f"?limit={int(limit)}" if limit is not None else ""
        return (
            "function(){"
            "var tok='';"
            "try{tok=(JSON.parse(localStorage.getItem('auth')||'{}')||{}).token||''}catch(e){}"
            "if(!tok){var ks=Object.keys(localStorage);for(var i=0;i<ks.length;i++){"
            "try{var v=JSON.parse(localStorage.getItem(ks[i])||'null');if(v&&v.token){tok=v.token;break}}catch(e2){}}}"
            "fetch('/api/v1" + path + query + "',{headers:{'Authorization':'Bearer '+tok}})"
            ".then(function(r){return r.text().then(function(t){var m='';try{var d=JSON.parse(t);"
            "m=(d&&d.message)||(d&&d.data&&d.data.message)||''}catch(e3){m=t}"
            "if(!m){m=r.ok?'请求已发送（HTTP '+r.status+'）':'HTTP '+r.status}alert(m)})})"
            ".catch(function(e4){alert('请求失败：'+e4)})"
            "}"
        )

    # ------------------------------------------------------------------
    # 插件详情页（仪表板）：状态 + 可用的操作按钮
    # ------------------------------------------------------------------

    def _page_status(self) -> Tuple[str, str, str]:
        """返回 (color, title, text)，供详情页顶部状态卡使用。"""
        running = self._scan_is_running()
        last = self.get_data("library_last") or {}
        if running:
            return "info", "媒体库补齐：正在运行", self._library_status_text()
        if last:
            return "success", "媒体库补齐：空闲（上次已结束）", self._last_scan_text()
        return "grey", "媒体库补齐：尚未运行过", "点下方「手动补全一轮（全部待补）」把历史遗留的 4K-only 影片一次性补齐；之后交给定时扫描补漏网之鱼。"

    def _page_section_one_text(self) -> str:
        """详情页第一块（订阅下载监控）的状态摘要。"""
        cats = self._category_label(self._categories) or "全部电影"
        words = []
        if self._require_words:
            words.append(f"必须含 {self._require_words}")
        if self._any_words:
            words.append(f"任选 {self._any_words}")
        return (
            f"状态：{'已启用' if self._enabled else '已关闭'}"
            f"｜生效类别：{cats}"
            f"｜补下分辨率：{self._target_pix}"
            f"｜冷却：{self._cooldown_days} 天"
            f"｜{'预演（不下载）' if self._dry_run else '实际下载'}"
            + (f"｜筛选：{'；'.join(words)}" if words else "")
        )

    def get_page(self) -> Optional[List[Dict[str, Any]]]:
        """插件详情页：实时状态 + 真正可点的按钮。

        MP v3 中配置页由 FormRender 渲染，它**不处理 events**（点了不发请求、也没有任何日志）；
        详情页由 PageRender 渲染，支持 events.click.api，并且动作完成后会自动重新加载本页数据，
        所以「手动补全 / 查看进度」这类操作按钮放在这里才真正可用。
        """
        color, title, text = self._page_status()
        last = self.get_data("library_last") or {}
        details = [str(x) for x in (last.get("details") or [])][:8]
        limit_desc = f"最多 {self._library_limit} 部" if self._library_limit > 0 else "不限数量"

        # 操作按钮用原生 HTML（v-html 渲染）而不是 VBtn：
        # PageRender 的 events 点击后宿主不会重新拉取页面数据、props.onClick 字符串它又不认，
        # 用原生 onclick 直接 fetch，并把服务端返回的进度写回 #dv-live，点了必有可见反馈。
        def js_btn(label: str, path: str, bg: str, fg: str, limit: Optional[int] = None) -> str:
            url = f"/api/v1{path}" + (f"?limit={int(limit)}" if limit is not None else "")
            body = (
                "var el=document.getElementById('dv-live');"
                "var tok='';"
                "try{tok=(JSON.parse(localStorage.getItem('auth')||'{}')||{}).token||''}catch(e){}"
                "if(el){el.textContent='请求中…'}"
                "fetch('" + url + "',{headers:{'Authorization':'Bearer '+tok}})"
                ".then(function(r){return r.text().then(function(t){var m=t;"
                "try{var d=JSON.parse(t);m=(d&&d.message)||(d&&d.data&&d.data.message)||''}catch(e2){}"
                "if(!m){m=r.ok?'请求已发送（HTTP '+r.status+'）':'HTTP '+r.status}"
                "if(el){el.textContent=m}else{alert(m)}})}).catch(function(e3){"
                "if(el){el.textContent='请求失败：'+e3}else{alert('请求失败：'+e3)}})"
            )
            style = (
                f"background:{bg};color:{fg};border:0;border-radius:6px;padding:8px 14px;"
                "font-size:14px;cursor:pointer;margin:0 8px 8px 0;line-height:1.2"
            )
            return f'<button type="button" style="{style}" onclick="{body}">{label}</button>'

        buttons_html = (
            '<div style="display:flex;flex-wrap:wrap;margin-top:12px">'
            + js_btn("手动补全一轮（全部待补）", "/plugin/DualVersionDownloader/scan", "#fb8c00", "#fff", 0)
            + js_btn(f"按上限补一轮（{limit_desc}）", "/plugin/DualVersionDownloader/scan", "#1976d2", "#fff")
            + js_btn("查看扫描进度（刷新状态）", "/plugin/DualVersionDownloader/scan_progress", "#0288d1", "#fff")
            + js_btn("清空冷却记录", "/plugin/DualVersionDownloader/reset_history", "#546e7a", "#fff")
            + "</div>"
            + '<div id="dv-live" style="margin-top:8px;font-size:14px;line-height:1.5;color:#eceff1">点上面的按钮，结果会显示在这里（扫描中会显示第几部/共几部）。</div>'
        )

        def event_btn(label: str, path: str, color: str, icon: str,
                      limit: Optional[int] = None) -> Dict[str, Any]:
            """v2 用 events 点击：前端会带登录态请求，并在完成后自动刷新本页数据。"""
            api = f"/plugin/DualVersionDownloader{path}"
            if limit is not None:
                api += f"?limit={int(limit)}"
            return {
                "component": "VBtn",
                "props": {
                    "color": color,
                    "variant": "flat",
                    "size": "small",
                    "prependIcon": icon,
                    "class": "mr-2 mb-2",
                },
                "text": label,
                "events": {"click": {"api": api, "method": "GET"}},
            }

        if IS_V3:
            action_nodes: List[Dict[str, Any]] = [{"component": "div", "html": buttons_html}]
        else:
            action_nodes = [{
                "component": "VRow",
                "props": {"class": "mt-2"},
                "content": [{
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        event_btn("手动补全一轮（全部待补）", "/scan", "warning", "mdi-playlist-plus", 0),
                        event_btn(f"按上限补一轮（{limit_desc}）", "/scan", "primary", "mdi-radar"),
                        event_btn("查看扫描进度", "/scan_progress", "info", "mdi-progress-clock"),
                        event_btn("清空冷却记录", "/reset_history", "grey", "mdi-broom"),
                    ],
                }],
            }]

        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": "① 订阅下载监控：4K 下载时自动追加 1080p",
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "success" if self._enabled else "warning",
                                    "variant": "tonal",
                                    "density": "compact",
                                },
                                "text": self._page_section_one_text(),
                            },
                            {
                                "component": "div",
                                "props": {"class": "text-caption mt-2"},
                                "text": "被动触发：MoviePilot 下载 4K 电影时自动补下同片 1080p 到同一目录；下载的是 1080p 则不动。此块无需手动操作。",
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-1"},
                        "text": "② 媒体库补齐：把缺 1080p 的老片补上",
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {"type": color, "variant": "tonal", "density": "compact"},
                                "content": [
                                    {"component": "div", "props": {"class": "text-subtitle-2"}, "text": title},
                                    {"component": "div", "props": {"class": "text-body-2"}, "text": text},
                                ],
                            },
                            *action_nodes,
                            {
                                "component": "div",
                                "props": {"class": "text-caption mt-3"},
                                "text": f"扫描的库：{'、'.join(self._library_names) or '未选择'}｜定时扫描：{self._library_cron or '未启用（仅手动）'}｜同一影片冷却 {self._library_cooldown_days} 天｜点按钮会弹「正在处理」并在完成后自动刷新本页状态；扫描开始与结束另发通知，可在插件菜单「查看日志」看逐部日志。",
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-2"},
                        "text": "最近一轮明细",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "text-body-2"},
                        "content": (
                            [{"component": "div", "text": line} for line in details]
                            if details
                            else [{"component": "div", "props": {"class": "text-medium-emphasis"}, "text": "暂无记录"}]
                        ),
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"class": "text-subtitle-2"},
                        "text": f"最近自动补全记录（{len(self._history or {})} 部）",
                    },
                    {
                        "component": "VCardText",
                        "props": {"class": "text-body-2"},
                        "content": (
                            [
                                {"component": "div", "text": line}
                                for line in [
                                    f"{str(ts)[:19].replace('T', ' ')}  {key}"
                                    for key, ts in sorted(
                                        (self._history or {}).items(), key=lambda kv: kv[1], reverse=True
                                    )[:10]
                                ]
                            ]
                            if self._history
                            else [{"component": "div", "props": {"class": "text-medium-emphasis"}, "text": "暂无记录"}]
                        ),
                    },
                ],
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        category_items = self._category_select_items()
        site_items = self._site_options()
        library_items = self._library_options()
        server_items = self._server_options()
        last_text = self._library_status_text()
        _req = self._words(self._require_words)
        if len(_req) > 1:
            last_text += f"｜必须全部命中：{'、'.join(_req)}"
        return [
            {
                "component": "VForm",
                "content": [

                    {
                        "component": "VRow",
                        "content": [
                            self._col_switch("enabled", "启用插件"),
                            self._col_switch("notify", "发送通知"),
                            self._col_switch("dry_run", "仅预演不下载"),
                            self._col_switch("only_subscribe", "仅订阅来源触发"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col_text("target_pix", "补下的分辨率", "1080p"),
                            self._col_text("max_size_gb", "候选最大体积(GB，0为不限)", "0"),
                            self._col_text("min_seeders", "候选最少做种数(0为不限)", "0"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "categories",
                                            "label": "生效的电影类别（不选=全部电影）",
                                            "items": category_items,
                                            "multiple": True,
                                            "chips": True,
                                            "closableChips": True,
                                            "clearable": True,
                                            "hint": "MP 媒体分类，如 动画电影 / 华语电影 / 外语电影",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "prefer",
                                            "label": "候选排序偏好",
                                            "items": [
                                                {"title": "做种多优先", "value": "seeders"},
                                                {"title": "体积小优先", "value": "size"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col_text("cooldown_days", "同一影片冷却天数(0为不去重)", "7"),
                            self._col_text("min_size_gb", "候选最小体积(GB，0为不限)", "0"),
                            self._col_text("exclude_words", "排除关键词(逗号分隔)", "CAM,TS,枪版,TC"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col_text(
                                "inflight_hours",
                                "补下后多少小时内不重复补(小时，0=每次手动扫描都重试)",
                                "12",
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "sites",
                                            "label": "限定站点（不选=全部站点）",
                                            "items": site_items,
                                            "multiple": True,
                                            "chips": True,
                                            "closableChips": True,
                                            "clearable": True,
                                            "hint": "不选=全部已启用站点",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "quality",
                                            "label": "质量（不选=不限）",
                                            "items": [
                                                {"title": name, "value": name}
                                                for name in ("Remux", "BluRay", "WEB-DL", "HDTV")
                                            ],
                                            "multiple": True,
                                            "chips": True,
                                            "closableChips": True,
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "video_codec",
                                            "label": "视频编码（不选=不限）",
                                            "items": [
                                                {"title": name, "value": name}
                                                for name in ("H265", "H264", "AV1")
                                            ],
                                            "multiple": True,
                                            "chips": True,
                                            "closableChips": True,
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col_text(
                                "require_words", "必须包含关键词(全部命中，如 中字,中语)", "", md=6
                            ),
                            self._col_text(
                                "any_words", "任选关键词(命中任一，如 中字,中英,国语)", "", md=6
                            ),
                        ],
                    },
                    {"component": "VDivider", "props": {"class": "my-4"}},


                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "compact",
                                            "text": last_text,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col_switch("library_enabled", "启用媒体库补齐"),
                            self._col_text("library_limit", "每次最多补几部", "3", md=3),
                            self._col_text("library_cooldown_days", "同一影片重试间隔(天)", "30", md=3),
                            self._col_text("library_cron", "定时扫描(cron，留空=仅手动)", "0 4 * * *", md=3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "library_names",
                                            "label": "扫描的媒体库（多选）",
                                            "items": library_items,
                                            "multiple": True,
                                            "chips": True,
                                            "closableChips": True,
                                            "clearable": True,
                                            "hint": "Emby/Jellyfin 电影库",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "library_server",
                                            "label": "媒体服务器（不选=第一个 Emby）",
                                            "items": server_items,
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                            self._col_text(
                                "library_save_path", "1080p 下载目录(留空=自动取电影下载目录)", "", md=4
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col_text("library_downloader", "指定下载器(留空=默认)", "", md=4),
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "refresh_media",
                                            "label": "补下后刷新媒体库",
                                            "hint": "strm 库要刷新 Emby 才会出现新补的 1080p",
                                            "persistentHint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4, "class": "d-flex align-center"},
                                "content": [
                                    {
                                        "component": "div",
                                        "props": {"class": "text-caption"},
                                        "text": "操作按钮在「查看数据」页",
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "dry_run": False,
            "only_subscribe": True,
            "categories": [],
            "target_pix": "1080p",
            "max_size_gb": 0,
            "min_seeders": 0,
            "prefer": "seeders",
            "cooldown_days": 7,
            "min_size_gb": 0,
            "sites": [],
            "quality": [],
            "video_codec": [],
            "require_words": "",
            "any_words": "",
            "exclude_words": "CAM,TS,枪版,TC",
            "inflight_hours": 24,
            "library_enabled": False,
            "library_cron": "0 4 * * *",
            "library_names": [],
            "library_server": "",
            "library_save_path": "",
            "library_downloader": "",
            "library_limit": 3,
            "library_cooldown_days": 30,
            "refresh_media": True,
        }

    # ------------------------------------------------------------------
    # 表单小工具
    # ------------------------------------------------------------------

    @staticmethod
    def _col_switch(model: str, label: str) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 3},
            "content": [{"component": "VSwitch", "props": {"model": model, "label": label}}],
        }

    @staticmethod
    def _col_text(model: str, label: str, placeholder: str, md: int = 4) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [
                {
                    "component": "VTextField",
                    "props": {"model": model, "label": label, "placeholder": placeholder},
                }
            ],
        }

    def _category_select_items(self) -> List[dict]:
        return [
            {"title": item["name"], "value": item["value"]}
            for item in (self._category_options() or [])
        ]

    def _category_label(self, selected: List[str]) -> str:
        if not selected:
            return "全部电影"
        names = self._category_names if self._category_names else {
            item["value"]: item["name"] for item in (self._category_options() or [])
        }
        return "、".join(names.get(value, value) for value in selected)

    @staticmethod
    def _as_list(raw: Any) -> List[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            return [x.strip() for x in re.split(r"[,\s]+", raw) if x.strip()]
        if isinstance(raw, (list, tuple, set)):
            result = []
            for item in raw:
                if isinstance(item, dict):
                    value = item.get("value") or item.get("id")
                else:
                    value = item
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    result.append(text)
            return result
        return []

    # ------------------------------------------------------------------
    # 媒体类别：读取分类策略 / 选项 / 判定
    # ------------------------------------------------------------------

    @staticmethod
    def _unwrap_policy_payload(raw: Any) -> Any:
        """兼容三种存法：分类策略本体 / ClassificationPolicyState（active） / 字符串。"""
        if isinstance(raw, str) and raw.strip():
            try:
                raw = json.loads(raw)
            except ValueError:
                return None
        if isinstance(raw, dict):
            for key in ("active", "policy"):
                inner = raw.get(key)
                if isinstance(inner, dict) and inner.get("categories") is not None:
                    return inner
            if raw.get("categories") is not None:
                return raw
        return raw if not isinstance(raw, dict) else None

    def _load_policy(self) -> Optional[Any]:
        """读取 MoviePilot 当前的媒体分类策略（带短时缓存）。"""
        if ClassificationPolicy is None:
            return None
        if self._policy_cache is not None and self._policy_cache_at:
            if datetime.now() - self._policy_cache_at < timedelta(minutes=self._POLICY_TTL_MINUTES):
                return self._policy_cache
        try:
            policy_key = getattr(SystemConfigKey, "MediaClassificationPolicy", None)
            raw = self.systemconfig.get(policy_key) if policy_key else None
        except Exception as err:  # noqa: BLE001
            logger.debug(f"双版本补全：读取分类策略失败：{err}")
            return self._policy_cache
        payload = self._unwrap_policy_payload(raw)
        policy = None
        try:
            if isinstance(payload, ClassificationPolicy):
                policy = payload
            elif isinstance(payload, dict):
                policy = ClassificationPolicy(**payload)
        except Exception as err:  # noqa: BLE001
            logger.debug(f"双版本补全：解析分类策略失败：{err}")
            return self._policy_cache
        if policy is not None:
            self._policy_cache = policy
            self._policy_cache_at = datetime.now()
        return policy

    def _policy_categories(self) -> Optional[List[dict]]:
        """策略中启用的电影类别（顺序即配置顺序）。"""
        policy = self._load_policy()
        if policy is None:
            return None
        items: List[dict] = []
        seen = set()
        for category in getattr(policy, "categories", None) or []:
            media_type = str(getattr(category, "media_type", "") or "")
            if media_type and media_type != "电影":
                continue
            if not bool(getattr(category, "enabled", True)):
                continue
            cid = str(getattr(category, "id", "") or "").strip()
            name = str(getattr(category, "name", "") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            items.append({"value": cid, "name": name or cid})
        return items

    def _category_options(self, force: bool = False) -> List[dict]:
        """类别选项：优先读分类策略，失败时回退到上次缓存。"""
        cache = self.get_data("category_cache") or {}
        if not isinstance(cache, dict):
            cache = {}
        fresh = False
        ts = cache.get("ts")
        if ts:
            try:
                fresh = datetime.now() - datetime.fromisoformat(str(ts)) < timedelta(
                    hours=self._CATEGORY_CACHE_HOURS
                )
            except ValueError:
                fresh = False
        if force or not fresh or not cache.get("items"):
            probed = self._policy_categories()
            if probed:
                cache = {"items": probed, "ts": datetime.now().isoformat(timespec="seconds")}
                try:
                    self.save_data("category_cache", cache)
                except Exception as err:  # noqa: BLE001
                    logger.debug(f"双版本补全：保存类别缓存失败：{err}")
        return list(cache.get("items") or [])

    def _resolve_category(self, mediainfo: Any) -> Tuple[Optional[str], str]:
        """按 MoviePilot 分类策略算出该影片所属类别，返回 (类别ID, 类别名)。"""
        policy = self._load_policy()
        if policy is not None:
            try:
                from app.application.classification.analysis import (
                    build_classification_facts_from_media,
                )
                from app.application.classification.execution import (
                    evaluate_classification_facts,
                )

                facts = build_classification_facts_from_media(mediainfo)
                evaluation = evaluate_classification_facts(policy, facts)
                result = getattr(evaluation, "result", None) or evaluation
                selection = getattr(result, "effective", None) or getattr(result, "recommended", None)
                if selection is not None:
                    cid = getattr(selection, "category_id", None)
                    path = list(getattr(selection, "category_path", None) or [])
                    name = self._category_name_of(policy, cid) or (" / ".join(path) if path else "")
                    return (str(cid) if cid else None), name
            except Exception as err:  # noqa: BLE001
                logger.debug(f"双版本补全：分类求值失败，改用媒体自带类别：{err}")

        # 回退：媒体对象自带的分类结果 / 兼容字段
        classification = getattr(mediainfo, "classification", None)
        selection = getattr(classification, "effective", None) or getattr(
            classification, "recommended", None
        )
        if selection is not None:
            cid = getattr(selection, "category_id", None)
            path = list(getattr(selection, "category_path", None) or [])
            if cid:
                return str(cid), (" / ".join(path) if path else str(cid))
        for attr in ("library_category", "category"):
            value = getattr(mediainfo, attr, None)
            if value:
                text = " / ".join(value) if isinstance(value, (list, tuple)) else str(value)
                return None, text.strip()
        return None, ""

    @staticmethod
    def _category_name_of(policy: Any, category_id: Optional[str]) -> str:
        if not category_id:
            return ""
        for category in getattr(policy, "categories", None) or []:
            if str(getattr(category, "id", "")) == str(category_id):
                return str(getattr(category, "name", "") or "")
        return ""

    def _category_pass(self, mediainfo: Any) -> Tuple[bool, str]:
        """类别命中判定：未选类别时全部通过。"""
        if not self._categories:
            return True, ""
        cid, name = self._resolve_category(mediainfo)
        selected_ids = {str(value) for value in self._categories}
        selected_names = {
            self._category_names.get(str(value), str(value)) for value in self._categories
        }
        if cid and str(cid) in selected_ids:
            return True, name
        # 类别 ID 对不上（策略调整过/同名多类）时按名称兜底
        for token in re.split(r"\s*/\s*", name or ""):
            if token and token in selected_names:
                return True, name
        if cid:
            logger.debug(f"双版本补全：类别未命中，媒体类别 id={cid} 名称={name or '-'}")
        return False, name or "未判定"

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event) -> None:
        """本插件补下的 1080p 转移完成 → 等 strm 生成后刷新媒体库。"""
        if not (self._enabled and self._refresh_media):
            return
        try:
            data = event.event_data or {}
            if data.get("success") is False:
                return
            download_hash = str(data.get("download_hash") or "").strip()
            if not self._is_library_hash(download_hash):
                return
            logger.info(f"双版本补全：补下的 1080p 已转移完成（hash={download_hash}），稍后刷新媒体库")
            self._schedule_library_refresh(delay=120, reason="转移完成")
        except Exception as err:  # noqa: BLE001
            logger.debug(f"双版本补全：处理转移完成事件失败：{err}")

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event) -> None:
        """下载任务添加后判定：电影、仅 4K、类别命中、且非本插件自己补下的任务。"""
        if not self._enabled:
            return
        try:
            data = event.event_data or {}
            context = data.get("context")
            if not context:
                logger.debug("双版本补全：事件缺少 context，跳过")
                return

            mediainfo = getattr(context, "media_info", None)
            meta = getattr(context, "meta_info", None)
            torrent = getattr(context, "torrent_info", None)
            if not mediainfo or not meta:
                logger.debug(
                    f"双版本补全：事件缺少 mediainfo/meta_info（mediainfo={bool(mediainfo)}，"
                    f"meta={bool(meta)}），跳过"
                )
                return

            media_type = self._media_type_of(mediainfo)
            if media_type != MediaType.MOVIE.value:
                logger.debug(f"双版本补全：仅处理电影，跳过媒体类型 {media_type}")
                return

            source = str(data.get("source") or "")
            if self._only_subscribe and not source.startswith("Subscribe|"):
                logger.debug(f"双版本补全：跳过非订阅来源的下载（source={source or '未知'}）")
                return

            text = self._pix_text(torrent, meta)
            if not self._RE_4K.search(text):
                logger.debug(f"双版本补全：非 4K，跳过：{text[:80]}")
                return
            if self._RE_1080.search(text):
                # 标题里同时出现 1080 与 4K（如合集/多版本包）时不动，避免误判
                logger.debug(f"双版本补全：标题同时命中 1080p，跳过：{text[:120]}")
                return

            passed, category_name = self._category_pass(mediainfo)
            if not passed:
                logger.info(
                    f"双版本补全：{getattr(mediainfo, 'title', '')} 类别不符"
                    f"（{category_name}，仅处理 {self._category_label(self._categories)}），跳过"
                )
                return

            media_key = self._media_key(mediainfo)
            if not media_key:
                logger.debug(f"双版本补全：无法生成媒体键（{getattr(mediainfo, 'title', '')}），跳过")
                return
            download_hash = str(data.get("hash") or "").lower()
            if self._in_cooldown(media_key):
                logger.debug(f"双版本补全：{media_key} 在冷却期内，跳过")
                return

            with self._lock:
                # 同一个种子只处理一次，避免事件重复投递；不同种子仍按冷却期判定
                if download_hash and download_hash in self._hashes:
                    logger.debug(f"双版本补全：种子 {download_hash[:12]} 已处理过，跳过")
                    return
                now = datetime.now().isoformat(timespec="seconds")
                if download_hash:
                    self._hashes[download_hash] = now
                self._history[media_key] = now
                self._save_state()

            threading.Thread(
                target=self._complete,
                args=(media_key, context, data, category_name),
                name="DualVersionComplete",
                daemon=True,
            ).start()
        except Exception as err:  # noqa: BLE001
            logger.error(f"双版本补全：处理下载事件出错：{err}", exc_info=True)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _complete(self, media_key: str, context: Any, data: Dict[str, Any], category_name: str = "") -> None:
        """搜索并补下目标分辨率版本。"""
        mediainfo = context.media_info
        downloader = data.get("downloader")
        download_hash = data.get("hash")
        username = data.get("username")
        title = f"{getattr(mediainfo, 'title', '') or ''} ({getattr(mediainfo, 'year', '') or ''})"
        tag = f"［{category_name}］" if category_name else ""
        try:
            save_path = self._resolve_save_path(download_hash, downloader)
            candidates = self._search_candidates(mediainfo)
            if not candidates:
                logger.info(f"双版本补全：{title} 未找到可用的 {self._target_pix} 候选")
                self._notify(f"{title} 未找到可用的 {self._target_pix} 版本", ok=False)
                return

            best = candidates[0]
            best_title = getattr(getattr(best, "torrent_info", None), "title", "") or ""
            best_site = getattr(getattr(best, "torrent_info", None), "site_name", "") or ""
            size_gb = round((getattr(best.torrent_info, "size", 0) or 0) / 1024 ** 3, 2)

            if self._dry_run:
                logger.info(
                    f"双版本补全[预演]：{title}{tag} 将补下 {self._target_pix}：{best_title}"
                    f"（{best_site}，{size_gb}GB，保存目录 {save_path or '默认'}）"
                )
                self._notify(f"{title} 预演命中：{best_title}（{size_gb}GB），未实际下载", ok=True)
                return

            result = DownloadChain().download_single(
                context=best,
                save_path=save_path,
                downloader=downloader,
                username=username,
                source="Plugin|双版本补全",
            )
            if result:
                logger.info(
                    f"双版本补全：{title}{tag} 已补下 {self._target_pix}：{best_title}"
                    f"（{best_site}，{size_gb}GB，hash={result}）"
                )
                self._notify(f"{title} 已补下 {self._target_pix}：{best_title}（{size_gb}GB）", ok=True)
            else:
                logger.warn(f"双版本补全：{title} 补下失败（下载器未接受任务）：{best_title}")
                self._notify(f"{title} 补下失败：{best_title}", ok=False)
        except Exception as err:  # noqa: BLE001
            logger.error(f"双版本补全：{title} 补下过程出错：{err}", exc_info=True)
            self._notify(f"{title} 补下出错：{err}", ok=False)
        finally:
            with self._lock:
                self._history[media_key] = datetime.now().isoformat(timespec="seconds")
                self._save_state()

    def _search_candidates(self, mediainfo: Any) -> List[Any]:
        """按媒体身份搜索，过滤出目标分辨率的候选并排序。"""
        sites = None
        if self._sites:
            try:
                sites = [int(x) for x in re.split(r"[,\s]+", self._sites) if x.strip()]
            except ValueError:
                sites = None

        contexts = SearchChain().process(mediainfo=mediainfo, sites=sites) or []
        logger.info(f"双版本补全：搜索到 {len(contexts)} 个候选资源")
        picked = self._filter_candidates(contexts)
        self._relaxed_used = False
        if not picked and (self._last_filter_stats or {}).get("pix_ok"):
            # 严格规则一条都不剩时自动放宽关键词，按「确实有 1080p 就补下」处理
            self._strict_reason = (self._last_filter_stats or {}).get("reason") or "关键词不符"
            picked = self._filter_candidates(contexts, strict_words=False)
            if picked:
                self._relaxed_used = True
                logger.info(
                    f"双版本补全：严格匹配 0 条（{(self._last_filter_stats or {}).get('reason')}），"
                    f"已放宽关键词规则，命中 {len(picked)} 条"
                )
        return picked

    def _filter_candidates(self, contexts: List[Any], strict_words: bool = True) -> List[Any]:
        """从搜索结果里挑出目标分辨率的候选并排序（纯函数，便于自测）。

        strict_words=False 表示宽松兜底：忽略「必须包含 / 任选关键词」，其余规则照旧。
        关键词在「标题 + 副标题 + MP 解析字段」里匹配，和 MP 订阅规则的口径一致。
        """
        excludes = self._words(self._exclude_words)
        requires = self._words(self._require_words) if strict_words else []
        anys = self._words(self._any_words) if strict_words else []
        picked: List[Any] = []
        stats = {"total": len(contexts), "pix_ok": 0, "rejected": 0, "reason": "", "examples": []}
        for item in contexts:
            torrent = getattr(item, "torrent_info", None)
            meta = getattr(item, "meta_info", None)
            if not torrent:
                continue
            text = self._pix_text(torrent, meta)
            if self._RE_4K.search(text) or not self._RE_1080.search(text):
                continue
            stats["pix_ok"] += 1
            kw_text = self._keyword_text(torrent, meta).lower()
            title = getattr(torrent, "title", "") or ""
            reason = ""
            if excludes and any(self.hit_word(word, kw_text) for word in excludes):
                reason = "命中排除词"
            # 必须包含（全部命中，等价订阅规则的「包含」多关键词）
            elif requires and not all(self.hit_word(word, kw_text) for word in requires):
                reason = "未同时包含：" + "、".join(requires)
            # 任选其一（命中任一，等价订阅规则的多选项「包含」）
            elif anys and not any(self.hit_word(word, kw_text) for word in anys):
                reason = "未命中任一：" + "、".join(anys)
            elif self._quality and self._quality_of(text) not in self._quality:
                reason = "质量不符"
            elif self._codec and self._codec_of(text) not in self._codec:
                reason = "编码不符"
            elif self._max_size_gb > 0 and (getattr(torrent, "size", 0) or 0) > self._max_size_gb * 1024 ** 3:
                reason = "超过最大体积"
            elif self._min_size_gb > 0 and (getattr(torrent, "size", 0) or 0) < self._min_size_gb * 1024 ** 3:
                reason = "低于最小体积"
            elif self._min_seeders > 0 and (getattr(torrent, "seeders", 0) or 0) < self._min_seeders:
                reason = "做种数不足"
            if reason:
                stats["rejected"] += 1
                if not stats["reason"]:
                    stats["reason"] = reason
                if len(stats["examples"]) < 3 and title:
                    stats["examples"].append(title)
                continue
            picked.append(item)
        self._last_filter_stats = stats

        if self._prefer == "size":
            picked.sort(key=lambda c: (getattr(c.torrent_info, "size", 0) or 0,
                                       -(getattr(c.torrent_info, "seeders", 0) or 0)))
        else:
            picked.sort(key=lambda c: (-(getattr(c.torrent_info, "seeders", 0) or 0),
                                       getattr(c.torrent_info, "size", 0) or 0))
        return picked

    @staticmethod
    def _words(raw: Any) -> List[str]:
        """逗号/空格分隔的关键词表（小写）。"""
        return [word.strip().lower() for word in re.split(r"[,\s]+", str(raw or "")) if word.strip()]

    @staticmethod
    def hit_word(word: Any, text: str) -> bool:
        """关键词命中判定。

        含中文的关键词按子串匹配（「中字」要能命中「简中字幕」）；
        纯拉丁关键词必须落在词边界上，否则「TS」会误伤 DTS 音轨、
        「TC」会误伤 HTC —— 实测会把绝大多数 1080p 蓝光原盘名全滤掉。
        """
        key = str(word or "").strip().lower()
        if not key:
            return False
        body = str(text or "").lower()
        if re.search(r"[\u4e00-\u9fff]", key):
            return key in body
        pattern = r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])"
        return re.search(pattern, body) is not None

    @classmethod
    def _quality_of(cls, text: str) -> str:
        """从标题里识别来源质量：Remux / BluRay / WEB-DL / HDTV。"""
        for name, pattern in cls._QUALITY_RULES:
            if pattern.search(text):
                return name
        return ""

    @classmethod
    def _codec_of(cls, text: str) -> str:
        """从标题里识别视频编码：H265 / H264 / AV1。"""
        for name, pattern in cls._CODEC_RULES:
            if pattern.search(text):
                return name
        return ""

    @staticmethod
    def _pix_text(torrent: Any, meta: Any) -> str:
        """分辨率/质量/编码判定用文本（只看标题与解析结果，避免副标题里的年份/规格干扰）。"""
        return " ".join(
            str(x) for x in (
                getattr(torrent, "title", "") or "",
                getattr(meta, "resource_pix", "") or "",
                getattr(meta, "org_string", "") or "",
            ) if x
        )

    @classmethod
    def _keyword_text(cls, torrent: Any, meta: Any) -> str:
        """关键词匹配用文本：标题 + 副标题 + MP 解析出的编码/音频/制作组/版本等。

        与 MP 自带订阅规则的匹配口径一致——「国语」「中字」这类信息大多写在副标题或
        解析字段里，标题里往往没有，只看标题会把可用资源全部滤掉。
        """
        return " ".join(
            str(x) for x in (
                cls._pix_text(torrent, meta),
                getattr(torrent, "description", "") or "",
                getattr(meta, "video_encode", "") or "",
                getattr(meta, "audio_encode", "") or "",
                getattr(meta, "resource_team", "") or "",
                getattr(meta, "edition", "") or "",
                getattr(meta, "web_source", "") or "",
            ) if x
        )

    @staticmethod
    def _media_type_of(mediainfo: Any) -> Optional[str]:
        media_type = getattr(mediainfo, "type", None)
        return getattr(media_type, "value", media_type)

    def _resolve_save_path(self, download_hash: Optional[str], downloader: Optional[str]) -> Optional[str]:
        """读取原 4K 任务的保存目录，保证两版落在同一文件夹。"""
        if not download_hash:
            return None
        try:
            torrents = DownloadChain().list_torrents(hashs=download_hash, downloader=downloader) or []
        except Exception as err:  # noqa: BLE001
            logger.warn(f"双版本补全：读取原任务保存路径失败：{err}")
            return None
        for item in torrents:
            path = getattr(item, "save_path", None) or getattr(item, "path", None)
            if path:
                return str(path)
        logger.info("双版本补全：未能读取原任务保存路径，将使用默认下载目录")
        return None

    @staticmethod
    def _media_key(mediainfo: Any) -> Optional[str]:
        source = getattr(mediainfo, "media_source", None)
        media_id = getattr(mediainfo, "media_id", None)
        if source and media_id:
            return f"{getattr(source, 'value', source)}:{media_id}"
        tmdb_id = getattr(mediainfo, "tmdb_id", None)
        if not tmdb_id:
            return None
        return f"tmdb:{tmdb_id}"

    def _in_cooldown(self, media_key: str) -> bool:
        if self._cooldown_days <= 0:
            return False
        last = (self._history or {}).get(media_key)
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return False
        return datetime.now() - last_dt < timedelta(days=self._cooldown_days)

    def _prune_history(self) -> None:
        """冷却表与种子去重表都只保留最近 90 天、各自最多 500 条。"""
        limit = datetime.now() - timedelta(days=90)

        def _clean(raw: Any) -> Dict[str, str]:
            if not isinstance(raw, dict):
                return {}
            cleaned: Dict[str, str] = {}
            for key, value in raw.items():
                try:
                    if datetime.fromisoformat(str(value)) >= limit:
                        cleaned[str(key)] = str(value)
                except ValueError:
                    continue
            if len(cleaned) > 500:
                newest = sorted(cleaned.items(), key=lambda kv: kv[1], reverse=True)[:500]
                cleaned = dict(newest)
            return cleaned

        self._history = _clean(self._history)
        self._hashes = _clean(getattr(self, "_hashes", {}))

    def _save_state(self) -> None:
        """把冷却表与种子去重表落盘（调用方需自行持有锁）。"""
        self._prune_history()
        self.save_data("history", self._history)
        self.save_data("hashes", self._hashes)

    def _notify(self, text: str, ok: bool = True) -> None:
        if not self._notify_enabled:
            return
        try:
            self.post_message(
                mtype=MessageType.Plugin,
                title="双版本补全",
                text=("✅ " if ok else "⚠️ ") + text,
            )
        except Exception as err:  # noqa: BLE001
            logger.warn(f"双版本补全：发送通知失败：{err}")
