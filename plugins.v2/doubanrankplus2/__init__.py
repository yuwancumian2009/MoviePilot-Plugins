import datetime
import re
import xml.dom.minidom
from threading import Event
from typing import Optional, Tuple, List, Dict, Any, Set, TypedDict
import time
import random
import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from enum import Enum

from app.schemas import Response
from app.schemas.types import MediaType, EventType, NotificationType
from app.core.context import MediaInfo
from app.core.event import Event as ManagerEvent, eventmanager
from app.core.meta.metabase import MetaBase
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils
from app.modules.douban.apiv2 import DoubanApi


class Status(Enum):
    UNRECOGNIZED = "未识别"
    UNCATEGORIZED = "已识别未分类"
    YEAR_NOT_MATCH = "年份不符合"
    RATING_NOT_MATCH = "评分不符合"
    RATING_PENDING = "等待评分复查"
    RETRY_LATER = "等待重试"
    MEDIA_EXISTS = "媒体库已存在"
    SUBSCRIPTION_EXISTS = "订阅已存在"
    SUBSCRIPTION_ADDED = "已添加订阅"
    SUBSCRIPTION_CANCELLED = "已手动取消订阅"


class HistoryDataType(Enum):
    STATISTICS = "历史处理统计"
    RECOGNIZED = "已识别历史"
    UNRECOGNIZED = "未识别历史"
    ALL = "所有历史"
    LATEST = "最新12条历史"


class Icons(Enum):
    RECOGNIZED = "icon_recognized"
    STATISTICS = "icon_statistics"
    UNRECOGNIZED = "icon_unrecognized"
    RSS = "icon_rss"


class HistoryPayload(TypedDict):
    title: str
    type: str
    year: str
    poster: Optional[str]
    overview: str
    tmdbid: str
    doubanid: str
    unique: str
    time: str
    time_full: str
    vote: float
    status: str
    category: Optional[str]


class RssInfo(TypedDict):
    title: str
    link: str
    mtype: str
    doubanid: str | None
    year: str | None
    douban_vote: float | None


