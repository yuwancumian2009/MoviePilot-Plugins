import json
import re
import threading
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.schemas.types import EventType, MediaType, MessageType

# TMDB 类型 id -> 中文名（与 MP 分类词表一致，用于展示与匹配）
GENRE_NAMES: Dict[str, str] = {
    "12": "冒险",
    "14": "奇幻",
    "16": "动画",
    "18": "剧情",
    "27": "恐怖",
    "28": "动作",
    "35": "喜剧",
    "36": "历史",
    "37": "西部",
    "53": "惊悚",
    "80": "犯罪",
    "99": "纪录",
    "878": "科幻",
    "9648": "悬疑",
    "10402": "音乐",
    "10749": "爱情",
    "10751": "家庭",
    "10752": "战争",
    "10759": "动作冒险",
    "10762": "儿童",
    "10763": "新闻",
    "10764": "真人秀",
    "10765": "科幻奇幻",
    "10766": "肥皂剧",
    "10767": "脱口秀",
    "10768": "战争政治",
    "10770": "电视电影",
}

MOVIE_GENRE_IDS: List[str] = [
    "28", "12", "16", "35", "80", "99", "18", "10751", "14",
    "36", "27", "10402", "9648", "10749", "878", "53", "10752", "37", "10770",
]
TV_GENRE_IDS: List[str] = [
    "10759", "16", "35", "80", "99", "18", "10751", "10762", "9648",
    "10764", "10765", "10766", "10767", "10768", "10763",
]

GENRE_NAME_TO_ID: Dict[str, str] = {name: gid for gid, name in GENRE_NAMES.items()}


