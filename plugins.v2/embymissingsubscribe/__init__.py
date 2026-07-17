import threading
import re
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Any, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType, MediaType

lock = threading.Lock()


class EmbyMissingSubscribe(_PluginBase):
    """扫描 Emby 媒体库中的遗漏剧集和电影合集，自动添加 MoviePilot 订阅"""

    # 插件名称
    plugin_name = "Emby 缺失订阅（自用）"
    # 插件描述
    plugin_desc = "扫描 Emby 媒体库中的遗漏剧集和电影合集（BoxSet），自动订阅缺失内容并全自动清理历史整理记录"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/justzerock/MoviePilot-Plugins/main/icons/emby.png"
    # 插件版本
    plugin_version = "1.1.13"
    # 插件作者
    plugin_author = "yuwancumian"
    # 作者主页
    author_url = "https://github.com/yuwancumian2009/MoviePilot-Plugins"
    # 插件配置项 ID 前缀
    plugin_config_prefix = "embymissingsubscribe_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled: bool = False
    _notify: bool = True
    _onlyonce: bool = False
    _cron: str = ""
    _skip_future: bool = True
    _enable_episodes: bool = True
    _enable_collections: bool = False
    _skip_entire_missing: bool = True

    # 运行时
    mediaserver_helper = None

    def init_plugin(self, config: dict = None):
        self._event = threading.Event()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._mediaservers: list = []
        self._libraries: list = []
        self._all_libraries: list = []
        self.mediaserver_helper = MediaServerHelper()
        self._media_chain = MediaChain()
        self._subscribe_chain = SubscribeChain()

        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", True)
            self._onlyonce = config.get("onlyonce", False)
            self._cron = config.get("cron") or ""
            self._mediaservers = config.get("mediaservers") or []
            self._libraries = config.get("libraries") or []
            self._skip_future = config.get("skip_future", True)
            self._enable_episodes = config.get("enable_episodes", True)
            self._enable_collections = config.get("enable_collections", False)
            self._skip_entire_missing = config.get("skip_entire_missing", True)

        # 构建媒体库列表（供表单选择用）
        if self._mediaservers:
            self._all_libraries = self._build_library_list()

        self.stop_service()

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("Emby 缺失订阅服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.scan_missing,
                trigger="date",
                run_date=datetime.now(
                    tz=pytz.timezone(settings.TZ)
                ) + timedelta(seconds=3),
            )
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": False,
                "cron": self._cron,
                "mediaservers": self._mediaservers,
                "libraries": self._libraries,
                "skip_future": self._skip_future,
                "enable_episodes": self._enable_episodes,
                "enable_collections": self._enable_collections,
                "skip_entire_missing": self._skip_entire_missing,
            })
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return True if self._enabled and self._cron and self._mediaservers else False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/emby_missing",
                "event": EventType.PluginAction,
                "desc": "立即扫描 Emby 遗漏剧集和合集并订阅",
                "category": "Emby",
                "data": {"action": "emby_missing_subscribe"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        if self.get_state():
            return [
                {
                    "id": "EmbyMissingSubscribe",
                    "name": "Emby 缺失订阅",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.scan_missing,
                    "kwargs": {},
                }
            ]
        return []

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        """
        处理远程命令
        """
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "emby_missing_subscribe":
                return
        logger.info("收到远程命令，立即执行 Emby 缺失扫描")
        self.scan_missing()

    # ================================================================
    # 核心扫描逻辑（统一入口）
    # ================================================================

    def scan_missing(self):
        """
        入口：遍历所有已配置 of Emby 服务器，按开关执行遗漏剧集和合集扫描
        """
        with lock:
            if not self._mediaservers:
                logger.warning("未配置媒体服务器")
                return

            if not self._enable_episodes and not self._enable_collections:
                logger.warning("遗漏剧集和合集订阅均未启用，跳过扫描")
                return

            services = self.mediaserver_helper.get_services(
                name_filters=self._mediaservers
            )
            if not services:
                logger.warning("获取媒体服务器实例失败，请检查配置")
                return

            # 加载历史记录（避免重复处理）
            history: dict = self.get_data("history") or {}
            total_added: List[str] = []

            for server_name, service in services.items():
                if service.instance.is_inactive():
                    logger.warning(f"媒体服务器 {server_name} 未连接，跳过")
                    continue
                if service.type != "emby":
                    logger.warning(
                        f"媒体服务器 {server_name} 不是 Emby 类型，跳过"
                    )
                    continue

                # —— 遗漏剧集扫描 ——
                if self._enable_episodes:
                    try:
                        added = self._scan_server_episodes(
                            server_name, service, history
                        )
                        total_added.extend(added)
                    except Exception as e:
                        logger.error(
                            f"扫描媒体服务器 {server_name} 遗漏剧集时出错: {e}"
                        )

                # —— 合集缺失电影扫描 ——
                if self._enable_collections:
                    try:
                        added = self._scan_server_collections(
                            server_name, service, history
                        )
                        total_added.extend(added)
                    except Exception as e:
                        logger.error(
                            f"扫描媒体服务器 {server_name} 合集时出错: {e}"
                        )

            # 持久化历史
            self.save_data("history", history)

            # 发送通知
            if self._notify and total_added:
                text_lines = [f"新增 {len(total_added)} 个订阅："]
                for item in total_added[:10]:
                    text_lines.append(f"· {item}")
                if len(total_added) > 10:
                    text_lines.append(f"... 等共 {len(total_added)} 个")
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【Emby 缺失订阅】",
                    text="\n".join(text_lines),
                )

            if total_added:
                logger.info(
                    f"Emby 缺失扫描完成，新增 {len(total_added)} 个订阅"
                )
            else:
                logger.info("Emby 缺失扫描完成，无新增订阅")

    # ================================================================
    # 遗漏剧集扫描
    # ================================================================

    def _scan_server_episodes(
        self, server_name: str, service, history: dict
    ) -> List[str]:
        """
        扫描单个 Emby 服务器 of 遗漏剧集，返回本次新增订阅的描述列表
        """
        added_list: List[str] = []

        user_id = service.instance.user
        if not user_id:
            logger.warning(f"[{server_name}] 无法获取 Emby 用户 ID")
            return added_list

        library_ids = self._get_scan_library_ids(server_name)
        if not library_ids:
            library_ids = [None]

        for library_id in library_ids:
            try:
                items = self._fetch_missing_episodes(
                    service, user_id, library_id
                )
                if not items:
                    continue

                groups = self._group_by_series_season(items)

                for (series_id, season), episodes in groups.items():
                    series_name = episodes[0].get("SeriesName", "未知")

                    # 【安全拦截关卡一】：整季均未下载过滤
                    if self._skip_entire_missing and not self._has_existing_episodes(service, user_id, series_id, season):
                        logger.info(
                            f"[{server_name}] 剧集 {series_name} S{season:02d} "
                            f"在 Emby 中无任何现有物理剧集（整季未下载），根据插件设置跳过自动订阅"
                        )
                        continue

                    # 【安全拦截关卡二】：历史缓存去重拦截（如果已经订阅过该季，直接拦截挂起，不重复擦除整理历史记录）
                    history_key = f"{server_name}:{series_id}:S{season}"
                    if history_key in history:
                        logger.debug(
                            f"[{server_name}] 已经处理并订阅过该剧季，下载未完成前放行跳过，不重复擦除历史记录: {history_key}"
                        )
                        continue

                    ep_numbers = sorted(
                        ep.get("IndexNumber", 0) for ep in episodes
                    )

                    tmdb_id = self._resolve_tmdb_id(
                        service, user_id, series_id, series_name
                    )
                    if not tmdb_id:
                        logger.warning(
                            f"[{server_name}] 无法获取 TMDB ID: "
                            f"{series_name} S{season:02d}，跳过"
                        )
                        continue

                    # 【确定执行订阅的唯一瞬间】：严格卡在通过上面所有拦截关卡、确定要在这一刻发起新加补全订阅时，才精准定点清除这几集的历史记录！
                    for ep in episodes:
                        ep_num = ep.get("IndexNumber")
                        if ep_num is not None:
                            self._delete_transfer_history(
                                title=series_name,
                                mtype=MediaType.TV,
                                season=season,
                                episode=int(ep_num),
                                tmdb_id=tmdb_id
                            )

                    # 获取年份
                    year = str(episodes[0].get("ProductionYear", ""))

                    # 创建订阅
                    sub_id, msg = self._subscribe_chain.add(
                        title=series_name,
                        year=year,
                        mtype=MediaType.TV,
                        tmdbid=tmdb_id,
                        season=season,
                        exist_ok=True,
                        username="Emby 遗漏集",
                    )

                    if sub_id:
                        desc = (
                            f"{series_name} S{season:02d} "
                            f"(TMDB:{tmdb_id}, 遗漏 {len(episodes)} 集)"
                        )
                        added_list.append(desc)
                        logger.info(f"[{server_name}] 订阅成功: {desc}")
                    else:
                        logger.info(
                            f"[{server_name}] 订阅跳过: "
                            f"{series_name} S{season:02d} - {msg}"
                        )

                    # 无论成功与否都记录历史，避免反复重试
                    history[history_key] = {
                        "type": "episode",
                        "series_name": series_name,
                        "season": season,
                        "tmdb_id": tmdb_id,
                        "episodes": ep_numbers,
                        "subscribe_id": sub_id,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }

            except Exception as e:
                logger.error(
                    f"[{server_name}] 扫描媒体库 {library_id} 遗漏剧集时出错: {e}"
                )

        return added_list

    # ================================================================
    # 合集缺失电影扫描
    # ================================================================

    def _scan_server_collections(
        self, server_name: str, service, history: dict
    ) -> List[str]:
        """
        扫描单个 Emby 服务器的电影合集（BoxSet），返回本次新增订阅的描述列表
        """
        added_list: List[str] = []

        user_id = service.instance.user
        if not user_id:
            logger.warning(f"[{server_name}] 无法获取 Emby 用户 ID")
            return added_list

        library_ids = self._get_scan_library_ids(server_name)
        if not library_ids:
            library_ids = [None]

        for library_id in library_ids:
            try:
                boxsets = self._fetch_boxsets(service, user_id, library_id)
                if not boxsets:
                    continue

                for boxset in boxsets:
                    added = self._process_boxset(
                        server_name, service, user_id, boxset, history
                    )
                    added_list.extend(added)

            except Exception as e:
                logger.error(
                    f"[{server_name}] 扫描媒体库 {library_id} 合集时出错: {e}"
                )

        return added_list

    def _process_boxset(
        self,
        server_name: str,
        service,
        user_id: str,
        boxset: dict,
        history: dict,
    ) -> List[str]:
        """
        处理单个 BoxSet：查询 TMDB 合集，对比已有电影，订阅缺失部分
        """
        added_list: List[str] = []
        boxset_id = boxset.get("Id")
        boxset_name = boxset.get("Name", "未知合集")

        collection_id = self._resolve_collection_id(
            service, user_id, boxset
        )
        if not collection_id:
            logger.debug(
                f"[{server_name}] 合集 {boxset_name} "
                f"无法获取 TMDB 合集 ID，跳过"
            )
            return added_list

        tmdb_movies = TmdbChain().tmdb_collection(
            collection_id=collection_id
        )
        if not tmdb_movies:
            logger.debug(
                f"[{server_name}] TMDB 合集 {collection_id} "
                f"({boxset_name}) 无电影信息"
            )
            return added_list

        logger.info(
            f"[{server_name}] 合集 {boxset_name} "
            f"(TMDB:{collection_id}) 共 {len(tmdb_movies)} 部电影"
        )

        existing_tmdb_ids = self._get_boxset_movie_tmdb_ids(
            service, user_id, boxset_id
        )
        logger.debug(
            f"[{server_name}] 合集 {boxset_name} "
            f"已有 {len(existing_tmdb_ids)} 部电影"
        )

        for movie in tmdb_movies:
            if not movie or not movie.tmdb_id:
                continue

            if movie.tmdb_id in existing_tmdb_ids:
                continue

            # 【电影拦截关卡】如果本插件已经为该缺失电影创建过订阅，直接放行，绝不重复触发历史记录擦除
            history_key = (
                f"{server_name}:collection:{collection_id}"
                f":movie:{movie.tmdb_id}"
            )
            if history_key in history:
                logger.debug(
                    f"[{server_name}] 已处理过该电影合集订阅，放行跳过不处理: "
                    f"{movie.title} (TMDB:{movie.tmdb_id})"
                )
                continue

            # 在真正发起电影新订阅前，进行唯一的清除
            self._delete_transfer_history(
                title=movie.title,
                mtype=MediaType.MOVIE,
                tmdb_id=movie.tmdb_id
            )

            # 创建订阅
            sid, msg = SubscribeChain().add(
                title=movie.title,
                year=movie.year,
                mtype=MediaType.MOVIE,
                tmdbid=movie.tmdb_id,
                exist_ok=True,
                username="Emby 合集订阅",
                message=False,
            )

            if sid:
                desc = (
                    f"{movie.title} ({movie.year}) "
                    f"(TMDB:{movie.tmdb_id}, 合集: {boxset_name})"
                )
                added_list.append(desc)
                logger.info(f"[{server_name}] 订阅成功: {desc}")
            else:
                logger.info(
                    f"[{server_name}] 订阅跳过: "
                    f"{movie.title} ({movie.year}) - {msg}"
                )

            # 无论成功与否都记录历史，避免反复重试
            history[history_key] = {
                "type": "collection",
                "collection_name": boxset_name,
                "collection_id": collection_id,
                "movie_title": movie.title,
                "movie_year": movie.year,
                "tmdb_id": movie.tmdb_id,
                "subscribe_id": sid,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        return added_list

    # ================================================================
    # Emby API & MP DB 交互
    # ================================================================

    def _delete_transfer_history(
        self,
        title: str,
        mtype: MediaType,
        season: Optional[int] = None,
        episode: Optional[int] = None,
        tmdb_id: Optional[int] = None
    ):
        """
        利用原生 SQLAlchemy 会话，执行 LIKE 模糊前缀粗筛，完美突破多平台起名年份后缀差异，
        并在 Python 内存中进行跨版本、高密度的路径级特征识别和出栈销毁
        """
        from app.db import get_db

        # 自动寻找 MoviePilot 架构迁移下的真实实体关系映射类
        TransferHistory = None
        for path in [
            "app.db.models.transfer_history",
            "app.db.models.transfer",
            "app.db.models.history",
            "app.db.models"
        ]:
            try:
                mod = __import__(path, fromlist=["TransferHistory"])
                TransferHistory = getattr(mod, "TransferHistory", None)
                if TransferHistory:
                    break
            except ImportError:
                continue

        if not TransferHistory:
            logger.error("【Emby缺失订阅】无法在 MoviePilot 核心依赖中定位到 TransferHistory 实体类，清理历史跳过。")
            return

        # 唤醒原生普通生成器 get_db()
        db_gen = get_db()
        db = next(db_gen)

        try:
            # 去除标题中可能含有的 (2026) 等年份后缀
            title_no_year = re.sub(r"\s*\(\d{4}\)$", "", title).strip()

            # ——【核心修正】：SQL 层彻底砍掉任何针对 type 的过滤条件，仅按标题 LIKE 粗筛，100% 避开数据库中存储的是 'tv' 还是 '电视剧' 导致的 0 行返回问题 ——
            query = db.query(TransferHistory).filter(
                TransferHistory.title.like(f"%{title_no_year}%")
            )

            matched_records = query.all() or []
            deleted_count = 0

            # 开启高效探测，记录初筛从数据库捞出的总数量
            logger.debug(f"【Emby缺失订阅】清理历史探测：{title} 粗筛从数据库捞出 {len(matched_records)} 条候选记录。")

            for rec in matched_records:
                if not rec:
                    continue

                # ——【Python 内存层：智能媒体类型包容匹配】——
                h_type = str(getattr(rec, "type", "") or getattr(rec, "media_type", "") or "").lower()
                m_type = "tv" if mtype == MediaType.TV else "movie"
                if m_type == "tv":
                    if h_type and not any(x in h_type for x in ["tv", "剧", "动漫", "动画", "television"]):
                        continue
                else:
                    if h_type and not any(x in h_type for x in ["movie", "电影"]):
                        continue

                # 交叉精准校验拦截（若双方均有 TMDB 字段，不相符则坚决不误杀）
                h_tmdb = getattr(rec, "tmdb_id", None) or getattr(rec, "tmdbid", None)
                if h_tmdb and tmdb_id:
                    try:
                        if int(h_tmdb) != int(tmdb_id):
                            continue
                    except (ValueError, TypeError):
                        pass

                # 对于电视剧/综艺，由于底层表确无季、集整型字段，我们深度解析路径文本特征进行最终的精确定点锁定
                if mtype == MediaType.TV and season is not None and episode is not None:
                    dest_val = getattr(rec, "dest", "") or getattr(rec, "dest_path", "") or ""
                    src_val = getattr(rec, "src", "") or getattr(rec, "src_path", "") or ""
                    dest_path = str(dest_val).upper()
                    src_path = str(src_val).upper()
                    path_combined = f"{dest_path} {src_path}"

                    match_flag = False
                    
                    # 特征组一：标准各种组合 S01E07 / S1E7 / S01.E07 / S1.E7
                    for s_pattern in [f"S{season:02d}", f"S{season}"]:
                        for e_pattern in [f"E{episode:02d}", f"E{episode}"]:
                            if f"{s_pattern}{e_pattern}" in path_combined:
                                match_flag = True
                                break
                        if match_flag:
                            break

                    # 特征组二：汉字或空格分散型组合（例如：同时出现 "第1季" 且 出现 "第7集"）
                    if not match_flag:
                        has_s = (f"SEASON {season}" in path_combined or 
                                 f"SEASON{season}" in path_combined or 
                                 f"第{season}季" in path_combined)
                        has_e = (f"E{episode:02d}" in path_combined or 
                                 f"E{episode}" in path_combined or 
                                 f"第{episode}集" in path_combined or 
                                 f"第{episode:02d}集" in path_combined or
                                 f"EP{episode:02d}" in path_combined or
                                 f"EP{episode}" in path_combined)
                        if has_s and has_e:
                            match_flag = True

                    if not match_flag:
                        continue  # 路径特征未通过，安全放行

                # 完美收网，执行物理删除
                db.delete(rec)
                deleted_count += 1

            if deleted_count > 0:
                db.commit()
                if mtype == MediaType.TV:
                    logger.info(f"【Emby缺失订阅】自动联动清理成功！已从底层路径精准抹除旧整理历史: {title} S{season:02d}E{episode:02d} (共 {deleted_count} 条)")
                else:
                    logger.info(f"【Emby缺失订阅】自动联动清理成功！已从底层路径精准抹除旧整理历史: {title} (共 {deleted_count} 条)")

        except Exception as e:
            db.rollback()
            logger.error(f"【Emby缺失订阅】自动联动清理历史整理记录失败 {title}: {e}")
        finally:
            db.close()  # 显式回收释放会话，绝不造成数据库连接泄漏

    def _has_existing_episodes(
        self, service, user_id: str, series_id: str, season: int
    ) -> bool:
        """
        检查 Emby 中是否存在该季度至少一集非虚拟（实际存在）的剧集
        """
        url = (
            f"[HOST]emby/Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&ParentId={series_id}"
            f"&IncludeItemTypes=Episode"
            f"&ParentIndexNumber={season}"
            f"&IsMissing=false"
            f"&Recursive=true"
            f"&Limit=1"
        )
        try:
            res = service.instance.get_data(url=url)
            if not res:
                return False
            data = res.json()
            return data.get("TotalRecordCount", 0) > 0 or len(data.get("Items", [])) > 0
        except Exception as e:
            logger.error(f"检查 Emby 现有剧集失败: {e}")
            return False

    def _fetch_missing_episodes(
        self, service, user_id: str, parent_id: Optional[str] = None
    ) -> List[dict]:
        """
        调用 Emby /Shows/Missing API 获取遗漏（Virtual）剧集
        """
        url = (
            f"[HOST]emby/Shows/Missing?"
            f"api_key=[APIKEY]"
            f"&UserId={user_id}"
            f"&Fields=ProviderIds,Overview,PremiereDate,ProductionYear"
            f"&Limit=5000"  # 提高至 5000，彻底终结大型库截断现象
        )
        if parent_id:
            url += f"&ParentId={parent_id}"

        try:
            res = service.instance.get_data(url=url)
            if not res:
                return []
            data = res.json()
            items = data.get("Items", [])

            if self._skip_future and items:
                now = datetime.now()
                filtered = []
                for item in items:
                    premiere = item.get("PremiereDate")
                    if premiere:
                        try:
                            premiere_dt = datetime.fromisoformat(
                                premiere.replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                            if premiere_dt > now:
                                continue
                        except (ValueError, TypeError):
                            pass
                    filtered.append(item)
                items = filtered

            logger.info(
                f"获取到 {len(items)} 个遗漏剧集"
                + (f" (媒体库 {parent_id})" if parent_id else "")
            )
            return items

        except Exception as e:
            logger.error(f"获取遗漏剧集失败: {e}")
            return []

    def _fetch_boxsets(
        self,
        service,
        user_id: str,
        parent_id: Optional[str] = None,
    ) -> List[dict]:
        """
        获取 Emby 媒体库中的所有 BoxSet（合集）
        """
        url = (
            f"[HOST]emby/Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&IncludeItemTypes=BoxSet"
            f"&Recursive=true"
            f"&Fields=ProviderIds"
            f"&Limit=5000"
        )
        if parent_id:
            url += f"&ParentId={parent_id}"

        try:
            res = service.instance.get_data(url=url)
            if not res:
                return []
            data = res.json()
            items = data.get("Items", [])
            logger.info(
                f"获取到 {len(items)} 个合集"
                + (f" (媒体库 {parent_id})" if parent_id else "")
            )
            return items
        except Exception as e:
            logger.error(f"获取合集列表失败: {e}")
            return []

    def _get_boxset_movie_tmdb_ids(
        self, service, user_id: str, boxset_id: str
    ) -> set:
        """
        获取 BoxSet 中已有电影 of TMDB ID 集合
        """
        url = (
            f"[HOST]emby/Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&ParentId={boxset_id}"
            f"&IncludeItemTypes=Movie"
            f"&Fields=ProviderIds"
            f"&Recursive=true"
            f"&Limit=5000"
        )
        tmdb_ids: set = set()
        try:
            res = service.instance.get_data(url=url)
            if not res:
                return tmdb_ids
            data = res.json()
            for item in data.get("Items", []):
                provider_ids = item.get("ProviderIds", {})
                tmdb_str = (
                    provider_ids.get("Tmdb") or provider_ids.get("tmdb")
                )
                if tmdb_str:
                    try:
                        tmdb_ids.add(int(tmdb_str))
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            logger.error(f"获取合集子项失败: {e}")
        return tmdb_ids

    def _resolve_tmdb_id(
        self,
        service,
        user_id: str,
        series_id: str,
        series_name: str,
    ) -> Optional[int]:
        """
        获取剧集的 TMDB ID
        """
        try:
            url = (
                f"[HOST]emby/Users/{user_id}/Items/{series_id}?"
                f"api_key=[APIKEY]"
                f"&Fields=ProviderIds"
            )
            res = service.instance.get_data(url=url)
            if res:
                series_data = res.json()
                provider_ids = series_data.get("ProviderIds", {})
                tmdb_str = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
                if tmdb_str:
                    return int(tmdb_str)
        except Exception as e:
            logger.debug(f"从 Emby 获取 Series TMDB ID 失败: {e}")

        try:
            meta = MetaInfo(title=series_name)
            meta.type = MediaType.TV
            mediainfo = self._media_chain.recognize_media(meta=meta)
            if mediainfo and mediainfo.tmdb_id:
                logger.info(
                    f"通过 MediaChain 识别到 TMDB ID: "
                    f"{series_name} -> {mediainfo.tmdb_id}"
                )
                return mediainfo.tmdb_id
        except Exception as e:
            logger.debug(f"MediaChain 识别失败: {series_name}, {e}")

        return None

    def _resolve_collection_id(
        self, service, user_id: str, boxset: dict
    ) -> Optional[int]:
        """
        获取 BoxSet 对应的 TMDB 合集 ID
        """
        provider_ids = boxset.get("ProviderIds", {})
        tmdb_str = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
        if tmdb_str:
            try:
                return int(tmdb_str)
            except (ValueError, TypeError):
                pass

        boxset_id = boxset.get("Id")
        boxset_name = boxset.get("Name", "未知合集")
        if not boxset_id:
            return None

        url = (
            f"[HOST]emby/Users/{user_id}/Items?"
            f"api_key=[APIKEY]"
            f"&ParentId={boxset_id}"
            f"&IncludeItemTypes=Movie"
            f"&Fields=ProviderIds"
            f"&Limit=1"
        )
        try:
            res = service.instance.get_data(url=url)
            if not res:
                return None
            data = res.json()
            items = data.get("Items", [])
            if not items:
                return None

            movie_item = items[0]
            movie_provider_ids = movie_item.get("ProviderIds", {})
            movie_tmdb_str = (
                movie_provider_ids.get("Tmdb")
                or movie_provider_ids.get("tmdb")
            )
            if not movie_tmdb_str:
                return None

            movie_tmdb_id = int(movie_tmdb_str)

            mediainfo = MediaChain().recognize_media(
                mtype=MediaType.MOVIE, tmdbid=movie_tmdb_id
            )
            if not mediainfo:
                return None

            cid = getattr(mediainfo, "collection_id", None)
            if cid:
                logger.info(
                    f"通过子项电影 {movie_item.get('Name')} "
                    f"识别到合集 {boxset_name} 的 TMDB 合集 ID: {cid}"
                )
                return cid

            if mediainfo.tmdb_info:
                btc = mediainfo.tmdb_info.get("belongs_to_collection")
                if btc and btc.get("id"):
                    cid = btc["id"]
                    logger.info(
                        f"通过子项电影 {movie_item.get('Name')} "
                        f"的 belongs_to_collection 识别到"
                        f"合集 {boxset_name} 的 TMDB 合集 ID: {cid}"
                    )
                    return cid

        except Exception as e:
            logger.debug(
                f"从子项电影获取 TMDB 合集 ID 失败: {e}"
            )

        return None

    # ================================================================
    # 辅助方法
    # ================================================================

    @staticmethod
    def _group_by_series_season(
        items: List[dict],
    ) -> Dict[Tuple[str, int], List[dict]]:
        groups: Dict[Tuple[str, int], List[dict]] = {}
        for item in items:
            series_id = item.get("SeriesId")
            season = item.get("ParentIndexNumber", 1)
            if not series_id:
                continue
            key = (series_id, season)
            if key not in groups:
                groups[key] = []
            groups[key].append(item)
        return groups

    def _get_scan_library_ids(self, server_name: str) -> List[str]:
        if not self._libraries:
            return []

        result = []
        prefix = f"{server_name}-"
        for lib_value in self._libraries:
            if lib_value.startswith(prefix):
                lib_id = lib_value[len(prefix):]
                result.append(lib_id)
        return result

    def _build_library_list(self) -> list:
        lib_items = []
        if not self._mediaservers:
            return lib_items

        services = self.mediaserver_helper.get_services(
            name_filters=self._mediaservers
        )
        if not services:
            return lib_items

        for server_name, service in services.items():
            if service.instance.is_inactive() or service.type != "emby":
                continue
            try:
                url = (
                    "[HOST]emby/Library/VirtualFolders/Query?"
                    "api_key=[APIKEY]"
                )
                res = service.instance.get_data(url=url)
                if not res:
                    continue
                data = res.json()
                libraries = data.get("Items", [])
                for lib in libraries:
                    lib_id = lib.get("Id")
                    lib_name = lib.get("Name")
                    if lib_id and lib_name:
                        lib_items.append({
                            "title": f"{server_name}: {lib_name}",
                            "value": f"{server_name}-{lib_id}",
                        })
            except Exception as e:
                logger.debug(
                    f"获取媒体库列表失败: {server_name}, {e}"
                )

        return lib_items

    # ================================================================
    # 表单
    # ================================================================

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        server_items = []
        if self.mediaserver_helper:
            for svc in self.mediaserver_helper.get_services().values():
                server_items.append({
                    "title": svc.name,
                    "value": svc.name,
                })

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "发送通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enable_episodes",
                                            "label": "订阅遗漏剧集",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enable_collections",
                                            "label": "订阅合集缺失电影",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "skip_future",
                                            "label": "跳过未播出剧集",
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
                                            "model": "skip_entire_missing",
                                            "label": "跳过整季遗漏的订阅",
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
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "0 8 * * *",
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
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "mediaservers",
                                            "label": "媒体服务器",
                                            "items": server_items,
                                            "hint": "选择要扫描的 Emby 服务器",
                                            "persistentHint": True,
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
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "libraries",
                                            "label": "媒体库",
                                            "items": self._all_libraries or [],
                                            "hint": "选择要扫描的媒体库，不选则扫描全部",
                                            "persistentHint": True,
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
                                                "本插件支持两种扫描模式，可独立开关：\n\n"
                                                "【遗漏剧集】调用 Emby /Shows/Missing API 获取遗漏剧集，按剧集 + 季创建 MoviePilot 订阅\n\n"
                                                "【合集缺失电影】扫描 Emby 中的 BoxSet 合集，通过 TMDB 获取合集完整电影列表，自动订阅缺失的电影\n\n"
                                                "两种模式均会记录历史避免重复处理，并已全面安全支持网盘文件丢失后自动出栈清理对应的历史转移记录。"
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
            "onlyonce": False,
            "cron": "",
            "mediaservers": [],
            "libraries": [],
            "skip_future": True,
            "enable_episodes": True,
            "enable_collections": False,
            "skip_entire_missing": True,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(f"Emby 缺失订阅停止服务异常: {e}")