class DoubanRankPlus2(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣榜单Plus（自用）"
    # 插件描述
    plugin_desc = "豆瓣榜单增强订阅：豆瓣/TMDB双评分过滤、综艺季识别与可重试历史"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.5.1"
    # 插件作者
    plugin_author = "yuwancumian"
    # 作者主页
    author_url = "https://github.com/yuwancumian2009/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "DoubanRankPlus2_"
    # 加载顺序
    plugin_order = 7
    # 可使用的用户级别
    auth_level = 1

    # 退出事件
    _event = Event()

    downloadchain: DownloadChain
    subscribechain: SubscribeChain
    mediachain: MediaChain
    doubanapi: DoubanApi

    # 私有属性
    _plugin_id = "DoubanRankPlus2"
    _msg_install = "如果MP是V1版本需要**重启一次**让API生效，V2版本无需重启"
    _msg_migrate_install = "请确保原MP已**安装并启用**此插件"

    _scheduler = None
    _douban_address = {
        "movie-ustop": "https://rsshub.app/douban/movie/ustop",
        "movie-weekly": "https://rsshub.app/douban/movie/weekly",
        "movie-real-time": "https://rsshub.app/douban/movie/weekly/movie_real_time_hotest",
        "show-domestic": "https://rsshub.app/douban/movie/weekly/show_domestic",
        "movie-hot-gaia": "https://rsshub.app/douban/movie/weekly/movie_hot_gaia",
        "tv-hot": "https://rsshub.app/douban/movie/weekly/tv_hot",
        "movie-top250": "https://rsshub.app/douban/movie/weekly/movie_top250",
        "movie-top250-full": "https://rsshub.app/douban/list/movie_top250",
    }

    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _rss_addrs: List[str] = []
    _ranks: List[str] = []
    _vote: float = 0.0
    _clear: bool = False
    _clearflag: bool = False
    _clear_unrecognized: bool = False
    _clearflag_unrecognized: bool = False
    _proxy: bool = False
    _is_seasons_all: bool = True
    _release_year: int = 0
    _min_sleep_time: int = 3
    _max_sleep_time: int = 10
    _history_type: str = HistoryDataType.LATEST.value
    _is_exit_ip_rate_limit: bool = False
    _is_only_movies: bool = False
    _allow_unrated: bool = True
    _use_series_rating: bool = True
    _min_vote_count: int = 20
    # 豆瓣评分阈值、评分策略（both/any/douban/tmdb）、评价人数下限与低口碑过滤
    _douban_vote: float = 6.0
    _vote_strategy: str = "both"
    _min_douban_count: int = 500
    _min_tmdb_count: int = 50
    _low_reputation_enabled: bool = True
    _low_reputation_max_ratio: float = 0.25
    _low_reputation_margin: float = 0.5
    _notify: bool = True
    _title_aliases_raw: str = ""
    _title_aliases: Dict[str, str] = {}

    # 分类型评分阈值：留空=沿用统一规则，0=该源不设门槛
    _cat_enabled: bool = True
    _cat_raw: Dict[str, str] = {}
    _cat_tv_rules: Dict[str, Dict[str, Optional[float]]] = {}
    _cat_movie_rules: List[Tuple[Set[str], Dict[str, Optional[float]], str]] = []
    _last_category_name: Optional[str] = None
    _last_douban_vote_used: Optional[float] = None

    # 剧集分类判定顺序与显示名：综艺 > 动漫 > 日韩剧 > 国产剧 > 欧美剧
    _CATEGORY_NAMES: Dict[str, str] = {
        "variety": "综艺",
        "anime": "动漫",
        "jk_tv": "日韩剧",
        "cn_tv": "国产剧",
        "west_tv": "欧美剧",
    }
    _VARIETY_GENRES: Set[str] = {"真人秀", "脱口秀", "综艺", "选秀", "音乐真人秀"}
    _ANIME_GENRES: Set[str] = {"动画"}
    _CN_REGIONS: Set[str] = {
        "中国大陆",
        "中国香港",
        "中国台湾",
        "中国澳门",
        "大陆",
        "香港",
        "台湾",
    }
    _JK_REGIONS: Set[str] = {"韩国", "日本"}
    _WEST_REGIONS: Set[str] = {
        "美国",
        "英国",
        "加拿大",
        "澳大利亚",
        "新西兰",
        "爱尔兰",
        "法国",
        "德国",
        "意大利",
        "西班牙",
        "葡萄牙",
        "荷兰",
        "比利时",
        "瑞典",
        "丹麦",
        "挪威",
        "芬兰",
        "冰岛",
        "瑞士",
        "奥地利",
        "波兰",
        "捷克",
        "匈牙利",
        "希腊",
        "俄罗斯",
        "乌克兰",
        "墨西哥",
        "巴西",
        "阿根廷",
        "智利",
        "南非",
    }
    # 豆瓣独有类型 → TMDB 分类名 的归并
    _GENRE_ALIASES: Dict[str, str] = {
        "歌舞": "音乐",
        "武侠": "动作",
        "古装": "历史",
        "传记": "历史",
        "儿童": "家庭",
        "戏曲": "音乐",
        "黑色电影": "犯罪",
        "灾难": "惊悚",
    }
    # 已知分类名（TMDB 电影分类 + 豆瓣常见类型），用于避免误削后缀
    _KNOWN_GENRES: Set[str] = {
        "动作",
        "冒险",
        "动画",
        "喜剧",
        "犯罪",
        "纪录片",
        "剧情",
        "家庭",
        "奇幻",
        "历史",
        "恐怖",
        "音乐",
        "悬疑",
        "爱情",
        "科幻",
        "惊悚",
        "战争",
        "西部",
        "电视电影",
        "运动",
        "古装",
        "武侠",
        "灾难",
        "传记",
        "儿童",
        "短片",
        "情色",
        "同性",
        "歌舞",
        "戏曲",
        "黑色电影",
        "真人秀",
        "脱口秀",
        "综艺",
        "悬疑片",
    }
    # 电影规则可选分类（TMDB 官方电影分类中文名）
    _MOVIE_GENRE_HINT: str = (
        "动作、冒险、动画、喜剧、犯罪、纪录片、剧情、家庭、奇幻、历史、"
        "恐怖、音乐、悬疑、爱情、科幻、惊悚、战争、西部、电视电影"
    )

    # 本插件创建过的订阅及用户手动取消后的抑制列表。
    # 数据使用 _PluginBase.save_data 持久化，不依赖插件配置保存。
    _managed_subscriptions_data_key = "managed_subscriptions"
    _cancelled_subscriptions_data_key = "cancelled_subscriptions"

    _migrate_from_url = ""
    _migrate_api_token = ""
    _migrate_once = False

    def init_plugin(self, config: dict[str, Any] | None = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.mediachain = MediaChain()
        self.doubanapi = DoubanApi()

        # 本轮任务内的豆瓣信息/星级分布缓存，避免同一作品重复请求
        self._douban_info_cache: Dict[str, Dict[str, Any]] = {}
        self._douban_star_cache: Dict[str, List[float]] = {}

        if config:
            self._enabled = config.get("enabled", False)
            self._proxy = config.get("proxy", False)
            self._onlyonce = config.get("onlyonce", False)
            self._is_seasons_all = config.get("is_seasons_all", True)
            self._is_only_movies = config.get("is_only_movies", False)
            self._allow_unrated = config.get("allow_unrated", True)
            self._use_series_rating = config.get("use_series_rating", True)
            self._min_vote_count = self.__safe_int(
                config.get("min_vote_count", 20), default=20, minimum=0
            )
            self._douban_vote = self.__safe_float(
                config.get("douban_vote", 6.0), default=6.0
            )
            if not 0 <= self._douban_vote <= 10:
                logger.warn("豆瓣评分阈值超出 0-10 范围，已按默认值 6.0 处理")
                self._douban_vote = 6.0
            self._vote_strategy = self.__normalize_vote_strategy(
                config.get("vote_strategy", "both")
            )
            self._min_douban_count = self.__safe_int(
                config.get("min_douban_count", 500), default=500, minimum=0
            )
            self._min_tmdb_count = self.__safe_int(
                config.get("min_tmdb_count", 50), default=50, minimum=0
            )
            self._low_reputation_enabled = bool(
                config.get("low_reputation_enabled", True)
            )
            self._low_reputation_max_ratio = self.__safe_float(
                config.get("low_reputation_max_ratio", 0.25), default=0.25
            )
            if not 0 <= self._low_reputation_max_ratio <= 1:
                logger.warn("低口碑占比上限超出 0-1 范围，已按默认值 0.25 处理")
                self._low_reputation_max_ratio = 0.25
            self._low_reputation_margin = self.__safe_float(
                config.get("low_reputation_margin", 0.5), default=0.5
            )
            if self._low_reputation_margin < 0:
                self._low_reputation_margin = 0.5
            self._notify = config.get("notify", True)
            self._title_aliases_raw = str(config.get("title_aliases", "") or "")
            self._title_aliases = self.__parse_title_aliases(self._title_aliases_raw)

            # 分类型评分阈值：剧集 5 个固定分类 + 电影按 TMDB 分类自定义规则
            self._cat_enabled = bool(config.get("cat_enabled", True))
            self._cat_raw = {
                key: str(config.get(key, "") or "")
                for key in (
                    "cat_variety_douban",
                    "cat_variety_tmdb",
                    "cat_anime_douban",
                    "cat_anime_tmdb",
                    "cat_cn_tv_douban",
                    "cat_cn_tv_tmdb",
                    "cat_jk_tv_douban",
                    "cat_jk_tv_tmdb",
                    "cat_west_tv_douban",
                    "cat_west_tv_tmdb",
                    "cat_movie_rules",
                )
            }
            self._cat_tv_rules = {}
            for __category, __prefix in (
                ("variety", "cat_variety"),
                ("anime", "cat_anime"),
                ("cn_tv", "cat_cn_tv"),
                ("jk_tv", "cat_jk_tv"),
                ("west_tv", "cat_west_tv"),
            ):
                __douban_vote = self.__parse_category_vote(
                    config.get(__prefix + "_douban")
                )
                __tmdb_vote = self.__parse_category_vote(
                    config.get(__prefix + "_tmdb")
                )
                if __douban_vote is not None or __tmdb_vote is not None:
                    self._cat_tv_rules[__category] = {
                        "douban": __douban_vote,
                        "tmdb": __tmdb_vote,
                    }
            self._cat_movie_rules = self.__parse_movie_category_rules(
                config.get("cat_movie_rules")
            )

            self._migrate_from_url = config.get("migrate_from_url", "")
            self._migrate_api_token = config.get("migrate_api_token", "")
            self._migrate_once = config.get("migrate_once", False)

            self._cron = (
                config.get("cron", "").strip()
                if config.get("cron", "").strip()
                else ""
            )

            self._release_year = (
                int(config.get("release_year", "").strip())
                if config.get("release_year", "").strip()
                else 0
            )

            self._vote = (
                float(str(config.get("vote", "")).strip())
                if str(config.get("vote", "")).strip()
                else 0.0
            )

            __sleep_time = config.get("sleep_time", "3,10").strip()
            __sleep_time_list = re.split("[,，]", __sleep_time)

            self._min_sleep_time, self._max_sleep_time = (
                3,
                10,
            )  # default values

            if len(__sleep_time_list) == 2:
                __min_sleep_time, __max_sleep_time = map(
                    int, __sleep_time_list
                )
                if __max_sleep_time >= __min_sleep_time:
                    self._min_sleep_time = __min_sleep_time
                    self._max_sleep_time = __max_sleep_time
                else:
                    logger.warn("最大休眠时间小于最小休眠时间,使用默认值")
            else:
                logger.warn("休眠时间配置格式不正确,使用默认值")

            rss_addrs = config.get("rss_addrs")
            if rss_addrs and isinstance(rss_addrs, str):
                self._rss_addrs = rss_addrs.split("\n")
            else:
                self._rss_addrs = []

            self._ranks = config.get("ranks", [])
            self._clear = config.get("clear", False)
            self._clear_unrecognized = config.get("clear_unrecognized", False)
            self._history_type = config.get(
                "history_type", HistoryDataType.LATEST.value
            )
            self._is_exit_ip_rate_limit = config.get(
                "is_exit_ip_rate_limit", False
            )

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("豆瓣榜单Plus服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.__start_task,
                    trigger="date",
                    run_date=datetime.datetime.now(
                        tz=pytz.timezone(settings.TZ)
                    )
                    + datetime.timedelta(seconds=3),
                )

                if self._scheduler.get_jobs():
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce or self._clear:
                # 记录缓存清理标志
                self._clearflag = self._clear
                # 关闭清理缓存
                self._clear = False

            if self._onlyonce or self._clear_unrecognized:
                # 记录未识别缓存清理标志
                self._clearflag_unrecognized = self._clear_unrecognized
                # 关闭未识别清理缓存
                self._clear_unrecognized = False

            if self._onlyonce or self._clear or self._clear_unrecognized:
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除豆瓣榜单Plus历史记录",
            },
            {
                "path": "/migrate-history",
                "endpoint": self.get_migrate_history,
                "methods": ["GET"],
                "summary": "获取豆瓣榜单Plus历史记录",
            },
            {
                "path": "/migrate-config",
                "endpoint": self.get_migrate_config,
                "methods": ["GET"],
                "summary": "获取豆瓣榜单Plus配置",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [
                {
                    "id": f"{self._plugin_id}",
                    "name": "豆瓣榜单Plus服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__start_task,
                    "kwargs": {},
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": f"{self._plugin_id}",
                    "name": "豆瓣榜单Plus服务",
                    "trigger": CronTrigger.from_crontab("0 8 * * *"),
                    "func": self.__start_task,
                    "kwargs": {},
                }
            ]
        return []

    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        return (
            [
                {
                    "component": "VForm",
                    "content": [
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "enabled",
                                                "label": "启用插件",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "onlyonce",
                                                "label": "立即运行一次",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "proxy",
                                                "label": "使用代理服务器",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "is_seasons_all",
                                                "label": "订阅剧集全季度",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "is_only_movies",
                                                "label": "只订阅电影",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "is_exit_ip_rate_limit",
                                                "label": "豆瓣限制时结束",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "allow_unrated",
                                                "label": "允许未出分影视",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "use_series_rating",
                                                "label": "未出分时参考系列评分",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "clear",
                                                "label": "清理历史记录",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 6, "md": 4},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "clear_unrecognized",
                                                "label": "清理未识别历史",
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
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cron",
                                                "label": "执行周期",
                                                "placeholder": "5位cron表达式，留空自动",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "sleep_time",
                                                "label": "随机休眠时间范围",
                                                "placeholder": "默认: 3,10。减少豆瓣访问频率。格式：最小秒数,最大秒数。",
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
                                    "props": {"cols": 12, "md": 3},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "vote",
                                                "label": "TMDB 评分阈值",
                                                "placeholder": "TMDB 评分大于等于该值，默认 6.0",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 3},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "douban_vote",
                                                "label": "豆瓣评分阈值",
                                                "placeholder": "豆瓣评分大于等于该值，默认 6.0",
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
                                                "model": "vote_strategy",
                                                "label": "评分策略",
                                                "items": [
                                                    {
                                                        "title": "同时达标（豆瓣与TMDB都要满足）",
                                                        "value": "both",
                                                    },
                                                    {
                                                        "title": "任一达标（满足其一即可）",
                                                        "value": "any",
                                                    },
                                                    {
                                                        "title": "仅看豆瓣评分",
                                                        "value": "douban",
                                                    },
                                                    {
                                                        "title": "仅看TMDB评分",
                                                        "value": "tmdb",
                                                    },
                                                ],
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 3},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "release_year",
                                                "label": "上映年份",
                                                "placeholder": "年份大于等于该值才订阅",
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
                                    "props": {"cols": 12, "md": 3},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "min_douban_count",
                                                "label": "豆瓣评价人数下限",
                                                "placeholder": "默认 500，0 表示不限",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 3},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "min_tmdb_count",
                                                "label": "TMDB 票数下限",
                                                "placeholder": "默认 50，0 表示不限",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 3},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "min_vote_count",
                                                "label": "系列前作最低票数",
                                                "placeholder": "默认 20，0 表示不限",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 3},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "low_reputation_margin",
                                                "label": "低口碑检查区间",
                                                "placeholder": "默认 0.5，仅检查阈值上方该分数段",
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
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "low_reputation_enabled",
                                                "label": "启用低口碑过滤（豆瓣1-2星占比过高时拒绝）",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "low_reputation_max_ratio",
                                                "label": "低口碑1-2星占比上限",
                                                "placeholder": "默认 0.25（即 25%）",
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
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "notify",
                                                "label": "运行结束后推送通知（无论是否有新增订阅）",
                                            },
                                        }
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
                                            "component": "VTextarea",
                                            "props": {
                                                "model": "title_aliases",
                                                "label": "TMDB 标题别名",
                                                "placeholder": "每行：RSS标题=TMDB标题，例如：说唱巅峰对决=中国说唱巅峰对决",
                                            },
                                        }
                                    ],
                                }
                            ],
                        },
                            {
                            "component": "VRow",
                            "props": {
                                "cols": 12
                            },
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 12
                                    },
                                    "content": [
                                        {
                                            "component": "VAlert",
                                            "props": {
                                                "type": "info",
                                                "variant": "tonal"
                                            },
                                            "content": [
                                                {
                                                    "component": "p",
                                                    "text": "分类型评分阈值：先判断作品属于哪个分类，再用该分类的阈值按上面的达标模式判定（达标就订阅，不达标就跳过）。剧集分类优先级：综艺 > 动漫 > 日韩剧 > 国产剧 > 欧美剧（综艺按豆瓣的综艺标记判断，动漫按类型“动画”判断，其余按制片国家判断）；电影按 TMDB 分类（作品类型）自定义规则，可写多条，行序优先、第一条命中的生效。"
                                                },
                                                {
                                                    "component": "p",
                                                    "text": "填写方式：留空 = 该分类沿用统一规则；填数字 = 该分类用这个分数；填 0 = 该分类此项不设门槛（该项不参与判定）。未命中任何分类的条目一律按统一规则处理。"
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                            },
                            {
                            "component": "VRow",
                            "props": {
                                "cols": 12,
                                "md": 6
                            },
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 12,
                                        "md": 6
                                    },
                                    "content": [
                                        {
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "cat_enabled",
                                                "label": "启用分类型评分阈值（未命中的分类走统一规则）"
                                            }
                                        }
                                    ]
                                }
                            ]
                            },
                            {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_variety_douban",
                                                "label": "综艺 豆瓣评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_variety_tmdb",
                                                "label": "综艺 TMDB评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_anime_douban",
                                                "label": "动漫 豆瓣评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_anime_tmdb",
                                                "label": "动漫 TMDB评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                }
                            ]
                            },
                            {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_cn_tv_douban",
                                                "label": "国产剧 豆瓣评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_cn_tv_tmdb",
                                                "label": "国产剧 TMDB评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_jk_tv_douban",
                                                "label": "日韩剧 豆瓣评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_jk_tv_tmdb",
                                                "label": "日韩剧 TMDB评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                }
                            ]
                            },
                            {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_west_tv_douban",
                                                "label": "欧美剧 豆瓣评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 6,
                                        "md": 3
                                    },
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cat_west_tv_tmdb",
                                                "label": "欧美剧 TMDB评分",
                                                "placeholder": "留空=统一规则"
                                            }
                                        }
                                    ]
                                }
                            ]
                            },
                            {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": 12
                                    },
                                    "content": [
                                        {
                                            "component": "VTextarea",
                                            "props": {
                                                "model": "cat_movie_rules",
                                                "label": "电影分类规则（TMDB 分类，每行一条，行序优先）",
                                                "placeholder": "恐怖: 6.5/5.0",
                                                "hint": "格式：分类名: 豆瓣阈值/TMDB阈值。例如 恐怖: 6.5/5.0（豆瓣6.5、TMDB5.0）、动画: 8.0（只设豆瓣）、纪录片: /7.0（只设TMDB）、西部: 0/0（两项都不设门槛）；一条规则可写多个分类名，用 / 分隔，如 恐怖/惊悚: 6.5。可选分类：" + self._MOVIE_GENRE_HINT,
                                                "persistentHint": True
                                            }
                                        }
                                    ]
                                }
                            ]
                            },
                        {
                            "component": "VRow",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VCol",
                                    "content": [
                                        {
                                            "component": "VSelect",
                                            "props": {
                                                "model": "history_type",
                                                "label": "数据面板历史显示",
                                                "items": [
                                                    {
                                                        "title": f"{HistoryDataType.LATEST.value}",
                                                        "value": f"{HistoryDataType.LATEST.value}",
                                                    },
                                                    {
                                                        "title": f"{HistoryDataType.RECOGNIZED.value}",
                                                        "value": f"{HistoryDataType.RECOGNIZED.value}",
                                                    },
                                                    {
                                                        "title": f"{HistoryDataType.UNRECOGNIZED.value}",
                                                        "value": f"{HistoryDataType.UNRECOGNIZED.value}",
                                                    },
                                                    {
                                                        "title": f"{HistoryDataType.ALL.value}",
                                                        "value": f"{HistoryDataType.ALL.value}",
                                                    },
                                                ],
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
                                                "chips": True,
                                                "multiple": True,
                                                "model": "ranks",
                                                "label": "热门榜单",
                                                "items": [
                                                    {
                                                        "title": "电影北美票房榜",
                                                        "value": "movie-ustop",
                                                    },
                                                    {
                                                        "title": "一周口碑电影榜",
                                                        "value": "movie-weekly",
                                                    },
                                                    {
                                                        "title": "实时热门电影",
                                                        "value": "movie-real-time",
                                                    },
                                                    {
                                                        "title": "热门综艺",
                                                        "value": "show-domestic",
                                                    },
                                                    {
                                                        "title": "热门电影",
                                                        "value": "movie-hot-gaia",
                                                    },
                                                    {
                                                        "title": "热门电视剧",
                                                        "value": "tv-hot",
                                                    },
                                                    {
                                                        "title": "电影TOP10",
                                                        "value": "movie-top250",
                                                    },
                                                    {
                                                        "title": "电影TOP250",
                                                        "value": "movie-top250-full",
                                                    },
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
                                {
                                    "component": "VCol",
                                    "content": [
                                        {
                                            "component": "VTextarea",
                                            "props": {
                                                "model": "rss_addrs",
                                                "label": "自定义榜单地址",
                                                "placeholder": "",
                                            },
                                        },
                                        {
                                            "component": "VAlert",
                                            "props": {
                                                "type": "info",
                                                "variant": "tonal",
                                            },
                                            "content": [
                                                {
                                                    "component": "p",
                                                    "text": "每行一个地址。地址后可选加分号 `;`，第一个分号后是自定义地址的下载路径，用#按类型分割下载路径/电影#/电视剧#/动漫；第二个分号后以@开头并以@结尾，则按类型订阅，只订阅电影：@movies@，只订阅电视剧： @tv@。如果你只需要类型则以两个分号+@作为类型选择.。注意电影英文后面是带s的，tv没有s",
                                                },
                                                {
                                                    "component": "p",
                                                    "text": "https://rsshub.app/douban/movie/ustop",
                                                },
                                                {
                                                    "component": "p",
                                                    "text": "https://rsshub.app/douban/movie/ustop;/download_to_path",
                                                },
                                                {
                                                    "component": "p",
                                                    "text": "https://rsshub.app/douban/doulist/44852852;/download_to_movies#/download_to_tv#/download_to_anime",
                                                },
                                                {
                                                    "component": "p",
                                                    "text": "https://rsshub.app/douban/movie/ustop;/download_to_path;@movies@",
                                                },
                                                {
                                                    "component": "p",
                                                    "text": "https://rsshub.app/douban/doulist/44852852;;@tv@",
                                                },
                                            ],
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
                                            "component": "VAlert",
                                            "props": {
                                                "type": "info",
                                                "variant": "tonal",
                                            },
                                            "content": [
                                                {
                                                    "component": "span",
                                                    "text": f"{self._msg_install}",
                                                }
                                            ],
                                        },
                                        {
                                            "component": "VAlert",
                                            "props": {
                                                "type": "info",
                                                "variant": "tonal",
                                            },
                                            "content": [
                                                {
                                                    "component": "span",
                                                    "text": f"下面配置仅在需要迁移插件的历史记录和配置时，在新MP中填写，开启运行一次选项并立即运行一次。原MP不需要填写下面的配置或开启选项，{self._msg_migrate_install}",
                                                }
                                            ],
                                        },
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
                                            "component": "VSwitch",
                                            "props": {
                                                "model": "migrate_once",
                                                "label": "迁移配置和历史一次",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "migrate_from_url",
                                                "label": "原MP地址: 例如 http://mp.com:3001",
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12, "md": 6},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "migrate_api_token",
                                                "label": "原MP API Token",
                                            },
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
            {
                "enabled": False,
                "cron": "",
                "proxy": False,
                "onlyonce": False,
                "vote": 0.0,
                "ranks": [],
                "rss_addrs": [],
                "clear": False,
                "clear_unrecognized": False,
                "release_year": "0",
                "sleep_time": "3,10",
                "is_seasons_all": True,
                "is_only_movies": False,
                "allow_unrated": True,
                "use_series_rating": True,
                "min_vote_count": "20",
                "douban_vote": "6.0",
                "vote_strategy": "both",
                "min_douban_count": "500",
                "min_tmdb_count": "50",
                "low_reputation_enabled": True,
                "low_reputation_max_ratio": "0.25",
                "low_reputation_margin": "0.5",
                "notify": True,
                "title_aliases": "",
                "cat_enabled": True,
                "cat_variety_douban": "",
                "cat_variety_tmdb": "",
                "cat_anime_douban": "",
                "cat_anime_tmdb": "",
                "cat_cn_tv_douban": "",
                "cat_cn_tv_tmdb": "",
                "cat_jk_tv_douban": "",
                "cat_jk_tv_tmdb": "",
                "cat_west_tv_douban": "",
                "cat_west_tv_tmdb": "",
                "cat_movie_rules": "",
                "history_type": HistoryDataType.LATEST.value,
                "is_exit_ip_rate_limit": False,
                "migrate_from_url": "",
                "migrate_api_token": "",
                "migrate_once": False,
            },
        )

    @staticmethod
    def __get_svg_content(color: str, ds: List[str]):
        def __get_path_content(fill: str, d: str) -> dict[str, Any]:
            return {
                "component": "path",
                "props": {"fill": fill, "d": d},
            }

        path_content = [__get_path_content(color, d) for d in ds]
        component = {
            "component": "svg",
            "props": {
                "class": "icon",
                "viewBox": "0 0 1024 1024",
                "width": "40",
                "height": "40",
            },
            "content": path_content,
        }
        return component

    @staticmethod
    def __get_icon_content():
        color = "#8a8a8a"
        icon_content = {
            Icons.RECOGNIZED: DoubanRankPlus2.__get_svg_content(
                color,
                [
                    "M512 417.792c-53.248 0-94.208 40.96-94.208 94.208 0 53.248 40.96 94.208 94.208 94.208 53.248 0 94.208-40.96 94.208-94.208 0-53.248-40.96-94.208-94.208-94.208z",
                    "M512 229.376C245.76 229.376 36.864 475.136 28.672 487.424c-12.288 16.384-12.288 36.864 0 53.248 8.192 12.288 217.088 258.048 483.328 258.048 266.24 0 475.136-245.76 483.328-258.048 12.288-16.384 12.288-36.864 0-53.248-8.192-12.288-217.088-258.048-483.328-258.048z m0 479.232c-106.496 0-196.608-90.112-196.608-196.608 0-110.592 90.112-196.608 196.608-196.608 110.592 0 196.608 90.112 196.608 196.608 0 110.592-86.016 196.608-196.608 196.608zM61.44 741.376c-24.576 0-40.96 16.384-40.96 40.96v180.224c0 24.576 16.384 40.96 40.96 40.96h180.224c24.576 0 40.96-16.384 40.96-40.96s-16.384-40.96-40.96-40.96H102.4v-139.264c0-24.576-16.384-40.96-40.96-40.96zM61.44 282.624c24.576 0 40.96-16.384 40.96-40.96V102.4H245.76c24.576 0 40.96-16.384 40.96-40.96s-16.384-40.96-40.96-40.96H61.44c-24.576 0-40.96 16.384-40.96 40.96V245.76c0 20.48 16.384 36.864 40.96 36.864zM782.336 102.4h139.264v139.264c0 24.576 16.384 40.96 40.96 40.96s40.96-16.384 40.96-40.96V61.44c0-24.576-16.384-40.96-40.96-40.96h-180.224c-24.576 0-40.96 16.384-40.96 40.96s16.384 40.96 40.96 40.96zM962.56 741.376c-24.576 0-40.96 16.384-40.96 40.96v143.36h-139.264c-24.576 0-40.96 16.384-40.96 40.96s16.384 40.96 40.96 40.96h180.224c24.576 0 40.96-16.384 40.96-40.96v-184.32c0-24.576-16.384-40.96-40.96-40.96z",
                ],
            ),
            Icons.STATISTICS: DoubanRankPlus2.__get_svg_content(
                color,
                [
                    "M471.04 270.336V20.48c-249.856 20.48-450.56 233.472-450.56 491.52 0 274.432 225.28 491.52 491.52 491.52 118.784 0 229.376-40.96 315.392-114.688L655.36 708.608c-40.96 28.672-94.208 45.056-139.264 45.056-135.168 0-245.76-106.496-245.76-245.76 0-114.688 81.92-217.088 200.704-237.568z",
                    "M552.96 20.48v249.856C655.36 286.72 737.28 368.64 753.664 471.04h249.856C983.04 233.472 790.528 40.96 552.96 20.48zM712.704 651.264l176.128 176.128c65.536-77.824 106.496-172.032 114.688-274.432h-249.856c-8.192 36.864-20.48 69.632-40.96 98.304z",
                ],
            ),
            Icons.UNRECOGNIZED: DoubanRankPlus2.__get_svg_content(
                color,
                [
                    "M241.664 921.6H102.4v-139.264c0-24.576-16.384-40.96-40.96-40.96s-40.96 16.384-40.96 40.96v180.224c0 24.576 16.384 40.96 40.96 40.96h180.224c24.576 0 40.96-16.384 40.96-40.96s-16.384-40.96-40.96-40.96zM245.76 20.48H61.44c-24.576 0-40.96 16.384-40.96 40.96V245.76c0 24.576 16.384 40.96 40.96 40.96s40.96-16.384 40.96-40.96V102.4H245.76c24.576 0 40.96-16.384 40.96-40.96s-20.48-40.96-40.96-40.96zM962.56 20.48h-180.224c-24.576 0-40.96 16.384-40.96 40.96s16.384 40.96 40.96 40.96h139.264v139.264c0 24.576 16.384 40.96 40.96 40.96s40.96-16.384 40.96-40.96V61.44c0-24.576-16.384-40.96-40.96-40.96zM962.56 741.376c-24.576 0-40.96 16.384-40.96 40.96v143.36h-139.264c-24.576 0-40.96 16.384-40.96 40.96s16.384 40.96 40.96 40.96h180.224c24.576 0 40.96-16.384 40.96-40.96v-184.32c0-24.576-16.384-40.96-40.96-40.96zM696.32 401.408c0-102.4-81.92-184.32-184.32-184.32S327.68 299.008 327.68 401.408c0 57.344 24.576 110.592 69.632 143.36l-36.864 204.8c-4.096 12.288 0 28.672 8.192 36.864 8.192 12.288 20.48 16.384 36.864 16.384h212.992c12.288 0 28.672-4.096 36.864-16.384 8.192-12.288 12.288-24.576 8.192-36.864l-36.864-204.8c45.056-28.672 69.632-81.92 69.632-143.36z"
                ],
            ),
            Icons.RSS: DoubanRankPlus2.__get_svg_content(
                color,
                [
                    "M320.16155 831.918c0 70.738-57.344 128.082-128.082 128.082S63.99955 902.656 63.99955 831.918s57.344-128.082 128.082-128.082 128.08 57.346 128.08 128.082z m351.32 94.5c-16.708-309.2-264.37-557.174-573.9-573.9C79.31155 351.53 63.99955 366.21 63.99955 384.506v96.138c0 16.83 12.98 30.944 29.774 32.036 223.664 14.568 402.946 193.404 417.544 417.544 1.094 16.794 15.208 29.774 32.036 29.774h96.138c18.298 0.002 32.978-15.31 31.99-33.58z m288.498 0.576C943.19155 459.354 566.92955 80.89 97.00555 64.02 78.94555 63.372 63.99955 77.962 63.99955 96.032v96.136c0 17.25 13.67 31.29 30.906 31.998 382.358 15.678 689.254 322.632 704.93 704.93 0.706 17.236 14.746 30.906 31.998 30.906h96.136c18.068-0.002 32.658-14.948 32.01-33.008z"
                ],
            ),
        }
        return icon_content

    @staticmethod
    def __get_historys_statistic_content(
        title: str, value: str, icon_name: Icons
    ) -> dict[str, Any]:
        icon_content = DoubanRankPlus2.__get_icon_content().get(icon_name, "")
        total_elements = {
            "component": "VCol",
            "props": {"cols": 6, "md": 3},
            "content": [
                {
                    "component": "VCard",
                    "props": {
                        "variant": "tonal",
                    },
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {
                                "class": "d-flex align-center",
                            },
                            "content": [
                                icon_content,
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "ml-2",
                                    },
                                    "content": [
                                        {
                                            "component": "span",
                                            "props": {"class": "text-caption"},
                                            "text": f"{title}",
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "d-flex align-center flex-wrap"
                                            },
                                            "content": [
                                                {
                                                    "component": "span",
                                                    "props": {
                                                        "class": "text-h6"
                                                    },
                                                    "text": f"{value}",
                                                }
                                            ],
                                        },
                                    ],
                                },
                            ],
                        }
                    ],
                },
            ],
        }
        return total_elements

    def __get_historys_statistics_content(
        self,
        historys_total,
        historys_recognized_total,
        historys_unrecognized_total,
    ):
        addr_list = self._rss_addrs + [
            self._douban_address.get(rank) for rank in self._ranks
        ]

        # 数据统计
        data_statistics = [
            {
                "title": "历史总计数量",
                "value": historys_total,
                "icon_name": Icons.STATISTICS,
            },
            {
                "title": "已识别数量",
                "value": historys_recognized_total,
                "icon_name": Icons.RECOGNIZED,
            },
            {
                "title": "未识别数量",
                "value": historys_unrecognized_total,
                "icon_name": Icons.UNRECOGNIZED,
            },
            {
                "title": "榜单数量",
                "value": len(addr_list),
                "icon_name": Icons.RSS,
            },
        ]

        content = list(
            map(
                lambda s: DoubanRankPlus2.__get_historys_statistic_content(
                    title=s["title"],
                    value=s["value"],
                    icon_name=s["icon_name"],
                ),
                data_statistics,
            )
        )

        component = {"component": "VRow", "content": content}
        return component

    def __get_history_post_content(self, history: HistoryPayload):
        title = history.get("title", "")
        if len(title) > 8:
            title = title[:8] + "..."
        title = title.replace(" ", "")

        year = history.get("year")
        vote = history.get("vote")
        poster = history.get("poster")
        time_str = history.get("time")
        mtype = history.get("type")
        doubanid = history.get("doubanid")
        tmdbid = history.get("tmdbid")

        status = history.get("status")
        unique = history.get("unique")
        category_text = str(history.get("category") or "").strip() or "统一规则"

        if (
            tmdbid
            and tmdbid != "0"
            and (mtype == MediaType.MOVIE.value or mtype == MediaType.TV.value)
        ):
            type_str = "movie" if mtype == MediaType.MOVIE.value else "tv"
            href = f"https://www.themoviedb.org/{type_str}/{tmdbid}"
        elif doubanid and doubanid != "0":
            href = f"https://movie.douban.com/subject/{doubanid}"
        else:
            href = "#"

        component = {
            "component": "VCard",
            "props": {
                "variant": "tonal",
            },
            "content": [
                {
                    "component": "VDialogCloseBtn",
                    "props": {
                        "innerClass": "absolute -top-4 right-0 scale-50 opacity-50",
                    },
                    "events": {
                        "click": {
                            "api": f"plugin/{self._plugin_id}/delete_history",
                            "method": "get",
                            "params": {
                                "key": f"{unique}",
                                "apikey": settings.API_TOKEN,
                            },
                        }
                    },
                },
                {
                    "component": "div",
                    "props": {
                        "class": "d-flex justify-space-start flex-nowrap flex-row",
                    },
                    "content": [
                        {
                            "component": "div",
                            "content": [
                                {
                                    "component": "VImg",
                                    "props": {
                                        "src": poster,
                                        "height": 150,
                                        "width": 100,
                                        "aspect-ratio": "2/3",
                                        "class": "object-cover shadow ring-gray-500",
                                        "cover": True,
                                        "transition": True,
                                        "lazy-src": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGQAAACWCAQAAACCseXNAAAAkklEQVR42u3PAREAAAQEMJ9cFFUVkMBtDZbpeiEiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIpcFcbGoK4SMl3wAAAAASUVORK5CYII=",  # 添加懒加载
                                    },
                                }
                            ],
                        },
                        {
                            "component": "div",
                            "content": [
                                {
                                    "component": "VCardTitle",
                                    "props": {
                                        "class": "py-1 pl-2 pr-4 text-lg whitespace-nowrap"
                                    },
                                    "content": [
                                        {
                                            "component": "a",
                                            "props": {
                                                "href": f"{href}",
                                                "target": "_blank",
                                            },
                                            "text": title,
                                        }
                                    ],
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"类型: {mtype}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"年份: {year}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"评分: {vote}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"时间: {time_str}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"状态: {status}",
                                },
                                {
                                    "component": "VCardText",
                                    "props": {"class": "pa-0 px-2"},
                                    "text": f"分类: {category_text}",
                                },
                            ],
                        },
                    ],
                },
            ],
        }

        return component

    def __get_historys_posts_content(
        self, historys: List[HistoryPayload] | None
    ):
        posts_content = []
        if not historys:
            posts_content = [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {
                        "class": "text-start",
                    },
                }
            ]
        else:
            for history in historys:
                posts_content.append(self.__get_history_post_content(history))

        component = {
            "component": "div",
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {
                        "class": "pt-6 pb-2 px-0 text-base whitespace-nowrap"
                    },
                    "content": [
                        {
                            "component": "span",
                            "text": f"{self._history_type}",
                        }
                    ],
                },
                {
                    "component": "div",
                    "props": {
                        "class": "grid gap-3 grid-info-card p-4",
                    },
                    "content": posts_content,
                },
            ],
        }

        return component

    def get_page(self) -> List[Dict[str, Any]]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """

        # 查询历史记录
        historys = self.get_data("history")
        if not historys:
            return [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {
                        "class": "text-center",
                    },
                }
            ]

        # 数据按时间降序排序
        historys = sorted(
            historys, key=lambda x: x.get("time_full"), reverse=True
        )

        history_recognized = []
        history_unrecognized = []

        for history in historys:
            if history.get("status") != Status.UNRECOGNIZED.value:
                history_recognized.append(history)
            else:
                history_unrecognized.append(history)

        history_recognized = sorted(
            history_recognized, key=lambda x: x.get("time_full"), reverse=True
        )
        history_unrecognized = sorted(
            history_unrecognized,
            key=lambda x: x.get("time_full"),
            reverse=True,
        )

        historys_total = len(historys)
        historys_recognized_total = len(history_recognized)
        historys_unrecognized_total = len(history_unrecognized)

        historys_in_type: list[HistoryPayload] | None = None
        if self._history_type == HistoryDataType.LATEST.value:
            historys_in_type = historys[:12]
        elif self._history_type == HistoryDataType.RECOGNIZED.value:
            historys_in_type = history_recognized
        elif self._history_type == HistoryDataType.UNRECOGNIZED.value:
            historys_in_type = history_unrecognized
        elif self._history_type == HistoryDataType.ALL.value:
            historys_in_type = historys

        historys_posts_content = self.__get_historys_posts_content(
            historys_in_type
        )
        historys_statistics_content = self.__get_historys_statistics_content(
            historys_total,
            historys_recognized_total,
            historys_unrecognized_total,
        )

        # 拼装页面
        return [
            {
                "component": "div",
                "content": [
                    historys_statistics_content,
                    historys_posts_content,
                ],
            }
        ]

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))

    def __validate_token(self, api_token: str) -> Any:
        """
        验证 API 密钥
        """
        if api_token != settings.API_TOKEN:
            return Response(success=False, message="API密钥错误")
        return None

    def delete_history(self, key: str, apikey: str):
        """
        删除同步历史记录
        """
        logger.debug(f"删除同步历史记录:::{key}")
        validation_response = self.__validate_token(apikey)
        if validation_response:
            return validation_response
        # 历史记录
        historys = self.get_data("history")
        if not historys:
            return Response(success=False, message="未找到历史记录")
        # 删除指定记录
        historys = [h for h in historys if h.get("unique") != key]
        self.save_data("history", historys)
        return Response(success=True, message="删除成功")

    def get_migrate_history(self, migrate_api_token: str):
        """
        获取迁移l历史记录
        """
        logger.debug("获取迁移历史记录")
        validation_response = self.__validate_token(migrate_api_token)
        if validation_response:
            return validation_response

        return self.get_data("history")

    def get_migrate_config(self, migrate_api_token: str):
        """
        获取迁移配置
        """
        validation_response = self.__validate_token(migrate_api_token)
        if validation_response:
            return validation_response

        __config = self.__get_config()
        logger.debug(f"获取迁移配置:::{__config}")
        # 删除不需要的键
        for key in ["migrate_api_token", "migrate_from_url", "migrate_once"]:
            __config.pop(key, None)
        return __config

    def __get_config(self):
        """
        获取配置
        """
        return {
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "vote": self._vote,
            "ranks": self._ranks,
            "rss_addrs": "\n".join(map(str, self._rss_addrs)),
            "clear": self._clear,
            "clear_unrecognized": self._clear_unrecognized,
            "is_seasons_all": self._is_seasons_all,
            "is_only_movies": self._is_only_movies,
            "allow_unrated": self._allow_unrated,
            "use_series_rating": self._use_series_rating,
            "min_vote_count": str(self._min_vote_count),
            "douban_vote": str(self._douban_vote),
            "vote_strategy": self._vote_strategy,
            "min_douban_count": str(self._min_douban_count),
            "min_tmdb_count": str(self._min_tmdb_count),
            "low_reputation_enabled": self._low_reputation_enabled,
            "low_reputation_max_ratio": str(self._low_reputation_max_ratio),
            "low_reputation_margin": str(self._low_reputation_margin),
            "notify": self._notify,
            "title_aliases": self._title_aliases_raw,
            "cat_enabled": self._cat_enabled,
            **self._cat_raw,
            "release_year": str(self._release_year),
            "sleep_time": f"{self._min_sleep_time},{self._max_sleep_time}",
            "history_type": self._history_type,
            "is_exit_ip_rate_limit": self._is_exit_ip_rate_limit,
            "migrate_from_url": self._migrate_from_url.rstrip("/"),
            "migrate_api_token": self._migrate_api_token,
            "migrate_once": self._migrate_once,
        }

    def __update_config(self):
        """
        更新配置
        """
        __config = self.__get_config()
        logger.debug(f"更新配置 {__config}")
        self.update_config(__config)

    @staticmethod
    def __safe_int(value: Any, default: int = 0, minimum: int | None = None) -> int:
        try:
            result = int(str(value).strip())
        except (TypeError, ValueError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        return result

    @staticmethod
    def __safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __normalize_subscription_season(season: Any) -> int | None:
        """统一电影/剧集季号，避免 None、空串和字符串数字造成键不一致。"""
        if season in (None, "", 0, "0"):
            return None
        try:
            return int(season)
        except (TypeError, ValueError):
            return None

    @classmethod
    def __build_subscription_key(
        cls,
        tmdbid: Any,
        mtype: Any,
        season: Any = None,
    ) -> str | None:
        """使用 TMDB ID、媒体类型和季号构造稳定的订阅抑制键。"""
        if tmdbid in (None, "", 0, "0"):
            return None
        media_type = mtype.value if isinstance(mtype, MediaType) else str(mtype or "")
        normalized_season = cls.__normalize_subscription_season(season)
        return f"tmdb:{tmdbid}|type:{media_type}|season:{normalized_season or 0}"

    @staticmethod
    def __normalize_subscription_records(data: Any) -> Dict[str, Dict[str, Any]]:
        """兼容并清理持久化记录，只保留具有稳定 key 的字典项。"""
        if isinstance(data, dict):
            return {
                str(key): value
                for key, value in data.items()
                if key and isinstance(value, dict)
            }
        if isinstance(data, list):
            return {
                str(item.get("key")): item
                for item in data
                if isinstance(item, dict) and item.get("key")
            }
        return {}

    def __get_managed_subscriptions(self) -> Dict[str, Dict[str, Any]]:
        return self.__normalize_subscription_records(
            self.get_data(self._managed_subscriptions_data_key)
        )

    def __get_cancelled_subscriptions(self) -> Dict[str, Dict[str, Any]]:
        return self.__normalize_subscription_records(
            self.get_data(self._cancelled_subscriptions_data_key)
        )

    def __record_managed_subscription(
        self,
        subscribe_id: Any,
        mediainfo: MediaInfo,
        season: Any,
    ) -> None:
        """记录本插件实际创建的订阅，供删除事件进行来源校验。"""
        key = self.__build_subscription_key(
            mediainfo.tmdb_id, mediainfo.type, season
        )
        if not key:
            logger.warn(f"{mediainfo.title_year} 缺少 TMDB ID，无法监控手动取消")
            return
        records = self.__get_managed_subscriptions()
        records[key] = {
            "key": key,
            "subscribe_id": subscribe_id,
            "title": mediainfo.title,
            "year": mediainfo.year,
            "type": mediainfo.type.value,
            "tmdbid": mediainfo.tmdb_id,
            "season": self.__normalize_subscription_season(season),
            "created_at": datetime.datetime.now(
                tz=pytz.timezone(settings.TZ)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data(self._managed_subscriptions_data_key, records)

    def __is_subscription_cancelled(
        self, mediainfo: MediaInfo, season: Any
    ) -> bool:
        key = self.__build_subscription_key(
            mediainfo.tmdb_id, mediainfo.type, season
        )
        return bool(key and key in self.__get_cancelled_subscriptions())

    @eventmanager.register(EventType.SubscribeDeleted)
    def subscribe_deleted(self, event: ManagerEvent) -> None:
        """
        监听 MoviePilot 的订阅删除事件。

        仅当被删除订阅确实由本插件创建时才写入抑制列表；用户自己创建或其他
        插件创建的订阅不会受影响。完成订阅由主程序自动迁入历史时不会触发该事件。
        """
        event_data = event.event_data if event else None
        if not isinstance(event_data, dict):
            return
        subscribe_info = event_data.get("subscribe_info")
        if not isinstance(subscribe_info, dict):
            return

        subscribe_id = event_data.get("subscribe_id") or subscribe_info.get("id")
        key = self.__build_subscription_key(
            subscribe_info.get("tmdbid"),
            subscribe_info.get("type"),
            subscribe_info.get("season"),
        )
        if not key:
            return

        managed = self.__get_managed_subscriptions()
        managed_record = managed.get(key)
        is_plugin_owned = subscribe_info.get("username") == self.plugin_name
        is_recorded = bool(
            managed_record
            and (
                subscribe_id in (None, "")
                or str(managed_record.get("subscribe_id")) == str(subscribe_id)
            )
        )
        if not (is_plugin_owned or is_recorded):
            return

        cancelled = self.__get_cancelled_subscriptions()
        cancelled[key] = {
            **(managed_record or {}),
            "key": key,
            "subscribe_id": subscribe_id,
            "title": subscribe_info.get("name")
            or (managed_record or {}).get("title")
            or "未知影视",
            "year": subscribe_info.get("year")
            or (managed_record or {}).get("year"),
            "type": subscribe_info.get("type"),
            "tmdbid": subscribe_info.get("tmdbid"),
            "season": self.__normalize_subscription_season(
                subscribe_info.get("season")
            ),
            "cancelled_at": datetime.datetime.now(
                tz=pytz.timezone(settings.TZ)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data(self._cancelled_subscriptions_data_key, cancelled)
        if key in managed:
            managed.pop(key, None)
            self.save_data(self._managed_subscriptions_data_key, managed)
        logger.info(
            f"检测到手动取消本插件订阅：{cancelled[key]['title']}"
            f"{' 第' + str(cancelled[key]['season']) + '季' if cancelled[key]['season'] else ''}，"
            "已加入不再自动订阅列表"
        )

    @staticmethod
    def __chinese_number_to_int(value: str) -> int | None:
        """解析常见中文季号，支持一至九十九及阿拉伯数字。"""
        value = str(value or "").strip()
        if not value:
            return None
        if value.isdigit():
            return int(value)
        digits = {
            "零": 0,
            "〇": 0,
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
        }
        if value == "十":
            return 10
        if "十" in value:
            left, right = value.split("十", 1)
            tens = digits.get(left, 1) if left else 1
            ones = digits.get(right, 0) if right else 0
            return tens * 10 + ones
        if all(char in digits for char in value):
            result = 0
            for char in value:
                result = result * 10 + digits[char]
            return result
        return None

    @classmethod
    def __parse_season_title(
        cls, title: str, year: str | None = None
    ) -> Tuple[str, int | None]:
        """将“节目名 第十季”拆成 TMDB 系列基础名与季号。"""
        original = str(title or "").strip()
        season = None
        patterns = [
            r"第\s*([零〇一二两三四五六七八九十\d]+)\s*季",
            r"\bSeason\s*(\d+)\b",
            r"\bS(\d{1,2})\b",
        ]
        base_title = original
        for pattern in patterns:
            match = re.search(pattern, base_title, flags=re.IGNORECASE)
            if not match:
                continue
            season = cls.__chinese_number_to_int(match.group(1))
            base_title = re.sub(
                pattern, " ", base_title, flags=re.IGNORECASE
            ).strip()
            break
        base_title = re.sub(r"\s+", " ", base_title).strip(" -–—·:：")
        if year:
            base_title = re.sub(
                rf"[\s·_\-–—]*{re.escape(str(year))}$", "", base_title
            ).strip(" -–—·:：")
        return base_title or original, season

    @staticmethod
    def __parse_title_aliases(raw_value: str) -> Dict[str, str]:
        aliases: Dict[str, str] = {}
        for line in str(raw_value or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"\s*[=＝]\s*", line, maxsplit=1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                logger.warn(f"忽略格式错误的标题别名：{line}")
                continue
            aliases[parts[0].strip()] = parts[1].strip()
        return aliases

    def __get_alias_title(self, title: str) -> str | None:
        normalized = str(title or "").strip()
        if not normalized:
            return None
        if normalized in self._title_aliases:
            return self._title_aliases[normalized]
        compact = re.sub(r"\s+", "", normalized).lower()
        for source, target in self._title_aliases.items():
            if re.sub(r"\s+", "", source).lower() == compact:
                return target
        return None

    @staticmethod
    def __is_retryable_status(status: str | None) -> bool:
        return status in {
            Status.UNRECOGNIZED.value,
            Status.RATING_PENDING.value,
            Status.RETRY_LATER.value,
        }

    @staticmethod
    def __is_legacy_zero_vote_history(item: dict[str, Any] | None) -> bool:
        """兼容旧版：曾被作为“评分不符合”写入历史的 0 分条目需要重新评估。"""
        if not item or item.get("status") != Status.RATING_NOT_MATCH.value:
            return False
        try:
            return float(item.get("vote") or 0) <= 0
        except (TypeError, ValueError):
            return False

    def __remove_retryable_history(
        self, history: List[dict[str, Any]], unique_flag: str
    ) -> None:
        history[:] = [
            item
            for item in history
            if not (
                item
                and item.get("unique") == unique_flag
                and (
                    self.__is_retryable_status(item.get("status"))
                    or self.__is_legacy_zero_vote_history(item)
                )
            )
        ]

    def __recognize_rss_media(
        self,
        title: str,
        year: str | None,
        douban_id: str | None,
        mtype: MediaType | None,
        force_tv: bool = False,
    ) -> Tuple[MediaInfo | None, bool]:
        """
        增强识别：豆瓣ID优先；失败后使用系列基础名、季号、别名文本识别。
        返回 (媒体信息, 是否建议稍后重试)。
        """
        base_title, season = self.__parse_season_title(title, year)
        if force_tv or season:
            mtype = MediaType.TV

        if douban_id:
            try:
                tmdbinfo, is_rate_limit = self.__get_tmdbinfo_by_doubanid(
                    doubanid=douban_id, mtype=mtype
                )
                if tmdbinfo:
                    tmdb_type = tmdbinfo.get("media_type") or mtype
                    if not isinstance(tmdb_type, MediaType):
                        type_text = str(tmdb_type or "").strip().lower()
                        if type_text in {"tv", "电视剧", "剧集"}:
                            tmdb_type = MediaType.TV
                        elif type_text in {"movie", "电影"}:
                            tmdb_type = MediaType.MOVIE
                        else:
                            tmdb_type = mtype
                    mediainfo = self.chain.recognize_media(
                        tmdbid=tmdbinfo.get("id"),
                        mtype=tmdb_type,
                        cache=False,
                    )
                    if mediainfo:
                        logger.info(
                            f"通过豆瓣ID {douban_id} 匹配到TMDB："
                            f"{mediainfo.title_year} ({mediainfo.tmdb_id})"
                        )
                        return mediainfo, False
                if is_rate_limit:
                    logger.warn(f"豆瓣ID {douban_id} 查询触发速率限制，改用文本识别")
            except Exception as err:
                logger.warn(f"豆瓣ID {douban_id} 匹配TMDB失败，改用文本识别：{err}")

        candidates = []
        for candidate in [
            base_title,
            self.__get_alias_title(base_title),
            self.__get_alias_title(title),
            title,
        ]:
            candidate = str(candidate or "").strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        # 电视剧/综艺的 RSS 年份通常是当前季年份，而 TMDB 搜索使用系列首播年份。
        # 因此先带年份精确匹配，再自动去掉年份重试，兼顾准确率和综艺识别率。
        search_years = [year]
        if mtype == MediaType.TV and year:
            search_years.append(None)

        for candidate in candidates:
            for search_year in search_years:
                meta = MetaInfo(candidate)
                meta.year = search_year
                if mtype:
                    meta.type = mtype
                if season:
                    meta.type = MediaType.TV
                    meta.begin_season = season
                logger.info(
                    f"TMDB文本识别：Title={candidate}, Year={search_year}, "
                    f"Type={meta.type}, Season={season}"
                )
                try:
                    mediainfo = self.chain.recognize_media(meta=meta, cache=False)
                except Exception as err:
                    logger.error(f"TMDB文本识别请求失败：{candidate}，{err}")
                    return None, True
                if mediainfo:
                    return mediainfo, False
        return None, False

    def __tmdb_get(
        self, endpoint: str, params: Dict[str, Any] | None = None
    ) -> Tuple[dict[str, Any] | None, bool]:
        """调用 TMDB 官方接口；第二个返回值表示请求是否成功。"""
        api_key = getattr(settings, "TMDB_API_KEY", None)
        api_domain = getattr(settings, "TMDB_API_DOMAIN", "api.themoviedb.org")
        if not api_key:
            logger.error("未配置 TMDB_API_KEY，无法查询电影合集")
            return None, False
        url = f"https://{api_domain}/3/{endpoint.lstrip('/')}"
        query = {"api_key": api_key, "language": "zh-CN"}
        query.update(params or {})
        try:
            response = requests.get(
                url,
                params=query,
                timeout=30,
                proxies=settings.PROXY or {},
            )
            if response.status_code != 200:
                logger.error(
                    f"TMDB请求失败：{endpoint}，状态码={response.status_code}"
                )
                return None, False
            data = response.json()
            return data if isinstance(data, dict) else None, True
        except requests.RequestException as err:
            logger.error(f"TMDB请求异常：{endpoint}，{err}")
            return None, False
        except ValueError as err:
            logger.error(f"TMDB响应解析失败：{endpoint}，{err}")
            return None, False

    def __get_movie_series_vote(
        self, mediainfo: MediaInfo
    ) -> Tuple[float | None, int, bool]:
        """返回电影有效前作的平均分、数量、查询是否成功。"""
        details, success = self.__tmdb_get(f"movie/{mediainfo.tmdb_id}")
        if not success or not details:
            return None, 0, False
        collection = details.get("belongs_to_collection")
        if not collection or not collection.get("id"):
            return None, 0, True
        collection_info, success = self.__tmdb_get(
            f"collection/{collection.get('id')}"
        )
        if not success or not collection_info:
            return None, 0, False

        current_date = details.get("release_date")
        if not current_date and mediainfo.year:
            current_date = f"{mediainfo.year}-12-31"
        votes = []
        for part in collection_info.get("parts") or []:
            if part.get("id") == mediainfo.tmdb_id:
                continue
            release_date = part.get("release_date")
            if not release_date or (current_date and release_date >= current_date):
                continue
            vote = self.__safe_float(part.get("vote_average"))
            vote_count = self.__safe_int(part.get("vote_count"), minimum=0)
            if vote <= 0 or vote_count < self._min_vote_count:
                continue
            votes.append(vote)
        if not votes:
            return None, 0, True
        return sum(votes) / len(votes), len(votes), True

    def __get_tv_season_vote(
        self, tmdb_id: int, season: int
    ) -> Tuple[float | None, int, bool]:
        """优先读取季评分，缺失时按各集票数加权计算；返回 (评分, 票数, 是否成功)。"""
        try:
            season_info = self.chain.tmdb_info(
                tmdbid=tmdb_id, mtype=MediaType.TV, season=season
            )
        except Exception as err:
            logger.error(f"查询TMDB季详情失败：TMDB={tmdb_id}, S{season:02d}，{err}")
            return None, 0, False
        if season_info is None:
            return None, 0, False
        if not season_info:
            return None, 0, True
        season_vote = self.__safe_float(season_info.get("vote_average"))
        season_count = self.__safe_int(season_info.get("vote_count"), minimum=0)
        if season_vote > 0:
            return season_vote, season_count, True

        weighted_sum = 0.0
        total_count = 0
        for episode in season_info.get("episodes") or []:
            vote = self.__safe_float(episode.get("vote_average"))
            vote_count = self.__safe_int(episode.get("vote_count"), minimum=0)
            if vote <= 0 or vote_count < self._min_vote_count:
                continue
            weighted_sum += vote * vote_count
            total_count += vote_count
        if total_count <= 0:
            return None, 0, True
        return weighted_sum / total_count, total_count, True

    def __check_unrated_policy(
        self, mediainfo: MediaInfo, season: int | None
    ) -> Status | None:
        """评分为0时，按电影前作或电视剧往季的平均评分决定。"""
        if not self._allow_unrated:
            logger.info(f"{mediainfo.title_year} 暂无评分，配置为不允许未出分影视")
            return Status.RATING_PENDING
        if not self._use_series_rating:
            logger.info(f"{mediainfo.title_year} 暂无评分，未启用系列评分，直接放行")
            return None

        if mediainfo.type == MediaType.MOVIE:
            average, count, success = self.__get_movie_series_vote(mediainfo)
            if not success:
                logger.warn(f"{mediainfo.title_year} 前作评分查询失败，等待重试")
                return Status.RETRY_LATER
            if count == 0 or average is None:
                logger.info(f"{mediainfo.title_year} 没有有效前作评分，按新系列电影放行")
                return None
            logger.info(
                f"{mediainfo.title_year} 当前未出分，{count} 部有效前作平均分 "
                f"{average:.2f}，最低要求 {self._vote}"
            )
            return None if average >= self._vote else Status.RATING_PENDING

        current_season = season or 1
        current_vote, _current_vote_count, success = self.__get_tv_season_vote(
            mediainfo.tmdb_id, current_season
        )
        if not success:
            return Status.RETRY_LATER
        if current_vote is not None and current_vote > 0:
            logger.info(
                f"{mediainfo.title_year} 第{current_season}季评分 "
                f"{current_vote:.2f}，最低要求 {self._vote}"
            )
            return None if current_vote >= self._vote else Status.RATING_PENDING
        if current_season <= 1:
            logger.info(f"{mediainfo.title_year} 为第一季且暂无评分，直接放行")
            return None

        previous_votes = []
        for season_number in range(1, current_season):
            vote, _season_vote_count, success = self.__get_tv_season_vote(
                mediainfo.tmdb_id, season_number
            )
            if not success:
                return Status.RETRY_LATER
            if vote is not None and vote > 0:
                previous_votes.append(vote)
        if not previous_votes:
            logger.info(f"{mediainfo.title_year} 没有有效往季评分，直接放行")
            return None
        average = sum(previous_votes) / len(previous_votes)
        logger.info(
            f"{mediainfo.title_year} 第{current_season}季暂无评分，"
            f"{len(previous_votes)} 个有效往季平均分 {average:.2f}，"
            f"最低要求 {self._vote}"
        )
        return None if average >= self._vote else Status.RATING_PENDING

    # ==================== 豆瓣/TMDB 双评分过滤 ====================

    @staticmethod
    def __normalize_vote_strategy(strategy: Any) -> str:
        """评分策略：both=同时达标，any=任一达标，douban=仅豆瓣，tmdb=仅TMDB。"""
        value = str(strategy or "").strip().lower()
        return value if value in {"both", "any", "douban", "tmdb"} else "both"

    def __is_douban_gate_enabled(self) -> bool:
        """豆瓣评分是否参与门槛判定。"""
        return self._douban_vote > 0 and self._vote_strategy in {
            "both",
            "any",
            "douban",
        }

    def __is_tmdb_gate_enabled(self) -> bool:
        """TMDB 评分是否参与门槛判定。"""
        return self._vote > 0 and self._vote_strategy in {"both", "any", "tmdb"}

    def __get_douban_rating(
        self, doubanid: str | None, mtype: MediaType | None = None
    ) -> Tuple[float, int, bool]:
        """返回 (豆瓣评分, 评价人数, 是否有有效评分)；未出分或失败时评分为 0。"""
        if not doubanid:
            return 0.0, 0, False
        info, _is_ip_rate_limit = self.__douban_info(
            doubanid=str(doubanid), mtype=mtype
        )
        if not info or not isinstance(info, dict):
            return 0.0, 0, False
        rating = info.get("rating")
        if not isinstance(rating, dict):
            rating = {}
        vote = self.__safe_float(rating.get("value"))
        if not 0 < vote <= 10:
            vote = 0.0
        count = self.__safe_int(rating.get("count"), minimum=0)
        return vote, count, vote > 0

    def __get_douban_star_stats(
        self, doubanid: str | None, mtype: MediaType | None = None
    ) -> List[float] | None:
        """获取豆瓣星级分布（1~5 星占比，已归一化）；失败返回 None。"""
        if not doubanid:
            return None
        cache_key = str(doubanid)
        if cache_key in self._douban_star_cache:
            return self._douban_star_cache[cache_key]
        subject_type = "tv" if mtype == MediaType.TV else "movie"
        invoke = getattr(self.doubanapi, "_DoubanApi__invoke_search", None)
        if not callable(invoke):
            return None
        try:
            data = invoke(f"/{subject_type}/{doubanid}/rating")
        except Exception as err:
            logger.debug(f"获取豆瓣星级分布失败：{doubanid}，{err}")
            return None
        stats: List[float] = []
        if isinstance(data, dict) and isinstance(data.get("stats"), list):
            for value in data.get("stats") or []:
                stats.append(max(0.0, self.__safe_float(value)))
        if len(stats) != 5 or sum(stats) <= 0:
            return None
        total = sum(stats)
        stats = [value / total for value in stats]
        self._douban_star_cache[cache_key] = stats
        return stats


    @staticmethod
    def __parse_category_vote(value: Any) -> Optional[float]:
        """
        解析分类阈值：留空=沿用统一规则(None)，0=该源不设门槛，其余为具体分数。
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            vote = float(text)
        except (TypeError, ValueError):
            return None
        if vote <= 0:
            return 0.0
        return min(vote, 10.0)

    @staticmethod
    def __normalize_genre_name(name: Any) -> str:
        """归一化类型名：去空白与“片/类”后缀，并把豆瓣独有类型归并到 TMDB 分类。"""
        text = str(name or "").strip().replace(" ", "")
        if not text:
            return ""
        # 原名本身就是已知分类（如“纪录片”）时不做后缀裁剪，避免误伤
        candidates = [text]
        for suffix in ("片", "类", "剧"):
            if len(text) > 1 and text.endswith(suffix):
                candidates.append(text[: -len(suffix)])
        for candidate in candidates:
            if candidate in DoubanRankPlus2._KNOWN_GENRES:
                return DoubanRankPlus2._GENRE_ALIASES.get(candidate, candidate)
        resolved = candidates[-1] if len(candidates) > 1 else text
        return DoubanRankPlus2._GENRE_ALIASES.get(resolved, resolved)

    def __parse_movie_category_rules(
        self, raw_value: Any
    ) -> List[Tuple[Set[str], Dict[str, Optional[float]], str]]:
        """
        解析电影分类规则（每行一条，行序优先）：
            恐怖: 6.5/5.0    → 豆瓣 6.5、TMDB 5.0
            动画: 8.0        → 只设豆瓣，TMDB 沿用统一规则
            纪录片: /7.0     → 只设 TMDB，豆瓣沿用统一规则
            西部: 0/0        → 该分类两源都不设门槛
        """
        rules: List[Tuple[Set[str], Dict[str, Optional[float]], str]] = []
        if not raw_value:
            return rules
        for line in str(raw_value).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                name_part, value_part = line.split(":", 1)
            elif "：" in line:
                name_part, value_part = line.split("：", 1)
            else:
                parts = line.split(None, 1)
                name_part = parts[0]
                value_part = parts[1] if len(parts) > 1 else ""
            names = {
                self.__normalize_genre_name(part)
                for part in re.split(r"[/、,，|]", name_part)
            }
            names.discard("")
            if not names:
                logger.warning(f"电影分类规则缺少分类名，已忽略：{line}")
                continue
            value_parts = [
                part.strip()
                for part in re.split(r"[/|，,\s]+", value_part.strip())
            ]
            douban_vote = self.__parse_category_vote(
                value_parts[0] if value_parts else None
            )
            tmdb_vote = self.__parse_category_vote(
                value_parts[1] if len(value_parts) > 1 else None
            )
            if douban_vote is None and tmdb_vote is None:
                logger.warning(f"电影分类规则未设置阈值，已忽略：{line}")
                continue
            rules.append(
                (names, {"douban": douban_vote, "tmdb": tmdb_vote}, line)
            )
        return rules

    def __get_cached_douban_info(
        self, doubanid: str | None
    ) -> Dict[str, Any] | None:
        """读取本轮已缓存的豆瓣详情（不发起请求）。"""
        if not doubanid:
            return None
        info = self._douban_info_cache.get(str(doubanid))
        return info if isinstance(info, dict) and info else None

    def __get_douban_genres_regions(
        self, doubanid: str | None
    ) -> Tuple[List[str], List[str], bool]:
        """从缓存的豆瓣详情提取 (类型列表, 制片国家列表, 是否综艺)。"""
        info = self.__get_cached_douban_info(doubanid)
        if not info:
            return [], [], False
        genres = []
        for genre in info.get("genres") or []:
            name = self.__normalize_genre_name(genre)
            if name:
                genres.append(name)
        regions = [
            str(region).strip()
            for region in (info.get("countries") or [])
            if str(region).strip()
        ]
        return genres, regions, bool(info.get("is_show"))

    def __match_tv_category(
        self, genres: List[str], regions: List[str], is_show: bool
    ) -> Optional[str]:
        """剧集分类：综艺 > 动漫 > 日韩剧 > 国产剧 > 欧美剧；未命中返回 None。"""
        genre_set = set(genres)
        region_set = set(regions)
        if is_show or self._VARIETY_GENRES & genre_set:
            return "variety"
        if self._ANIME_GENRES & genre_set:
            return "anime"
        if self._JK_REGIONS & region_set:
            return "jk_tv"
        if self._CN_REGIONS & region_set:
            return "cn_tv"
        if self._WEST_REGIONS & region_set:
            return "west_tv"
        return None

    def __get_effective_thresholds(
        self, doubanid: str | None, mtype: MediaType | None
    ) -> Tuple[float, float, Optional[str]]:
        """
        返回该作品生效的 (豆瓣阈值, TMDB阈值, 分类名)。
        命中分类规则时用分类阈值，否则沿用统一规则；阈值为 0 表示该源不设门槛。
        """
        if not self._cat_enabled:
            return self._douban_vote, self._vote, None
        genres, regions, is_show = self.__get_douban_genres_regions(doubanid)
        if not genres and not regions and not is_show:
            return self._douban_vote, self._vote, None

        is_tv = mtype == MediaType.TV
        if mtype in (None, MediaType.UNKNOWN):
            info = self.__get_cached_douban_info(doubanid) or {}
            is_tv = bool(info.get("is_tv"))

        rule: Optional[Dict[str, Optional[float]]] = None
        category_name: Optional[str] = None
        if is_tv:
            category = self.__match_tv_category(genres, regions, is_show)
            if category:
                rule = self._cat_tv_rules.get(category)
                category_name = self._CATEGORY_NAMES.get(category)
        else:
            # 电影：按规则行序匹配，第一条命中的生效
            genre_set = set(genres)
            for names, candidate, _raw in self._cat_movie_rules:
                if names & genre_set:
                    rule = candidate
                    category_name = "、".join(sorted(names & genre_set))
                    break

        douban_vote = self._douban_vote
        tmdb_vote = self._vote
        if rule:
            if rule.get("douban") is not None:
                douban_vote = rule["douban"]
            if rule.get("tmdb") is not None:
                tmdb_vote = rule["tmdb"]
        return douban_vote, tmdb_vote, category_name

    def __get_min_douban_threshold(self) -> float:
        """
        所有已配置豆瓣阈值中的最小值，用于早筛第一层的保守拒绝。
        任一类阈值为 0（不设门槛）时返回 0，表示不做零请求早筛。
        """
        if not self._cat_enabled:
            return self._douban_vote
        candidates = [self._douban_vote]
        for rule in self._cat_tv_rules.values():
            if rule.get("douban") is not None:
                candidates.append(rule["douban"])
        for _names, rule, _raw in self._cat_movie_rules:
            if rule.get("douban") is not None:
                candidates.append(rule["douban"])
        return min(candidates) if candidates else self._douban_vote

    def __is_low_reputation(
        self,
        doubanid: str | None,
        mtype: MediaType | None,
        douban_vote: float,
        threshold: float | None = None,
    ) -> bool:
        """
        低口碑判定：仅对“评分刚过阈值”的边界样本查询星级分布，
        1~2 星占比超过上限则判定为低口碑；查询失败时放行。
        阈值取该作品分类规则生效后的豆瓣阈值。
        """
        if threshold is None:
            threshold = self._douban_vote
        if not self._low_reputation_enabled or not doubanid:
            return False
        if threshold <= 0 or douban_vote < threshold:
            return False
        if (
            self._low_reputation_margin > 0
            and douban_vote >= threshold + self._low_reputation_margin
        ):
            return False
        stats = self.__get_douban_star_stats(doubanid, mtype)
        if not stats:
            return False
        low_ratio = stats[0] + stats[1]
        if low_ratio > self._low_reputation_max_ratio:
            logger.info(
                f"豆瓣 {doubanid} 评分 {douban_vote:.1f}，但 1-2 星占比 "
                f"{low_ratio:.1%} 超过上限 {self._low_reputation_max_ratio:.0%}，判定为低口碑"
            )
            return True
        return False

    def __check_douban_gate(
        self, rss_vote: Any, doubanid: str | None, mtype: MediaType | None
    ) -> Tuple[Status | None, float, int, str]:
        """
        识别前的豆瓣评分早筛，返回 (拒绝状态, 豆瓣评分, 评价人数, 原因)。
        仅在豆瓣评分单独即可判定不达标时拒绝（同时达标/仅豆瓣 策略），
        从而省去 TMDB 搜索与逐季查询；未出分时不做早筛。
        """
        if not self.__is_douban_gate_enabled():
            return None, 0.0, 0, ""
        if self._vote_strategy not in {"both", "douban"}:
            return None, 0.0, 0, ""

        # 第一层：RSS 自带评分且低于“所有已配置阈值中的最小值”，零请求直接拒绝
        rss_vote_value = self.__safe_float(rss_vote)
        min_douban_vote = self.__get_min_douban_threshold()
        if (
            min_douban_vote > 0
            and 0 < rss_vote_value <= 10
            and rss_vote_value < min_douban_vote
        ):
            return (
                Status.RATING_NOT_MATCH,
                rss_vote_value,
                0,
                f"RSS 豆瓣评分 {rss_vote_value:.1f} 低于阈值 {min_douban_vote}",
            )

        # 第二层：需要评价人数（或 RSS 无评分）时查询豆瓣详情，本轮缓存复用
        vote, count, has_rating = self.__get_douban_rating(doubanid, mtype)
        if not has_rating:
            return None, 0.0, 0, ""
        if self._min_douban_count > 0 and count < self._min_douban_count:
            # 人数会随剧集更新增长，用可重试状态，避免被历史去重永久跳过
            return (
                Status.RATING_PENDING,
                vote,
                count,
                f"豆瓣评价人数 {count} 低于下限 {self._min_douban_count}",
            )
        # 按分类取生效阈值：分类规则可以比统一规则更严，也可以更松
        douban_threshold, _tmdb_threshold, category_name = (
            self.__get_effective_thresholds(doubanid, mtype)
        )
        self._last_category_name = category_name
        if douban_threshold > 0 and vote < douban_threshold:
            return (
                Status.RATING_NOT_MATCH,
                vote,
                count,
                f"{category_name + ' | ' if category_name else ''}豆瓣评分 {vote:.1f} "
                f"{vote:.1f} 低于阈值 {douban_threshold}",
            )

        # 第三层：边界样本的低口碑判定（1-2 星占比）
        if self.__is_low_reputation(doubanid, mtype, vote, douban_threshold):
            return (
                Status.RATING_NOT_MATCH,
                vote,
                count,
                "豆瓣 1-2 星占比过高（低口碑）",
            )
        return None, vote, count, ""

    def __evaluate_rating_gate(
        self,
        tmdb_vote: float,
        tmdb_count: int,
        douban_vote: float,
        douban_count: int,
        douban_threshold: Optional[float] = None,
        tmdb_threshold: Optional[float] = None,
        category_name: Optional[str] = None,
    ) -> Tuple[Status | None, str, bool]:
        """
        按评分策略评估豆瓣与 TMDB 两个评分源。
        阈值取该作品分类规则生效后的值；阈值为 0 表示该源不参与判定。
        返回 (拒绝状态, 说明, 是否需要走未出分策略)。
        """
        if douban_threshold is None:
            douban_threshold = self._douban_vote
        if tmdb_threshold is None:
            tmdb_threshold = self._vote
        results: Dict[str, str] = {}
        if self.__is_tmdb_gate_enabled() and tmdb_threshold > 0:
            if tmdb_vote <= 0:
                results["tmdb"] = "unknown"
            elif self._min_tmdb_count > 0 and tmdb_count < self._min_tmdb_count:
                results["tmdb"] = "count"
            elif tmdb_vote < tmdb_threshold:
                results["tmdb"] = "score"
            else:
                results["tmdb"] = "pass"
        if self.__is_douban_gate_enabled() and douban_threshold > 0:
            if douban_vote <= 0:
                results["douban"] = "unknown"
            elif (
                # 人数为 0 表示未知（RSS 兜底评分），不参与人数下限校验
                douban_count > 0
                and self._min_douban_count > 0
                and douban_count < self._min_douban_count
            ):
                results["douban"] = "count"
            elif douban_vote < douban_threshold:
                results["douban"] = "score"
            else:
                results["douban"] = "pass"

        enabled = list(results.keys())
        if not enabled:
            return None, "", False

        def __state(key: str) -> str:
            return results.get(key, "")

        def __reason(key: str) -> str:
            if key == "douban":
                if __state(key) == "count":
                    return (
                        f"豆瓣评价人数 {douban_count} 低于下限 "
                        f"{self._min_douban_count}"
                    )
                return f"豆瓣评分 {douban_vote:.1f} 低于阈值 {douban_threshold}"
            if __state(key) == "count":
                return f"TMDB票数 {tmdb_count} 低于下限 {self._min_tmdb_count}"
            return f"TMDB评分 {tmdb_vote:.1f} 低于阈值 {tmdb_threshold}"

        def __summary() -> str:
            parts = []
            if "douban" in results:
                if douban_vote <= 0:
                    parts.append("豆瓣未出分")
                elif douban_count > 0:
                    parts.append(f"豆瓣{douban_vote:.1f}分/{douban_count}人")
                else:
                    parts.append(f"豆瓣{douban_vote:.1f}分(人数未知)")
            if "tmdb" in results:
                parts.append(
                    f"TMDB{tmdb_vote:.1f}分/{tmdb_count}票"
                    if tmdb_vote > 0
                    else "TMDB未出分"
                )
            text = "，".join(parts)
            return f"{category_name} | {text}" if category_name else text

        fails = [key for key in enabled if __state(key) in {"score", "count"}]
        passes = [key for key in enabled if __state(key) == "pass"]
        unknowns = [key for key in enabled if __state(key) == "unknown"]

        # 仅因“评价人数/票数不足”被拦的改用可重试状态：新片的人数与票数会随时间增长，
        # 判成“评分不符合”会被历史去重永久跳过，导致这些片子永远错过。
        __fail_status = (
            Status.RATING_PENDING
            if fails and all(__state(key) == "count" for key in fails)
            else Status.RATING_NOT_MATCH
        )

        if self._vote_strategy == "any":
            if passes:
                return None, "", False
            if fails:
                return (
                    __fail_status,
                    f"{__summary()}（{'、'.join(__reason(key) for key in fails)}）",
                    False,
                )
            return None, "", True

        if fails:
            return (
                __fail_status,
                f"{__summary()}（{'、'.join(__reason(key) for key in fails)}）",
                False,
            )
        if unknowns:
            return None, "", True
        return None, "", False

    def __start_task(self):
        """
        运行任务
        """
        # 清理本轮任务的豆瓣缓存
        self._douban_info_cache = {}
        self._douban_star_cache = {}

        if self._migrate_once:
            if self._migrate_from_url and self._migrate_api_token:
                logger.info("开始从原MP迁移配置...")
                __original_config = self.__get_migrate_config()
                if __original_config and isinstance(__original_config, dict):
                    self._enabled = __original_config.get(
                        "enabled", self._enabled
                    )
                    self._cron = __original_config.get("cron", self._cron)
                    self._onlyonce = __original_config.get(
                        "onlyonce", self._onlyonce
                    )
                    self._vote = __original_config.get("vote", self._vote)
                    self._ranks = __original_config.get("ranks", self._ranks)
                    self._rss_addrs = __original_config.get(
                        "rss_addrs", self._rss_addrs
                    ).split("\n")
                    self._clear = __original_config.get("clear", self._clear)
                    self._clear_unrecognized = __original_config.get(
                        "clear_unrecognized", self._clear_unrecognized
                    )
                    self._is_seasons_all = __original_config.get(
                        "is_seasons_all", self._is_seasons_all
                    )
                    self._is_only_movies = __original_config.get(
                        "is_only_movies", self._is_only_movies
                    )
                    self._allow_unrated = __original_config.get(
                        "allow_unrated", self._allow_unrated
                    )
                    self._use_series_rating = __original_config.get(
                        "use_series_rating", self._use_series_rating
                    )
                    self._min_vote_count = self.__safe_int(
                        __original_config.get("min_vote_count", self._min_vote_count),
                        default=self._min_vote_count,
                        minimum=0,
                    )
                    self._title_aliases_raw = str(
                        __original_config.get("title_aliases", self._title_aliases_raw) or ""
                    )
                    self._title_aliases = self.__parse_title_aliases(
                        self._title_aliases_raw
                    )

                    self._release_year = __original_config.get(
                        "release_year", self._release_year
                    )
                    self._min_sleep_time, self._max_sleep_time = map(
                        int,
                        __original_config.get(
                            "sleep_time", self._min_sleep_time
                        ).split(","),
                    )
                    self._history_type = __original_config.get(
                        "history_type", self._history_type
                    )
                    self._is_exit_ip_rate_limit = __original_config.get(
                        "is_exit_ip_rate_limit", self._is_exit_ip_rate_limit
                    )
                else:
                    logger.warn("未获取到原MP配置，结束程序")
                    return

                __original_history = self.__get_migrate_history()
                if __original_history:
                    self.save_data("history", __original_history)
                else:
                    logger.warn("未获取到历史记录，结束程序")
                    return

                # 关闭一次性开关
                self._migrate_once = False
                self.__update_config()
                logger.info("迁移配置和历史完成")
            else:
                logger.error(
                    "迁移配置错误，请检查是否填写了原MP地址和原MP API Token"
                )
                return

        logger.info("开始刷新豆瓣榜单Plus ...")
        addr_list = self._rss_addrs + [
            self._douban_address.get(rank) for rank in self._ranks
        ]
        if not addr_list:
            logger.info("未设置榜单RSS地址")
            return
        else:
            logger.info(f"共 {len(addr_list)} 个榜单RSS地址需要刷新")

        # 读取历史记录
        if self._clearflag:
            history = []  # type: ignore
            self.save_data("history", history)
            # 历史只清理一次
            self._clearflag = False
            logger.info(f"已清理所有 {self.plugin_name} 的历史记录")
        else:
            history = self.get_data("history") or []
            if history and self._clearflag_unrecognized:
                original_length = len(history)
                history = [
                    h
                    for h in history
                    if h.get("status") != Status.UNRECOGNIZED.value
                ]
                deleted_count = original_length - len(history)
                self.save_data("history", history)
                # 未识别历史只清理一次
                self._clearflag_unrecognized = False
                logger.info(
                    f"已清理 {deleted_count} 条 {self.plugin_name} 未识别的历史记录"
                )

        # 只将终态加入去重集合；未识别、待评分、接口失败，以及旧版误判为
        # “评分不符合”的 0 分条目，会在后续任务中重新评估。
        unique_flags = {
            h.get("unique")
            for h in history
            if h is not None
            and not self.__is_retryable_status(h.get("status"))
            and not self.__is_legacy_zero_vote_history(h)
        }

        # 本次运行的统计与新增订阅，用于运行结束后推送通知
        run_status_counter: Dict[str, int] = {}
        run_added_subscriptions: List[Dict[str, Any]] = []
        # 本次扫描全貌：榜单数 / 抓取条数 / 已处理过跳过数 / 抓取失败榜单
        run_scan: Dict[str, Any] = {
            "rank_total": len([__a for __a in addr_list if __a]),
            "rank_ok": 0,
            "rank_failed": [],
            "items": 0,
            "skipped": 0,
        }

        def __record_history(payload: HistoryPayload) -> None:
            """写入历史记录，并统计本次运行结果（供运行结束通知使用）。"""
            history.append(payload)
            __status_text = str(payload.get("status") or "")
            run_status_counter[__status_text] = (
                run_status_counter.get(__status_text, 0) + 1
            )
            if __status_text != Status.SUBSCRIPTION_ADDED.value:
                return
            # 豆瓣评分本轮已缓存，此处不会产生额外请求
            __douban_vote, _douban_count, _has_rating = self.__get_douban_rating(
                payload.get("doubanid"), None
            )
            if not __douban_vote and self._last_douban_vote_used:
                # 豆瓣详情不可用时，用 RSS 兜底评分展示
                __douban_vote = self._last_douban_vote_used
            run_added_subscriptions.append(
                {
                    "title": payload.get("title"),
                    "type": payload.get("type"),
                    "year": payload.get("year"),
                    "doubanid": payload.get("doubanid"),
                    "tmdbid": payload.get("tmdbid"),
                    "tmdb_vote": payload.get("vote"),
                    "douban_vote": __douban_vote,
                    "category": payload.get("category"),
                }
            )

        # count_addr_list = 0
        for addr_index, _addr in enumerate(addr_list):
            # count_addr_list += 1
            # if addr_index == 5 or addr_index == 5:
            #     break

            if not _addr:
                continue
            try:
                logger.info(f"获取RSS：{_addr} ...")
                addr_result = DoubanRankPlus2.__get_info_addr(_addr)
                addr = addr_result.get("addr", None)
                customize_save_paths = addr_result.get(
                    "customize_save_paths", None
                )
                subscription_type = addr_result.get("subscription_type", None)

                logger.debug(f"addr::: {addr}")
                logger.debug(f"customize_save_paths::: {customize_save_paths}")
                logger.debug(f"subscription_type::: {subscription_type}")

                rss_infos = self.__get_rss_info(addr)
                if not rss_infos:
                    logger.error(f"RSS地址：{addr} ，未查询到数据")
                    run_scan["rank_failed"].append(addr)
                    continue
                else:
                    run_scan["rank_ok"] += 1
                    run_scan["items"] += len(rss_infos)
                    logger.info(
                        f"RSS地址：{addr} ，共 {len(rss_infos)} 条数据"
                    )

                for rss_info_index, rss_info in enumerate(rss_infos):
                    if self._event.is_set():
                        logger.info("订阅服务停止")
                        return
                    mtype = None

                    logger.info(
                        f"第 {addr_index + 1}/{len(addr_list)} 条订阅数据处理进度: {rss_info_index + 1}/{len(rss_infos)}"
                    )

                    logger.debug(f"rss_info:::{rss_info}")
                    title = rss_info.get("title")
                    if not title:
                        logger.warn("标题为空，无法处理")
                        continue

                    douban_id = rss_info.get("doubanid")
                    year = rss_info.get("year")
                    type_str = rss_info.get("mtype")
                    force_tv = bool(
                        subscription_type == "tv"
                        or "show_domestic" in str(addr or "")
                    )

                    if type_str == "movie":
                        mtype = MediaType.MOVIE
                    elif type_str:
                        mtype = MediaType.TV
                    if force_tv:
                        mtype = MediaType.TV
                    unique_flag = f"{self.plugin_config_prefix}{title}_{year}_(DB:{douban_id})"
                    logger.debug(f"unique_flag:::{unique_flag}")

                    # 在集合中查找 unique_flag
                    if unique_flag in unique_flags:
                        run_scan["skipped"] += 1
                        logger.info(
                            f"已处理过: Title: {title}, Year:{year}, DBID:{douban_id}"
                        )
                        continue

                    # 豆瓣评分早筛：识别前先按豆瓣评分/评价人数/口碑过滤，
                    # 命中则省去 TMDB 搜索与逐季查询（仅“同时达标/仅豆瓣”策略生效）。
                    (
                        gate_status,
                        gate_vote,
                        _gate_count,
                        gate_reason,
                    ) = self.__check_douban_gate(
                        rss_vote=rss_info.get("douban_vote"),
                        doubanid=douban_id,
                        mtype=mtype,
                    )
                    if gate_status:
                        logger.info(
                            f"豆瓣评分早筛拒绝: Title: {title}, Year:{year}, "
                            f"DBID:{douban_id}, 原因: {gate_reason}"
                        )
                        history_payload = (
                            DoubanRankPlus2.__get_history_rejected_payload(
                                title=title,
                                unique=unique_flag,
                                status=gate_status,
                                vote=gate_vote,
                                year=year,
                                doubanid=douban_id,
                                category=self._last_category_name,
                            )
                        )
                        self.__remove_retryable_history(history, unique_flag)
                        __record_history(history_payload)
                        if self.__is_retryable_status(gate_status.value):
                            unique_flags.discard(unique_flag)
                        else:
                            unique_flags.add(unique_flag)
                        continue

                    logger.info(
                        f"开始处理: Title: {title}, Year:{year}, DBID:{douban_id}, Type:{mtype}"
                    )
                    base_title, parsed_season = self.__parse_season_title(title, year)
                    meta = MetaInfo(base_title)
                    meta.year = year
                    if mtype:
                        meta.type = mtype
                    if parsed_season:
                        meta.type = MediaType.TV
                        meta.begin_season = parsed_season
                    logger.debug(f"增强识别 MetaInfo:::{meta}")

                    mediainfo, should_retry = self.__recognize_rss_media(
                        title=title,
                        year=year,
                        douban_id=douban_id,
                        mtype=mtype,
                        force_tv=force_tv,
                    )

                    if not mediainfo:
                        logger.warn(f"未识别到 {title} 的媒体信息")
                        history_payload = DoubanRankPlus2.__get_history_unrecognized_payload(
                            title,
                            unique_flag,
                            year,
                            douban_id,
                            category=self.__get_effective_thresholds(
                                douban_id, mtype
                            )[2],
                        )
                        if should_retry:
                            history_payload["status"] = Status.RETRY_LATER.value
                        self.__remove_retryable_history(history, unique_flag)
                        __record_history(history_payload)
                        logger.debug(f"已添加到可重试历史：{history_payload}")
                        continue

                    logger.debug(f"{meta}:::{meta}")
                    logger.info(
                        f"已识别到 {title} ({year}) 的媒体信息: {mediainfo.title_year}, 类型: {mediainfo.type}"
                    )

                    if self._is_only_movies and mediainfo.type == MediaType.TV:
                        logger.info(f"仅下载电影，跳过 {mediainfo.title_year}")
                        continue

                    if subscription_type:
                        if (
                            subscription_type == "movies"
                            and mediainfo.type == MediaType.TV
                        ):
                            logger.info(
                                f"仅下载电影，跳过 {mediainfo.title_year}"
                            )
                            continue
                        if (
                            subscription_type == "tv"
                            and mediainfo.type == MediaType.MOVIE
                        ):
                            logger.info(
                                f"仅下载剧集，跳过 {mediainfo.title_year}"
                            )
                            continue

                    # 保存路径
                    save_path = None
                    if customize_save_paths and isinstance(
                        customize_save_paths, dict
                    ):
                        if mediainfo.type == MediaType.TV:
                            save_path = customize_save_paths.get("tv")
                        elif mediainfo.type == MediaType.MOVIE:
                            save_path = customize_save_paths.get("movie")

                    number_of_seasons = mediainfo.number_of_seasons
                    logger.debug(f"number_of_seasons:::{number_of_seasons}")

                    # 已识别状态默认值
                    status = Status.UNCATEGORIZED

                    # 查询缺失的媒体信息
                    is_exist_all, missing_season = self.__check_lib_exists(
                        meta, mediainfo, mediainfo.type == MediaType.MOVIE
                    )

                    logger.debug(
                        f"is_exist_all:::{is_exist_all}, missing_season:::{missing_season}"
                    )

                    # 如果 RSS 未明确季号且开启全季订阅，则轮流处理每一季。
                    # 标题已包含“第N季”时只处理目标季，避免为新一季误订全部旧季。
                    if (
                        self._is_seasons_all
                        and mediainfo.type == MediaType.TV
                        and not meta.begin_season
                        and number_of_seasons
                        and not is_exist_all
                    ):
                        logger.debug(
                            f"meta.begin_season:::{meta.begin_season}"
                        )
                        genre_ids = mediainfo.genre_ids
                        ANIME_GENRE_ID = 16
                        logger.debug(
                            f"{mediainfo.title_year} genre_ids::: {genre_ids}"
                        )
                        if (
                            ANIME_GENRE_ID in genre_ids
                            and customize_save_paths
                            and isinstance(customize_save_paths, dict)
                        ):
                            save_path = customize_save_paths.get("anime")
                            logger.info(
                                f"{mediainfo.title_year} 为动漫类别, 动漫自定义保存路径为: {save_path}"
                            )

                        for i in range(1, number_of_seasons + 1):
                            logger.debug(
                                f"开始添加 {mediainfo.title_year} 第{i}/{number_of_seasons}季订阅"
                            )
                            __status = self.__checke_and_add_subscribe(
                                meta=meta,
                                mediainfo=mediainfo,
                                season=i,
                                save_path=save_path,
                                is_exist_all=is_exist_all,
                                missing_season=missing_season,
                                douban_id=douban_id,
                                rss_douban_vote=rss_info.get("douban_vote"),
                            )
                            if not meta.begin_season or i == meta.begin_season:
                                status = __status
                    else:
                        status = self.__checke_and_add_subscribe(
                            meta=meta,
                            mediainfo=mediainfo,
                            season=meta.begin_season,
                            save_path=save_path,
                            is_exist_all=is_exist_all,
                            missing_season=missing_season,
                            douban_id=douban_id,
                            rss_douban_vote=rss_info.get("douban_vote"),
                        )

                    # 覆盖同一条目的旧可重试记录，避免历史面板不断累积。
                    self.__remove_retryable_history(history, unique_flag)

                    # 存储历史记录
                    history_payload = {
                        "title": title,
                        "type": mediainfo.type.value,
                        "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(),
                        "overview": mediainfo.overview,
                        "tmdbid": str(mediainfo.tmdb_id) or "0",
                        "doubanid": douban_id or "0",
                        "unique": unique_flag,
                        "time": datetime.datetime.now(
                            tz=pytz.timezone(settings.TZ)
                        ).strftime("%m-%d %H:%M"),
                        "time_full": datetime.datetime.now(
                            tz=pytz.timezone(settings.TZ)
                        ).strftime("%Y-%m-%d %H:%M:%S"),
                        "vote": mediainfo.vote_average,
                        "status": status.value,
                        "category": self._last_category_name,
                    }
                    __record_history(history_payload)
                    if self.__is_retryable_status(status.value):
                        unique_flags.discard(unique_flag)
                    else:
                        unique_flags.add(unique_flag)
                    logger.debug(f"已添加到历史：{history_payload}")

            except Exception as e:
                logger.error(f"处理RSS地址：{addr} 出错: {str(e)}")
            finally:
                # 保存历史记录
                logger.info(f"保存榜单 {addr} 处理后的历史记录")

                self.save_data("history", history)

        logger.info("所有榜单RSS刷新完成")

        # 运行结束后推送通知（无论本次是否有新增订阅）
        self.__send_run_notification(
            status_counter=run_status_counter,
            added_subscriptions=run_added_subscriptions,
            scan=run_scan,
        )

    def __send_run_notification(
        self,
        status_counter: Dict[str, int],
        added_subscriptions: List[Dict[str, Any]],
        scan: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        运行结束后通过 MP 通知系统推送本次执行结果。
        无论是否有新增订阅都会发送；可在插件设置页关闭。
        """
        if not self._notify:
            logger.info("未开启运行结果通知，跳过推送")
            return
        try:
            total = sum(status_counter.values())
            added_count = len(added_subscriptions)
            if added_count:
                title = f"豆瓣榜单订阅完成：新增 {added_count} 个订阅"
            else:
                title = "豆瓣榜单订阅完成：无新增订阅"

            lines: List[str] = []
            if scan:
                lines.append(
                    f"本次扫描 {scan.get('rank_total', 0)} 个榜单，"
                    f"抓到 {scan.get('items', 0)} 条，"
                    f"跳过 {scan.get('skipped', 0)} 条（已处理过），"
                    f"处理 {total} 条新条目"
                )
                __failed = list(scan.get("rank_failed") or [])
                if __failed:
                    __names = "、".join(
                        re.sub(r"^https?://[^/]+", "", str(__u)) or str(__u)
                        for __u in __failed
                    )
                    lines.append(f"· 抓取失败 {len(__failed)} 个榜单：{__names}")
            elif total:
                lines.append(f"本次共处理 {total} 条榜单条目")
            for status in Status:
                count = status_counter.get(status.value, 0)
                if count:
                    lines.append(f"· {status.value}：{count}")
            known_status = {status.value for status in Status}
            for status_text, count in status_counter.items():
                if status_text and status_text not in known_status:
                    lines.append(f"· {status_text}：{count}")

            if added_count:
                lines.append("")
                lines.append(f"新增订阅（{added_count}）：")
                for index, item in enumerate(added_subscriptions, start=1):
                    lines.append(
                        f"{index}. {DoubanRankPlus2.__get_notify_subscription_line(item)}"
                    )
            else:
                lines.append("")
                lines.append("本次没有新增订阅。")

            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text="\n".join(lines),
            )
            logger.info(f"已推送运行结果通知：{title}")
        except Exception as err:
            logger.error(f"推送运行结果通知失败：{str(err)}")

    @staticmethod
    def __get_notify_subscription_line(item: Dict[str, Any]) -> str:
        """构造通知中单条新增订阅的描述行。"""
        type_text = str(item.get("type") or "").strip()
        title = str(item.get("title") or "").strip() or "未知"
        year = str(item.get("year") or "").strip()
        name = f"{title} ({year})" if year else title
        category_text = str(item.get("category") or "").strip()
        tag = type_text
        if category_text:
            tag = f"{type_text}·{category_text}" if type_text else category_text
        parts = [f"[{tag}] {name}" if tag else name]

        try:
            douban_vote = float(item.get("douban_vote") or 0)
        except (TypeError, ValueError):
            douban_vote = 0.0
        try:
            tmdb_vote = float(item.get("tmdb_vote") or 0)
        except (TypeError, ValueError):
            tmdb_vote = 0.0
        vote_texts = []
        if douban_vote > 0:
            vote_texts.append(f"豆瓣 {douban_vote:.1f}")
        if tmdb_vote > 0:
            vote_texts.append(f"TMDB {tmdb_vote:.1f}")
        if vote_texts:
            parts.append(" ".join(vote_texts))

        link = DoubanRankPlus2.__get_notify_subscription_link(item)
        if link:
            parts.append(link)
        return " | ".join(parts)

    @staticmethod
    def __get_notify_subscription_link(item: Dict[str, Any]) -> str:
        """构造新增订阅的详情页链接（优先豆瓣）。"""
        doubanid = str(item.get("doubanid") or "").strip()
        if doubanid and doubanid != "0":
            return f"https://movie.douban.com/subject/{doubanid}/"
        tmdbid = str(item.get("tmdbid") or "").strip()
        if tmdbid and tmdbid != "0":
            path = (
                "tv"
                if str(item.get("type") or "") == MediaType.TV.value
                else "movie"
            )
            return f"https://www.themoviedb.org/{path}/{tmdbid}"
        return ""

    def __check_lib_exists(
        self,
        meta: MetaBase,
        mediainfo: MediaInfo,
        is_movie: bool,
    ) -> Tuple[bool, list[int] | None]:
        """
        检查媒体库缺失
        @return: True: 媒体库中已存在 False: 媒体库中不存在; list[int]: 缺失的季
        """
        # 查询缺失的媒体信息
        is_exist_flag, no_exist_details = (
            self.downloadchain.get_no_exists_info(
                meta=meta, mediainfo=mediainfo
            )
        )
        logger.debug(f"is_exist_flag:::{is_exist_flag}")
        logger.debug(f"no_exist_detail:::{no_exist_details}")

        if is_exist_flag:
            logger.info(f"{mediainfo.title_year} 媒体库中已存在")
            return True, None
        else:
            if is_movie:
                return False, None
            else:
                # 检查缺失的季
                __missing_seasons = []
                for _media_id, seasons in no_exist_details.items():
                    for season, _season_details in seasons.items():
                        if season not in __missing_seasons:
                            __missing_seasons.append(season)
                missing_seasons = (
                    __missing_seasons if len(__missing_seasons) > 0 else None
                )
                logger.debug(f"缺失季: {missing_seasons}")
                return missing_seasons is None, missing_seasons

    def __checke_and_add_subscribe(
        self,
        meta: MetaBase,
        mediainfo: MediaInfo,
        season: int | None,
        save_path,
        is_exist_all: bool,
        missing_season: list[int] | None,
        douban_id: str | None = None,
        rss_douban_vote: Any = None,
    ) -> Status:

        if is_exist_all:
            logger.debug(f"{mediainfo.title_year} 媒体库中已存在，跳过订阅")
            return Status.MEDIA_EXISTS
        else:
            if missing_season:
                logger.debug(
                    f"{mediainfo.title_year} 缺失季: {missing_season}，当前尝试添加季：{season}",
                )

            if (
                missing_season
                and season is not None
                and season not in missing_season
            ):
                logger.info(
                    f"{mediainfo.title_year} 第 {season} 季媒体库中已存在，跳过订阅"
                )
                return Status.MEDIA_EXISTS

        if save_path:
            logger.info(
                f"{mediainfo.title_year} 的自定义保存路径为: {save_path}"
            )

        # 判断上映年份是否符合要求；年份缺失时不误杀，交给识别结果和后续订阅处理。
        year_for_filter = (
            meta.year
            if mediainfo.type == MediaType.TV and season and meta.year
            else mediainfo.year
        )
        media_year = self.__safe_int(year_for_filter, default=0, minimum=0)
        if self._release_year and media_year and media_year < self._release_year:
            logger.info(
                f"{mediainfo.title_year} 用于筛选的年份: {year_for_filter}, 不符合要求"
            )
            return Status.YEAR_NOT_MATCH

        # 评分过滤：豆瓣 + TMDB 双评分，按策略（同时达标/任一达标/单源）判定。
        self._last_category_name = None
        # 电影使用作品评分；剧集有明确季号时必须使用当前季评分，不能用整部系列总评分代替。
        if self.__is_tmdb_gate_enabled() or self.__is_douban_gate_enabled():
            tmdb_vote = 0.0
            tmdb_count = 0
            if mediainfo.type == MediaType.TV and season:
                season_vote, season_count, query_success = (
                    self.__get_tv_season_vote(mediainfo.tmdb_id, season)
                )
                if not query_success:
                    return Status.RETRY_LATER
                tmdb_vote = season_vote or 0.0
                tmdb_count = season_count
            else:
                tmdb_vote = self.__safe_float(mediainfo.vote_average)
                tmdb_count = self.__safe_int(
                    getattr(mediainfo, "vote_count", 0), minimum=0
                )

            douban_vote, douban_count, _douban_ok = self.__get_douban_rating(
                douban_id, mediainfo.type
            )
            if not _douban_ok:
                # 豆瓣详情拿不到（无 ID / 未出分 / 限流）时，用 RSS 自带的豆瓣评分兜底；
                # 此时评价人数未知（记为 0），不参与人数下限校验
                __rss_vote = self.__safe_float(rss_douban_vote)
                if 0 < __rss_vote <= 10:
                    douban_vote, douban_count = __rss_vote, 0
                    logger.info(
                        f"{mediainfo.title_year} 豆瓣详情不可用，"
                        f"改用 RSS 豆瓣评分 {__rss_vote:.1f}（评价人数未知）参与判定"
                    )
            self._last_douban_vote_used = douban_vote if douban_vote > 0 else None

            (
                __douban_threshold,
                __tmdb_threshold,
                __category_name,
            ) = self.__get_effective_thresholds(douban_id, mediainfo.type)
            self._last_category_name = __category_name
            if __category_name:
                logger.debug(
                    f"{mediainfo.title_year} 命中分类规则：{__category_name}"
                    f"（豆瓣阈值 {__douban_threshold}、TMDB阈值 {__tmdb_threshold}）"
                )

            gate_status, gate_reason, need_unrated = (
                self.__evaluate_rating_gate(
                    tmdb_vote=tmdb_vote,
                    tmdb_count=tmdb_count,
                    douban_vote=douban_vote,
                    douban_count=douban_count,
                    douban_threshold=__douban_threshold,
                    tmdb_threshold=__tmdb_threshold,
                    category_name=__category_name,
                )
            )
            if gate_status:
                logger.info(
                    f"{mediainfo.title_year}"
                    f"{' 第' + str(season) + '季' if season else ''}"
                    f"评分不符合要求：{gate_reason}"
                )
                return gate_status

            # 存在未出分的评分源时，沿用未出分策略（前作/往季平均分）
            if need_unrated:
                unrated_status = self.__check_unrated_policy(
                    mediainfo=mediainfo, season=season
                )
                if unrated_status:
                    return unrated_status

        # 查询缺失的媒体信息
        # exist_flag, _exist_details = self.downloadchain.get_no_exists_info(
        #     meta=meta, mediainfo=mediainfo
        # )

        # if exist_flag:
        #     logger.info(f"{mediainfo.title_year} 媒体库中已存在")
        #     return Status.MEDIA_EXISTS

        # 用户曾手动取消本插件创建的同一订阅时，永久跳过自动重订。
        if self.__is_subscription_cancelled(mediainfo, season):
            logger.info(
                f"{mediainfo.title_year}"
                f"{' 第' + str(season) + '季' if season else ''} 已手动取消过，跳过自动订阅"
            )
            return Status.SUBSCRIPTION_CANCELLED

        # 判断用户是否已经添加订阅
        if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
            logger.info(f"{mediainfo.title_year} 订阅已存在")
            return Status.SUBSCRIPTION_EXISTS

        # 添加订阅。MoviePilot V2 返回 (订阅ID, 消息)；兼容旧版无返回值。
        add_result = self.subscribechain.add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=season,
            exist_ok=True,
            username=self.plugin_name,
            save_path=save_path,
        )
        subscribe_id = None
        add_message = ""
        if isinstance(add_result, tuple):
            subscribe_id = add_result[0] if len(add_result) > 0 else None
            add_message = str(add_result[1] or "") if len(add_result) > 1 else ""
        elif isinstance(add_result, int):
            subscribe_id = add_result

        if isinstance(add_result, tuple) and not subscribe_id:
            logger.error(
                f"添加订阅失败: {mediainfo.title_year}"
                f"{' 第' + str(season) + '季' if season else ''} {add_message}"
            )
            return Status.RETRY_LATER

        self.__record_managed_subscription(
            subscribe_id=subscribe_id,
            mediainfo=mediainfo,
            season=season,
        )
        if season:
            logger.info(f"已添加订阅: {mediainfo.title_year} 第 {season} 季")
        else:
            logger.info(f"已添加订阅: {mediainfo.title_year} ")
        return Status.SUBSCRIPTION_ADDED

    def __get_rss_info(self, addr) -> List[RssInfo]:
        """
        获取RSS
        """
        try:
            # RSSHub 偶尔读超时：缩短单次超时并重试一次，避免整个榜单被静默丢掉
            ret = None
            for __attempt in range(2):
                try:
                    if self._proxy:
                        ret = RequestUtils(
                            timeout=90, proxies=settings.PROXY or {}
                        ).get_res(addr)
                    else:
                        ret = RequestUtils(timeout=90).get_res(addr)
                except Exception as __fetch_err:
                    logger.warn(
                        f"获取RSS异常（第 {__attempt + 1} 次）：{addr}，{__fetch_err}"
                    )
                    ret = None
                if ret:
                    break
                if __attempt == 0:
                    logger.info(f"RSS 抓取失败，3 秒后重试一次：{addr}")
                    time.sleep(3)
            if not ret:
                return []
            ret_xml = ret.text
            ret_array: List[RssInfo] = []

            # 解析XML
            dom_tree = xml.dom.minidom.parseString(ret_xml)
            rootNode = dom_tree.documentElement
            if rootNode is None:
                return []
            items = rootNode.getElementsByTagName("item")
            for item in items:
                try:
                    # 标题
                    title = DomUtils.tag_value(item, "title", default="")
                    # 链接
                    link = DomUtils.tag_value(item, "link", default="")
                    if not title and not link:
                        logger.warn("条目标题和链接均为空，无法处理")
                        continue

                    # 豆瓣ID：兼容 /subject/<id>/、/subject/<id> 以及
                    # /doubanapp/dispatch/movie/<id>（结尾无斜杠，旧正则解析不出）等格式
                    found_doubanid = re.findall(
                        r"/(?:subject|movie|tv)/(\d+)", str(link) or ""
                    )
                    if not found_doubanid:
                        found_doubanid = re.findall(
                            r"/(\d{5,})(?:[/?#]|$)", str(link) or ""
                        )
                    if found_doubanid:
                        doubanid = found_doubanid[0]
                        if not str(doubanid).isdigit():
                            logger.warn(f"解析的豆瓣ID格式不正确：{doubanid}")
                            continue
                    else:
                        doubanid = None

                    # 描述：年份与豆瓣评分共用
                    description = DomUtils.tag_value(
                        item, "description", default=""
                    )

                    # 豆瓣评分：RSS 描述通常形如 “评分：7.3分”，豆列等榜单可能没有
                    douban_vote = None
                    found_rating = re.findall(
                        r"评分[：:]\s*([0-9]+(?:\.[0-9]+)?)",
                        str(description) or "",
                    )
                    if found_rating:
                        douban_vote = self.__safe_float(found_rating[0])
                        if not 0 < douban_vote <= 10:
                            douban_vote = None

                    # 年份
                    year = DomUtils.tag_value(item, "year", default="")
                    if not year:
                        # 删除 '评价数' 到第一个 '<br>' 之间的字符串
                        year_description = re.sub(
                            r"评价数.*?<br>", "", str(description) or ""
                        )
                        # 删除所有 <img> 标签及其内容
                        year_description = re.sub(
                            r"<img.*?>", "", year_description
                        )
                        # 匹配4位独立数字1900-2099年
                        found_year = re.findall(
                            r"\b(19\d{2}|20\d{2})\b", year_description
                        )
                        year = found_year[0] if found_year else None

                    # 类型
                    mtype = DomUtils.tag_value(item, "type", default="")

                    rss_info: RssInfo = {
                        "title": str(title),
                        "link": str(link),
                        "mtype": str(mtype),
                        "year": str(year) if year else None,
                        "doubanid": str(doubanid) if doubanid else None,
                        "douban_vote": douban_vote,
                    }
                    # 返回对象
                    ret_array.append(rss_info)

                except Exception as e1:
                    logger.error("解析RSS条目失败：" + str(e1))
                    continue
            return ret_array
        except Exception as e:
            logger.error("获取RSS失败：" + str(e))
            return []

    @staticmethod
    def __get_info_addr(
        addr: str,
    ) -> Dict[str, Dict[str, str] | str | None]:
        # ) -> Dict[str, Dict[str, str] | str | None]:
        subscription_type = None

        # 提取分号分割的链接和保存地址
        if ";" not in addr:
            return {
                "addr": addr,
                "customize_save_paths": None,
                "subscription_type": None,
            }
        else:
            logger.debug("分割订阅地址")
            str_list: List[str] = addr.split(";")
            addr = str_list[0]
            customize_save_info = str_list[1] if len(str_list) > 1 else ""
            if len(str_list) > 2:
                subscription_type = str_list[2]

            logger.debug(f"addr: {addr}")
            logger.debug(f"customize_save_info: {customize_save_info}")
            logger.debug(f"subscription_type: {subscription_type}")

            if "#" in customize_save_info:
                customize_save_info_list = customize_save_info.split("#")

                logger.debug(
                    f"customize_save_info_list: {customize_save_info_list}"
                )

                customize_save_path_movie = customize_save_info_list[0]
                customize_save_path_tv = customize_save_info_list[1]
                customize_save_path_anime = (
                    customize_save_info_list[2]
                    if len(customize_save_info_list) > 2
                    else customize_save_path_tv
                )

                logger.debug(
                    f"订阅链接 {addr} 的自定义保存路径为: "
                    f"电影:{customize_save_path_movie}, "
                    f"电视剧: {customize_save_path_tv}, "
                    f"动漫: {customize_save_path_anime}"
                )

            else:
                customize_save_path_movie = customize_save_info
                customize_save_path_tv = customize_save_info
                customize_save_path_anime = customize_save_info

                logger.debug(
                    f"订阅链接 {addr} 的自定义保存路径为: {customize_save_info}"
                )

            if (
                subscription_type
                and subscription_type.startswith("@")
                and subscription_type.endswith("@")
            ):
                subscription_type = subscription_type.strip("@")
                logger.info(
                    f"订阅链接 {addr} 的订阅类型为: {subscription_type}"
                )

            customize_save_paths = {
                "movie": customize_save_path_movie,
                "tv": customize_save_path_tv,
                "anime": customize_save_path_anime,
            }
            return {
                "addr": addr,
                "customize_save_paths": customize_save_paths,
                "subscription_type": subscription_type,
            }

    @staticmethod
    def __get_history_unrecognized_payload(
        title: str,
        unique: str,
        year: str | None = None,
        doubanid: str | None = None,
        category: Optional[str] = None,
    ) -> HistoryPayload:
        """
        获取历史记录
        """
        history_payload: HistoryPayload = {
            "title": title,
            "unique": unique,
            "status": Status.UNRECOGNIZED.value,
            "type": MediaType.UNKNOWN.value,
            "year": year or "0",
            "poster": "/assets/no-image-CweBJ8Ee.jpeg",
            "overview": "",
            "tmdbid": "0",
            "doubanid": doubanid or "0",
            "time": datetime.datetime.now(
                tz=pytz.timezone(settings.TZ)
            ).strftime("%m-%d %H:%M"),
            "time_full": datetime.datetime.now(
                tz=pytz.timezone(settings.TZ)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "vote": 0.0,
            "category": category,
        }
        return history_payload

    @staticmethod
    def __get_history_rejected_payload(
        title: str,
        unique: str,
        status: Status,
        vote: float,
        year: str | None = None,
        doubanid: str | None = None,
        category: Optional[str] = None,
    ) -> HistoryPayload:
        """识别前被评分/口碑过滤拒绝的历史记录（尚无 TMDB 信息）。"""
        history_payload = DoubanRankPlus2.__get_history_unrecognized_payload(
            title=title,
            unique=unique,
            year=year,
            doubanid=doubanid,
            category=category,
        )
        history_payload["status"] = status.value
        history_payload["vote"] = vote
        return history_payload

    def __get_tmdbinfo_by_doubanid(
        self, doubanid: str, mtype: MediaType | None = None
    ) -> Tuple[dict[str, Any] | None, bool]:
        """
        根据豆瓣ID获取TMDB信息
        """
        doubaninfo, is_ip_rate_limit = self.__douban_info(
            doubanid=doubanid, mtype=mtype
        )
        if is_ip_rate_limit or not doubaninfo:
            return None, is_ip_rate_limit

        # 优先使用title匹配, original_title无法识别到季数
        title = doubaninfo.get("title", "")
        original_title = doubaninfo.get("original_title", "")
        # meta = MetaInfo(title=original_title if original_title else title)
        meta = MetaInfo(title=title if title else original_title)

        logger.debug(f"MetaInfo meta from original_title or title:::{meta}")

        # 年份
        meta.year = doubaninfo.get("year")

        # 处理类型
        media_type = doubaninfo.get("media_type")
        media_type = (
            media_type
            if isinstance(media_type, MediaType)
            else (
                MediaType.MOVIE
                if doubaninfo.get("type") == "movie"
                else MediaType.TV
            )
        )
        meta.type = media_type

        # 匹配TMDB信息
        if original_title:
            meta_names = list(
                dict.fromkeys(
                    [original_title, title, meta.cn_name, meta.en_name]
                )
            )
        else:
            meta_names = list(
                dict.fromkeys([title, meta.cn_name, meta.en_name])
            )

        # 移除空值
        meta_names = [name for name in meta_names if name]

        __mtype = mtype if mtype and mtype != MediaType.UNKNOWN else meta.type
        __begin_season = meta.begin_season if meta.begin_season else None
        __is_match_season_from_name = False

        for name in meta_names:
            if __is_match_season_from_name:
                # 如果已经从名字匹配到季数，则直接修正名字
                name = re.sub(
                    r"\d+$", "", name
                ).strip()  # 将匹配到的数字从 name 中移除，并去掉多余的空格
            elif __mtype == MediaType.TV and not __begin_season:
                # 如果季为空且是电视剧，则匹配获取 name 以数字结束的内容作为季数
                __matchSeason = re.search(r"\d+$", name)
                if __matchSeason:
                    __begin_season = int(
                        __matchSeason.group()
                    )  # 提取匹配内容并转换为 int
                    name = re.sub(
                        r"\d+$", "", name
                    ).strip()  # 将匹配到的数字从 name 中移除，并去掉多余的空格
                    __is_match_season_from_name = True
                    logger.debug("从名字匹配到季数：%s", __begin_season)

            logger.debug(f"match_tmdbinfo name:::{name}")
            logger.debug(f"match_tmdbinfo mtype:::{__mtype}")
            logger.debug(f"match_tmdbinfo meta.year:::{meta.year}")
            logger.debug(f"match_tmdbinfo begin_season:::{__begin_season}")
            tmdbinfo = self.mediachain.match_tmdbinfo(
                name=name,
                year=meta.year,
                mtype=__mtype,
                season=__begin_season,
            )
            # logger.debug(f"tmdbinfo:::{tmdbinfo}")

            if tmdbinfo:
                # 合季季后返回
                tmdbinfo["season"] = meta.begin_season
                return tmdbinfo, is_ip_rate_limit

        return None, is_ip_rate_limit

    def __douban_info(
        self, doubanid: str, mtype: MediaType | None = None
    ) -> Tuple[dict[str, Any] | None, bool]:
        """
        获取豆瓣信息
        :param doubanid: 豆瓣ID
        :param mtype:    媒体类型
        :return: 豆瓣信息
        """
        """
        豆瓣IP速率限制错误信息
        {'msg': 'subject_ip_rate_limit','code': 1309, 'request': 'GET /v2/movie/30483637','localized_message': '您所在的网络存在异常，请登录后重试。'}
        """

        def __douban_tv() -> Tuple[dict[str, Any] | None, bool]:
            """
            获取豆瓣剧集信息
            """
            info = self.doubanapi.tv_detail(doubanid)
            if info:
                if "subject_ip_rate_limit" in info.get("msg", ""):
                    logger.warn(f"触发豆瓣IP速率限制，错误信息：{info} ...")
                    return None, True
            return info, False

        def __douban_movie() -> Tuple[dict[str, Any] | None, bool]:
            """
            获取豆瓣电影信息
            """
            info = self.doubanapi.movie_detail(doubanid)
            if info:
                if "subject_ip_rate_limit" in info.get("msg", ""):
                    logger.warn(f"触发豆瓣IP速率限制，错误信息：{info} ...")
                    return None, True
            return info, False

        if not doubanid:
            return None, False

        # 本轮任务内缓存，早筛与订阅判定会各取一次，避免重复请求
        cache_key = str(doubanid)
        cached_info = self._douban_info_cache.get(cache_key)
        if cached_info is not None:
            return cached_info, False

        logger.info(f"开始获取豆瓣信息：{doubanid} ...")
        if mtype == MediaType.TV:
            info, is_ip_rate_limit = __douban_tv()
        else:
            movie_info, is_ip_rate_limit = __douban_movie()
            if not movie_info and not is_ip_rate_limit:
                logger.debug("未从电影类型获取到信息，返回从剧集获取信息")
                info, is_ip_rate_limit = __douban_tv()
            else:
                info, is_ip_rate_limit = movie_info, is_ip_rate_limit

        if isinstance(info, dict) and info and not is_ip_rate_limit:
            self._douban_info_cache[cache_key] = info
        return info, is_ip_rate_limit

    def __get_migrate_info(self, migrate_url: str):
        """
        从原MP API URL获取信息
        """
        logger.info(f"开始从原MP获取数据，【请求URL】：{migrate_url}")

        try:
            res = RequestUtils().request(method="get", url=migrate_url)
            if not res:
                logger.error(
                    "没有获取到原MP信息，检查原MP地址和API Token是否正确，检查浏览器打开【请求URL】查看是能获取到数据"
                )
                if self._migrate_once:
                    logger.error(
                        f"{self._msg_migrate_install}。{self._msg_install}"
                    )
                return None
            res.raise_for_status()  # 检查响应状态码，如果不是 2xx，会抛出 HTTPError 异常
            resData = res.json()

            if isinstance(resData, dict):
                if resData.get("success", "") is False:
                    logger.error(
                        f"获取原MP信息失败：{resData.get('message', '')}"
                    )
                    return None

                if resData.get("detail", "") == "Not Found":
                    logger.error("请检查【请求URL】是否能获取到数据")
                    if self._migrate_once:
                        logger.error(
                            f"{self._msg_migrate_install}。{self._msg_install}"
                        )
                    return None

            if isinstance(resData, list) and len(resData) == 0:
                logger.info(f"没有需要添加的迁移信息：{resData}")
                return None

            return resData
        except requests.exceptions.RequestException as err:
            logger.error(f"请求错误发生: {err}")  # 打印所有请求错误
        return None

    def __get_migrate_plugin_api_url(self, endpoint: str) -> str:
        """
        获取插件API URL
        """
        return f"{self._migrate_from_url}/api/v1/plugin/{self._plugin_id}/{endpoint}?migrate_api_token={self._migrate_api_token}"

    def __get_migrate_history(self):
        """
        获取所有迁移历史记录
        """
        url = self.__get_migrate_plugin_api_url("migrate-history")
        return self.__get_migrate_info(url)

    def __get_migrate_config(self):
        """
        获取所有迁移配置
        """
        url = self.__get_migrate_plugin_api_url("migrate-config")
        return self.__get_migrate_info(url)