class DualVersionDownloader(_PluginBase):
    """
    双版本补全。

    监听下载添加事件：当电影 / 剧集以 4K 版本开始下载时，按同一媒体身份再搜索一个
    1080p 版本并下载到与原任务相同的目录；原任务为 1080p 时不做任何处理。
    支持按媒体库中的类型（动作/剧情/动画…）限定生效范围。
    """

    # 插件名称
    plugin_name = "双版本补全"
    # 插件描述
    plugin_desc = "电影/剧集订阅下载 4K 版本时，自动补一个 1080p 版本到同一目录；可按类型限定生效范围"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/movie.png"
    # 插件版本
    plugin_version = "1.2.3"
    version = "1.2.3"
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

    # 4K / 1080p 判定
    _RE_4K = re.compile(r"(?i)(\b4k\b|2160[pi]|x2160|\buhd\b)")
    _RE_1080 = re.compile(r"(?i)(1080p|x1080)")

    # 类型缓存有效期
    _GENRE_CACHE_HOURS = 12

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
        self._enable_movie: bool = True
        self._enable_tv: bool = True
        self._movie_genres: List[str] = []
        self._tv_genres: List[str] = []
        self._config: Dict[str, Any] = {}
        self._history: Dict[str, str] = {}
        self._hashes: Dict[str, str] = {}
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
        self._sites = str(self._config.get("sites") or "").strip()
        self._exclude_words = str(self._config.get("exclude_words") or "").strip()
        self._enable_movie = bool(self._config.get("enable_movie", True))
        self._enable_tv = bool(self._config.get("enable_tv", True))
        self._movie_genres = self._as_list(self._config.get("movie_genres"))
        self._tv_genres = self._as_list(self._config.get("tv_genres"))

        try:
            self._max_size_gb = float(self._config.get("max_size_gb") or 0)
        except (TypeError, ValueError):
            self._max_size_gb = 0.0
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
        self._prune_history()

        logger.info(
            f"双版本补全：初始化完成，enabled={self._enabled}，target={self._target_pix}，"
            f"dry_run={self._dry_run}，only_subscribe={self._only_subscribe}，"
            f"电影={self._enable_movie}{self._movie_genres or '(全部类型)'}，"
            f"剧集={self._enable_tv}{self._tv_genres or '(全部类型)'}，"
            f"max_size_gb={self._max_size_gb}，min_seeders={self._min_seeders}，"
            f"prefer={self._prefer}，cooldown_days={self._cooldown_days}"
        )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        """无需后台服务，保留空实现。"""
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/genres",
                "endpoint": self.api_genres,
                "methods": ["GET"],
                "summary": "获取媒体库中的类型列表",
                "description": "读取媒体服务器（Emby/Jellyfin）中电影与剧集实际存在的类型，供配置选择",
            }
        ]

    def api_genres(self, refresh: bool = False) -> Dict[str, Any]:
        """给前端/脚本用的类型清单接口。"""
        return {
            "movie": self._genre_options("movie", force=refresh),
            "tv": self._genre_options("tv", force=refresh),
        }

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    # ------------------------------------------------------------------
    # 配置与页面
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        movie_items = self._genre_select_items("movie")
        tv_items = self._genre_select_items("tv")
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
                            self._col_switch("enable_movie", "处理电影"),
                            self._col_switch("enable_tv", "处理剧集"),
                            self._col_text("target_pix", "补下的分辨率", "1080p"),
                            self._col_text("max_size_gb", "候选最大体积(GB，0为不限)", "0"),
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
                                            "model": "movie_genres",
                                            "label": "生效的电影类型（不选=全部类型）",
                                            "items": movie_items,
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "tv_genres",
                                            "label": "生效的剧集类型（不选=全部类型）",
                                            "items": tv_items,
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
                            self._col_text("min_seeders", "候选最少做种数(0为不限)", "0"),
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
                            self._col_text("cooldown_days", "同一媒体冷却天数(0为不去重)", "7"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col_text("sites", "限定站点ID(逗号分隔，留空为全部)", ""),
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_words",
                                            "label": "排除关键词(逗号分隔)",
                                            "placeholder": "CAM,TS,枪版,TC",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
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
                                            "text": (
                                                "电影与剧集各自独立：下载 4K 时才会触发，补下的资源保存到与 4K 任务"
                                                "相同的目录，整理时按命名规则自动区分版本；下载 1080p 不做任何处理。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "dry_run": False,
            "only_subscribe": True,
            "enable_movie": True,
            "enable_tv": True,
            "movie_genres": [],
            "tv_genres": [],
            "target_pix": "1080p",
            "max_size_gb": 0,
            "min_seeders": 0,
            "prefer": "seeders",
            "cooldown_days": 7,
            "sites": "",
            "exclude_words": "CAM,TS,枪版,TC",
        }

    def get_page(self) -> List[dict]:
        records = sorted(
            (self._history or {}).items(), key=lambda kv: kv[1], reverse=True
        )[:20]
        lines = [f"{ts[:19].replace('T', ' ')}  {key}" for key, ts in records]
        movie_genres = self._genre_label(self._movie_genres) if self._enable_movie else "未启用"
        tv_genres = self._genre_label(self._tv_genres) if self._enable_tv else "未启用"
        return [
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
                                    "text": (
                                        f"状态：{'已启用' if self._enabled else '未启用'}　"
                                        f"补下分辨率：{self._target_pix}　"
                                        f"预演模式：{'开' if self._dry_run else '关'}　"
                                        f"已处理：{len(self._history or {})} 部"
                                    ),
                                },
                            },
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "secondary",
                                    "variant": "tonal",
                                    "class": "mt-2",
                                    "text": f"电影类型：{movie_genres}　剧集类型：{tv_genres}",
                                },
                            },
                        ],
                    }
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {"component": "VCardTitle", "text": "最近补全记录"},
                                    {
                                        "component": "VCardText",
                                        "text": "\n".join(lines) if lines else "暂无记录",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        ]

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

    def _genre_select_items(self, mtype: str) -> List[dict]:
        items = []
        for item in self._genre_options(mtype):
            label = item.get("name") or item.get("value")
            if item.get("in_library"):
                label = f"{label}（库中已有）"
            items.append({"title": label, "value": item.get("value")})
        return items

    def _genre_label(self, selected: List[str]) -> str:
        if not selected:
            return "全部类型"
        labels = []
        for value in selected:
            if value.startswith("name:"):
                labels.append(value[5:])
            else:
                labels.append(GENRE_NAMES.get(value, value))
        return "、".join(labels)

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
    # 类型：探测媒体库 / 选项 / 匹配
    # ------------------------------------------------------------------

    def _genre_options(self, mtype: str, force: bool = False) -> List[dict]:
        """返回类型选项：优先用媒体库中实际存在的类型，取不到时退回全量词表。"""
        cache = self.get_data("genre_cache") or {}
        if not isinstance(cache, dict):
            cache = {}
        bucket = cache.get(mtype) or {}
        fresh = False
        ts = cache.get("ts")
        if ts:
            try:
                fresh = datetime.now() - datetime.fromisoformat(str(ts)) < timedelta(hours=self._GENRE_CACHE_HOURS)
            except ValueError:
                fresh = False
        if force or not fresh or not bucket.get("items"):
            probed = self._probe_library_genres(mtype)
            if probed is not None:
                bucket = {"items": probed}
                cache[mtype] = bucket
                cache["ts"] = datetime.now().isoformat(timespec="seconds")
                try:
                    self.save_data("genre_cache", cache)
                except Exception as err:  # noqa: BLE001
                    logger.debug(f"双版本补全：保存类型缓存失败：{err}")

        items = bucket.get("items") or []
        if items:
            # 库中已有的排在前面
            return sorted(items, key=lambda x: (not x.get("in_library"), MOVIE_GENRE_IDS.index(x["value"])
                                                if x.get("value") in MOVIE_GENRE_IDS else 99))
        # 兜底：全量词表
        ids = MOVIE_GENRE_IDS if mtype == "movie" else TV_GENRE_IDS
        return [{"value": gid, "name": GENRE_NAMES.get(gid, gid), "in_library": False} for gid in ids]

    def _probe_library_genres(self, mtype: str) -> Optional[List[dict]]:
        """从已启用的媒体服务器（Emby/Jellyfin）读取媒体库中实际存在的类型。"""
        try:
            from app.sdk.services import MediaServerHelper
        except Exception as err:  # noqa: BLE001
            logger.debug(f"双版本补全：媒体服务器模块不可用：{err}")
            return None

        item_type = "Movie" if mtype == "movie" else "Series"
        found: Dict[str, dict] = {}
        probed_any = False
        try:
            configs = MediaServerHelper().get_configs(include_disabled=False) or {}
        except Exception as err:  # noqa: BLE001
            logger.debug(f"双版本补全：读取媒体服务器配置失败：{err}")
            return None

        for name, conf in configs.items():
            try:
                dump = conf.model_dump() if hasattr(conf, "model_dump") else dict(conf)
            except Exception:  # noqa: BLE001
                continue
            cfg = dump.get("config") or {}
            host = str(dump.get("host") or cfg.get("host") or "").rstrip("/")
            apikey = dump.get("apikey") or cfg.get("apikey") or cfg.get("api_key")
            if not host or not apikey:
                continue
            url = f"{host}/Genres?Recursive=true&IncludeItemTypes={item_type}&api_key={apikey}"
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "ignore"))
            except Exception as err:  # noqa: BLE001
                logger.debug(f"双版本补全：媒体服务器 {name} 类型读取失败：{err}")
                continue
            probed_any = True
            for item in payload.get("Items") or []:
                genre_name = (item.get("Name") or "").strip()
                if not genre_name:
                    continue
                gid = GENRE_NAME_TO_ID.get(genre_name)
                key = gid or f"name:{genre_name}"
                found[key] = {"value": key, "name": genre_name, "in_library": True}
        if not probed_any:
            return None
        return list(found.values())

    def _genres_of(self, mediainfo: Any) -> Tuple[Set[str], Set[str]]:
        """取媒体的类型 id 与名称，缺失时回查 TMDB。"""
        ids: Set[str] = set()
        names: Set[str] = set()
        for genre in getattr(mediainfo, "genres", None) or []:
            gid = getattr(genre, "id", None)
            name = getattr(genre, "name", None)
            if gid is not None:
                ids.add(str(gid))
            if name:
                names.add(str(name))
        for gid in getattr(mediainfo, "genre_ids", None) or []:
            ids.add(str(gid))
        if ids or names:
            return ids, names

        tmdb_id = getattr(mediainfo, "tmdb_id", None)
        if not tmdb_id:
            return ids, names
        media_type = self._media_type_of(mediainfo)
        try:
            from app.modules.themoviedb.tmdbapi import TmdbApi

            info = TmdbApi().get_info(
                MediaType.TV if media_type == MediaType.TV.value else MediaType.MOVIE, int(tmdb_id)
            )
        except Exception as err:  # noqa: BLE001
            logger.debug(f"双版本补全：查询 TMDB 类型失败：{err}")
            return ids, names
        if not info:
            return ids, names
        payload = dict(info) if not isinstance(info, dict) else info
        for gid in payload.get("genre_ids") or []:
            ids.add(str(gid))
        for genre in payload.get("genres") or []:
            if isinstance(genre, dict):
                if genre.get("id") is not None:
                    ids.add(str(genre["id"]))
                if genre.get("name"):
                    names.add(str(genre["name"]))
        return ids, names

    def _genre_pass(self, mediainfo: Any, selected: List[str]) -> bool:
        """选中的类型与媒体类型有交集则通过；未选表示不限。"""
        if not selected:
            return True
        ids, names = self._genres_of(mediainfo)
        for value in selected:
            if value.startswith("name:"):
                if value[5:] in names:
                    return True
            elif value in ids:
                return True
        return False

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event) -> None:
        """下载任务添加后判定：电影/剧集、仅 4K、类型命中、且非本插件自己补下的任务。"""
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
                logger.debug(f"双版本补全：事件缺少 mediainfo/meta_info（mediainfo={bool(mediainfo)}，meta={bool(meta)}），跳过")
                return

            media_type = self._media_type_of(mediainfo)
            if media_type == MediaType.MOVIE.value:
                if not self._enable_movie:
                    logger.debug("双版本补全：未启用电影处理，跳过")
                    return
                selected = self._movie_genres
            elif media_type == MediaType.TV.value:
                if not self._enable_tv:
                    logger.debug("双版本补全：未启用剧集处理，跳过")
                    return
                selected = self._tv_genres
            else:
                logger.debug(f"双版本补全：不支持的媒体类型 {media_type}，跳过")
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
                logger.debug(f"双版本补全：标题同时命中 1080p，跳过：{text}")
                return

            if not self._genre_pass(mediainfo, selected):
                logger.info(
                    f"双版本补全：{getattr(mediainfo, 'title', '')} 类型不符（{self._genre_label(selected)}），跳过"
                )
                return

            targets = self._target_episodes(context)
            season = self._season_of(context)
            media_key = self._media_key(mediainfo, targets, season)
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
                args=(media_key, context, data),
                name="DualVersionComplete",
                daemon=True,
            ).start()
        except Exception as err:  # noqa: BLE001
            logger.error(f"双版本补全：处理下载事件出错：{err}", exc_info=True)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _complete(self, media_key: str, context: Any, data: Dict[str, Any]) -> None:
        """搜索并补下目标分辨率版本。"""
        mediainfo = context.media_info
        downloader = data.get("downloader")
        download_hash = data.get("hash")
        username = data.get("username")
        title = f"{getattr(mediainfo, 'title', '') or ''} ({getattr(mediainfo, 'year', '') or ''})"
        targets = self._target_episodes(context)
        season = self._season_of(context)
        if targets:
            ep_text = ",".join(str(x) for x in sorted(targets))
            episode_hint = f" S{season:02d}E{ep_text}" if season else f" E{ep_text}"
        else:
            episode_hint = f" S{season}" if season else ""
        try:
            save_path = self._resolve_save_path(download_hash, downloader)
            candidates = self._search_candidates(mediainfo, targets)
            if not candidates:
                logger.info(f"双版本补全：{title}{episode_hint} 未找到可用的 {self._target_pix} 候选")
                self._notify(f"{title}{episode_hint} 未找到可用的 {self._target_pix} 版本", ok=False)
                return

            best = candidates[0]
            best_title = getattr(getattr(best, "torrent_info", None), "title", "") or ""
            best_site = getattr(getattr(best, "torrent_info", None), "site_name", "") or ""
            size_gb = round((getattr(best.torrent_info, "size", 0) or 0) / 1024 ** 3, 2)

            if self._dry_run:
                logger.info(
                    f"双版本补全[预演]：{title}{episode_hint} 将补下 {self._target_pix}：{best_title}"
                    f"（{best_site}，{size_gb}GB，保存目录 {save_path or '默认'}）"
                )
                self._notify(f"{title}{episode_hint} 预演命中：{best_title}（{size_gb}GB），未实际下载", ok=True)
                return

            result = DownloadChain().download_single(
                context=best,
                episodes=targets or None,
                save_path=save_path,
                downloader=downloader,
                username=username,
                source="Plugin|双版本补全",
            )
            if result:
                logger.info(
                    f"双版本补全：{title}{episode_hint} 已补下 {self._target_pix}：{best_title}"
                    f"（{best_site}，{size_gb}GB，hash={result}）"
                )
                self._notify(f"{title}{episode_hint} 已补下 {self._target_pix}：{best_title}（{size_gb}GB）", ok=True)
            else:
                logger.warn(f"双版本补全：{title}{episode_hint} 补下失败（下载器未接受任务）：{best_title}")
                self._notify(f"{title}{episode_hint} 补下失败：{best_title}", ok=False)
        except Exception as err:  # noqa: BLE001
            logger.error(f"双版本补全：{title}{episode_hint} 补下过程出错：{err}", exc_info=True)
            self._notify(f"{title}{episode_hint} 补下出错：{err}", ok=False)
        finally:
            with self._lock:
                self._history[media_key] = datetime.now().isoformat(timespec="seconds")
                self._save_state()

    def _search_candidates(self, mediainfo: Any, targets: Optional[Set[int]] = None) -> List[Any]:
        """按媒体身份搜索，过滤出目标分辨率的候选并排序。"""
        sites = None
        if self._sites:
            try:
                sites = [int(x) for x in re.split(r"[,\s]+", self._sites) if x.strip()]
            except ValueError:
                sites = None

        contexts = SearchChain().process(mediainfo=mediainfo, sites=sites) or []
        logger.info(f"双版本补全：搜索到 {len(contexts)} 个候选资源")
        return self._filter_candidates(contexts, targets)

    def _filter_candidates(self, contexts: List[Any], targets: Optional[Set[int]] = None) -> List[Any]:
        """从搜索结果里挑出目标分辨率的候选并排序（纯函数，便于自测）。"""
        excludes = [w.strip().lower() for w in re.split(r"[,\s]+", self._exclude_words) if w.strip()]
        picked: List[Any] = []
        for item in contexts:
            torrent = getattr(item, "torrent_info", None)
            meta = getattr(item, "meta_info", None)
            if not torrent:
                continue
            text = self._pix_text(torrent, meta)
            if self._RE_4K.search(text) or not self._RE_1080.search(text):
                continue
            if any(word in text.lower() for word in excludes):
                continue
            if targets and not self._covers_episodes(item, targets):
                continue
            size = getattr(torrent, "size", 0) or 0
            if self._max_size_gb > 0 and size > self._max_size_gb * 1024 ** 3:
                continue
            if self._min_seeders > 0 and (getattr(torrent, "seeders", 0) or 0) < self._min_seeders:
                continue
            picked.append(item)

        if self._prefer == "size":
            picked.sort(key=lambda c: (getattr(c.torrent_info, "size", 0) or 0,
                                       -(getattr(c.torrent_info, "seeders", 0) or 0)))
        else:
            picked.sort(key=lambda c: (-(getattr(c.torrent_info, "seeders", 0) or 0),
                                       getattr(c.torrent_info, "size", 0) or 0))
        return picked

    @staticmethod
    def _covers_episodes(item: Any, targets: Set[int]) -> bool:
        """候选是否覆盖目标集数：整季包（无集信息）或集列表包含目标集。"""
        meta = getattr(item, "meta_info", None)
        episodes = set(getattr(meta, "episode_list", None) or [])
        if not episodes:
            return True
        return targets.issubset(episodes)

    @staticmethod
    def _pix_text(torrent: Any, meta: Any) -> str:
        return " ".join(
            str(x) for x in (
                getattr(torrent, "title", "") or "",
                getattr(meta, "resource_pix", "") or "",
                getattr(meta, "org_string", "") or "",
            ) if x
        )

    @staticmethod
    def _media_type_of(mediainfo: Any) -> Optional[str]:
        media_type = getattr(mediainfo, "type", None)
        return getattr(media_type, "value", media_type)

    @staticmethod
    def _target_episodes(context: Any) -> Set[int]:
        """剧集：取本次下载要补的集数；电影返回空集。"""
        episodes: Set[int] = set()
        for holder in (getattr(context, "media_info", None), getattr(context, "meta_info", None)):
            for value in getattr(holder, "episode_list", None) or []:
                try:
                    episodes.add(int(value))
                except (TypeError, ValueError):
                    continue
            if episodes:
                break
        if episodes:
            return episodes
        meta = getattr(context, "meta_info", None)
        begin = getattr(meta, "begin_episode", None)
        end = getattr(meta, "end_episode", None)
        if begin:
            try:
                begin = int(begin)
                end = int(end) if end else begin
                return set(range(begin, end + 1))
            except (TypeError, ValueError):
                return set()
        return set()

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
    @staticmethod
    def _season_of(context: Any) -> Optional[int]:
        """剧集：推断本次下载的季号（MediaInfo 无 season_id，回退 MetaInfo.season_list / seasons 键）。"""
        mediainfo = getattr(context, "media_info", None)
        meta = getattr(context, "meta_info", None)
        for value in (getattr(mediainfo, "season_id", None),):
            try:
                if value:
                    return int(value)
            except (TypeError, ValueError):
                pass
        for value in getattr(meta, "season_list", None) or []:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        seasons = getattr(mediainfo, "seasons", None) or {}
        if isinstance(seasons, dict):
            keys: List[int] = []
            for key in seasons.keys():
                try:
                    keys.append(int(key))
                except (TypeError, ValueError):
                    continue
            if len(keys) == 1:
                return keys[0]
        return None

    @staticmethod
    def _media_key(mediainfo: Any, targets: Optional[Set[int]] = None, season: Optional[int] = None) -> Optional[str]:
        source = getattr(mediainfo, "media_source", None)
        media_id = getattr(mediainfo, "media_id", None)
        if source and media_id:
            base = f"{getattr(source, 'value', source)}:{media_id}"
        else:
            tmdb_id = getattr(mediainfo, "tmdb_id", None)
            if not tmdb_id:
                return None
            base = f"tmdb:{tmdb_id}"
        season = season if season else getattr(mediainfo, "season_id", None)
        if season:
            return f"{base}:S{season}"
        return base

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
