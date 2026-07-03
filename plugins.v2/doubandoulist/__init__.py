import datetime
import time
import re
from pathlib import Path
from threading import Lock
from typing import Optional, Any, List, Dict, Tuple

import pytz
import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.media import MediaChain
from app.db.subscribe_oper import SubscribeOper
from app.db.user_oper import UserOper
from app.schemas.types import MediaType, EventType, SystemConfigKey

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.event import Event
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase

lock = Lock()


class DoubanDoulist(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣片单订阅下载"
    # 插件描述
    plugin_desc = "监控并同步特定的豆瓣片单（Doulist），自动将内部影片添加至MP订阅或搜索下载，支持为不同片单指定独立存储路径。"
    # 插件图标
    plugin_icon = "douban.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "yuwancumian"
    # 作者主页
    author_url = "https://github.com/yuwancumian2009/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "doubandoulist_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 2

    # 私有变量
    _doulist_base_url: str = "https://www.douban.com/doulist/%s/"
    _scheduler: Optional[BackgroundScheduler] = None

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _notify: bool = False
    _doulists: str = ""
    _cookie: str = ""
    _batch_size: int = 20
    _min_year: str = ""
    _min_rating: str = ""
    _clear: bool = False
    _clearflag: bool = False
    _search_download = False

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        # 配置映射
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._doulists = config.get("doulists")
            self._cookie = config.get("cookie")
            self._batch_size = int(config.get("batch_size") if config.get("batch_size") is not None else 20)
            self._min_year = config.get("min_year")
            self._min_rating = config.get("min_rating")
            self._onlyonce = config.get("onlyonce")
            self._clear = config.get("clear")
            self._search_download = config.get("search_download")

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("豆瓣片单同步服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.sync, 
                    trigger='date',
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                )

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce or self._clear:
                self._onlyonce = False
                self._clearflag = self._clear
                self._clear = False
                self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/douban_doulist_sync",
            "event": EventType.PluginAction,
            "desc": "同步豆瓣片单",
            "category": "订阅",
            "data": {
                "action": "douban_doulist_sync"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除豆瓣片单同步历史记录"
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "DoubanDoulist",
                "name": "豆瓣片单同步服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sync,
                "kwargs": {}
            }]
        elif self._enabled:
            return [{
                "id": "DoubanDoulist",
                "name": "豆瓣片单同步服务",
                "trigger": "interval",
                "func": self.sync,
                "kwargs": {"minutes": 60}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron表达式，留空自动'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4, 'style': 'display:flex;align-items: center;'}, 'content': [{'component': 'VSwitch', 'props': {'model': 'search_download', 'label': '搜索下载'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'batch_size', 'label': '单次同步新片数量限制', 'placeholder': '建议15-30，设为0则不限制'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'min_year', 'label': '上映年份筛选 (>=)', 'placeholder': '例如：2020'}}]},
                            # 文案同步更新，提示用户这是按 TMDB 评分过滤
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'min_rating', 'label': 'TMDB 最低评分筛选 (>=)', 'placeholder': '例如：7.6，将采用 TMDB 评分进行过滤'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{'component': 'VTextarea', 'props': {'model': 'doulists', 'label': '片单配置列表', 'placeholder': '支持每行输入一个片单，格式为“片单ID|存储路径”，不指定路径则只输入ID。\n例如：\n155102602|/data/downloads/adult\n87654321', 'rows': 4}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{'component': 'VTextField', 'props': {'model': 'cookie', 'label': '豆瓣 Cookie', 'placeholder': '用于绕过豆瓣针对公开片单的翻页频率限制（选填）'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VSwitch', 'props': {'model': 'clear', 'label': '清理历史同步记录'}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 2 * * *",
            "doulists": "",
            "cookie": "",
            "batch_size": 20,
            "min_year": "",
            "min_rating": "",
            "clear": False,
            "search_download": False
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data('history')
        if not historys:
            return [{'component': 'div', 'text': '暂无数据', 'props': {'class': 'text-center'}}]
        
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        contents = []
        for history in historys:
            contents.append({
                'component': 'VCard',
                'content': [
                    {
                        "component": "VDialogCloseBtn",
                        "props": {'innerClass': 'absolute top-0 right-0'},
                        'events': {
                            'click': {
                                'api': 'plugin/DoubanDoulist/delete_history',
                                'method': 'get',
                                'params': {
                                    'doubanid': history.get("doubanid"),
                                    'apikey': settings.API_TOKEN
                                }
                            }
                        },
                    },
                    {
                        'component': 'div',
                        'props': {'class': 'd-flex justify-space-start flex-nowrap flex-row'},
                        'content': [
                            {'component': 'div', 'content': [{'component': 'VImg', 'props': {'src': history.get("poster"), 'height': 120, 'width': 80, 'cover': True}}]},
                            {'component': 'div', 'content': [
                                {'component': 'VCardTitle', 'content': [{'component': 'a', 'props': {'href': f"https://movie.douban.com/subject/{history.get('doubanid')}", 'target': '_blank'}, 'text': history.get("title")}]},
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'类型：{history.get("type")}'},
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'同步时间：{history.get("time")}'},
                                {'component': 'VCardText', 'props': {'class': 'pa-0 px-2'}, 'text': f'结果：{history.get("action")}'}
                            ]}
                        ]
                    }
                ]
            })
        return [{'component': 'div', 'props': {'class': 'grid gap-3 grid-info-card'}, 'content': contents}]

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "doulists": self._doulists,
            "cookie": self._cookie,
            "batch_size": self._batch_size,
            "min_year": self._min_year,
            "min_rating": self._min_rating,
            "clear": self._clear,
            "search_download": self._search_download
        })

    def delete_history(self, doubanid: str, apikey: str):
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        historys = self.get_data('history')
        if not historys:
            return schemas.Response(success=False, message="未找到历史记录")
        historys = [h for h in historys if h.get("doubanid") != doubanid]
        self.save_data('history', historys)
        return schemas.Response(success=True, message="删除成功")

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

    def _parse_doulist(self, doulist_id: str) -> List[Tuple[str, str]]:
        items = []
        start = 0
        headers = {
            "User-Agent": settings.USER_AGENT or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.douban.com/"
        }
        if self._cookie:
            headers["Cookie"] = self._cookie

        while True:
            url = f"{self._doulist_base_url % doulist_id}?start={start}"
            logger.info(f"正在抓取片单 {doulist_id}，分页参数 start={start}")
            try:
                res = requests.get(url, headers=headers, timeout=15)
                if res.status_code != 200:
                    logger.error(f"豆瓣片单请求失败，状态码: {res.status_code}")
                    break
                
                soup = BeautifulSoup(res.text, "html.parser")
                items_div = soup.find_all("div", class_="doulist-item")
                if not items_div:
                    break

                page_has_movie = False
                for item in items_div:
                    title_div = item.find("div", class_="title")
                    if not title_div:
                        continue
                    a_tag = title_div.find("a")
                    if not a_tag or not a_tag.get("href"):
                        continue

                    href = a_tag.get("href")
                    if "movie.douban.com/subject" in href:
                        match = re.search(r"subject/(\d+)", href)
                        if match:
                            db_id = match.group(1)
                            title = a_tag.get_text(strip=True)
                            items.append((db_id, title))
                            page_has_movie = True

                if not page_has_movie or len(items_div) < 25:
                    break

                start += 25
                time.sleep(2)
            except Exception as e:
                logger.error(f"解析片单页出现异常: {str(e)}")
                break

        return items

    def sync(self):
        if not self._doulists:
            logger.warn("未配置豆瓣片单ID，退出同步")
            return

        history = [] if self._clearflag else (self.get_data('history') or [])
        
        mediachain = MediaChain()
        downloadchain = DownloadChain()
        subscribechain = SubscribeChain()
        searchchain = SearchChain()
        subscribeoper = SubscribeOper()

        raw_lines = re.split(r'[\r\n,]+', self._doulists)
        doulist_configs = []
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            if '|' in line:
                parts = line.split('|', 1)
                doulist_configs.append((parts[0].strip(), parts[1].strip()))
            else:
                doulist_configs.append((line, None))

        processed_new_count = 0

        for doulist_id, custom_path in doulist_configs:
            if self._batch_size > 0 and processed_new_count >= self._batch_size:
                break
                
            logger.info(f"===> 开始同步豆瓣片单: {doulist_id} (绑定定制路径: {custom_path}) <===")
            parsed_items = self._parse_doulist(doulist_id)
            logger.info(f"片单 {doulist_id} 解析完成，共发现电影/剧集资源 {len(parsed_items)} 个")

            for douban_id, raw_title in parsed_items:
                try:
                    if douban_id in [h.get("doubanid") for h in history]:
                        continue

                    if self._batch_size > 0 and processed_new_count >= self._batch_size:
                        logger.info(f"已达到单次处理上限（{self._batch_size}个新影视），暂停后续影片同步。")
                        break

                    logger.info(f"[配额 {processed_new_count + 1}/{self._batch_size}] 开始处理新影片: {raw_title} (豆瓣ID: {douban_id})")
                    processed_new_count += 1

                    meta = MetaInfo(title=raw_title)
                    douban_info = self.chain.douban_info(doubanid=douban_id)
                    
                    if not douban_info:
                        logger.warn(f"无法获取到影片 {raw_title} 的豆瓣详情数据，可能被风控或Cookie失效，跳过此片")
                        continue

                    meta.type = MediaType.MOVIE if douban_info.get("type") == "movie" else MediaType.TV
                    db_year = douban_info.get("year")

                    # 年份过滤（保留在前面，通过轻量级豆瓣年份快速拦截老片，节省 TMDB 请求）
                    if self._min_year:
                        try:
                            if db_year and int(db_year) < int(self._min_year):
                                logger.info(f"影片 {raw_title} 年份为 {db_year}，低于设定的最小年份 {self._min_year}，跳过")
                                history.append({
                                    "action": f"被过滤 (年份 {db_year} < {self._min_year})",
                                    "title": raw_title,
                                    "type": douban_info.get("type", "movie"),
                                    "year": db_year,
                                    "poster": douban_info.get("poster", ""),
                                    "overview": douban_info.get("intro", ""),
                                    "tmdbid": 0,
                                    "doubanid": douban_id,
                                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                })
                                continue
                        except Exception as ye:
                            logger.warn(f"年份过滤解析异常: {str(ye)}")

                    # 执行媒体库匹配，拿到规范的 TMDB 基础数据对象
                    if settings.RECOGNIZE_SOURCE == "themoviedb":
                        tmdbinfo = mediachain.get_tmdbinfo_by_doubanid(doubanid=douban_id, mtype=meta.type)
                        if not tmdbinfo:
                            logger.warn(f"无法通过豆瓣ID {douban_id} 转换得到 TMDB 信息")
                            continue
                        mediainfo = self.chain.recognize_media(meta=meta, tmdbid=tmdbinfo.get("id"))
                    else:
                        mediainfo = self.chain.recognize_media(meta=meta, doubanid=douban_id)

                    if not mediainfo:
                        logger.warn(f"影片 {raw_title} 媒体信息识别失败")
                        continue

                    # 修改点：将评分过滤下移至此处，全面改用 TMDB 评分进行判定
                    if self._min_rating:
                        try:
                            # 兼容 MoviePilot 不同版本中 MediaInfo 对象的 TMDB 评分属性名
                            tmdb_rating = getattr(mediainfo, 'vote_average', None) or getattr(mediainfo, 'rating', 0.0)
                            
                            try:
                                current_rating = float(tmdb_rating)
                            except (ValueError, TypeError):
                                current_rating = 0.0
                            
                            logger.info(f"影片 {mediainfo.title_year} TMDB 接口实际返回评分为: {tmdb_rating} (安全清洗为: {current_rating}), 设定最低评分为: {self._min_rating}")
                            
                            if current_rating < float(self._min_rating):
                                logger.info(f"影片 {mediainfo.title_year} TMDB 评分 {current_rating} 低于设定最低线 {self._min_rating}，执行过滤拦截")
                                history.append({
                                    "action": f"被过滤 (TMDB评分 {current_rating} < {self._min_rating})",
                                    "title": mediainfo.title,
                                    "type": mediainfo.type.value,
                                    "year": mediainfo.year,
                                    "poster": mediainfo.get_poster_image(),
                                    "overview": mediainfo.overview,
                                    "tmdbid": mediainfo.tmdb_id,
                                    "doubanid": douban_id,
                                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                })
                                continue
                        except Exception as re_err:
                            logger.warn(f"TMDB评分过滤解析异常: {str(re_err)}")

                    # 通过全部过滤后，进入检测缺失、安排下载/订阅的核心链路
                    exist_flag, no_exists = downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                    if exist_flag:
                        logger.info(f"本地已存在: {mediainfo.title_year}")
                        action = "已存在"
                    else:
                        username = f"豆瓣片单-{doulist_id}"
                        if self._search_download:
                            logger.info(f"本地不存在，开始为 {mediainfo.title_year} 检索下载资源...")
                            filter_results = searchchain.process(
                                mediainfo=mediainfo,
                                no_exists=no_exists,
                                sites=self.systemconfig.get(SystemConfigKey.RssSites),
                                rule_groups=self.systemconfig.get(SystemConfigKey.SubscribeFilterRuleGroups)
                            )
                            if filter_results:
                                action = "已安排下载"
                                if mediainfo.type == MediaType.MOVIE:
                                    download_id = downloadchain.download_single(context=filter_results[0], username=username, save_path=custom_path)
                                    if not download_id:
                                        SubscribeChain().add(title=mediainfo.title, year=mediainfo.year, mtype=mediainfo.type, tmdbid=mediainfo.tmdb_id, exist_ok=True, username=username, save_path=custom_path)
                                        action = "下载失败转订阅"
                                else:
                                    downloaded_list, no_exists = downloadchain.batch_download(contexts=filter_results, no_exists=no_exists, username=username, save_path=custom_path)
                                    if no_exists:
                                        sub_id, _ = SubscribeChain().add(title=mediainfo.title, year=mediainfo.year, mtype=mediainfo.type, tmdbid=mediainfo.tmdb_id, exist_ok=True, username=username, save_path=custom_path)
                                        action = "部分下载转订阅"
                                        subscribe = subscribeoper.get(sub_id)
                                        if subscribe:
                                            subscribechain.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo, downloads=downloaded_list, lefts=no_exists)
                            else:
                                SubscribeChain().add(title=mediainfo.title, year=mediainfo.year, mtype=mediainfo.type, tmdbid=mediainfo.tmdb_id, exist_ok=True, username=username, save_path=custom_path)
                                action = "未找到资源已添加订阅"
                        else:
                            SubscribeChain().add(title=mediainfo.title, year=mediainfo.year, mtype=mediainfo.type, tmdbid=mediainfo.tmdb_id, exist_ok=True, username=username, save_path=custom_path)
                            action = "已添加订阅"

                    history.append({
                        "action": f"{action} -> {custom_path}" if custom_path else action,
                        "title": mediainfo.title,
                        "type": mediainfo.type.value,
                        "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(),
                        "overview": mediainfo.overview,
                        "tmdbid": mediainfo.tmdb_id,
                        "doubanid": douban_id,
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    
                except Exception as item_err:
                    logger.error(f"同步片单单条数据记录异常 ({raw_title}): {str(item_err)}")

        self.save_data('history', history)
        self._clearflag = False
        logger.info(f"本次豆瓣片单同步执行完毕，共处理了 {processed_new_count} 个新影片。")

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        if event and event.event_data and event.event_data.get("action") == "douban_doulist_sync":
            logger.info("收到手动触发命令，开始执行豆瓣片单同步...")
            self.post_message(channel=event.event_data.get("channel"), title="开始同步豆瓣片单...", userid=event.event_data.get("user"))
            self.sync()
            self.post_message(channel=event.event_data.get("channel"), title="豆瓣片单同步完成！", userid=event.event_data.get("user"))