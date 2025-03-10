import pytz
import time
import requests
import threading
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from cachetools import TTLCache, cached

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap

from app.chain.site import SiteChain
from app.core.config import settings
from app.core.event import eventmanager
from app.db.site_oper import SiteOper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.timer import TimerUtils

class GroupChatZone(_PluginBase):
    # 插件名称
    plugin_name = "群聊区"
    # 插件描述
    plugin_desc = "定时向多个站点发送预设消息(特定站点可获得奖励)。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins/main/icons/GroupChat.png"
    # 插件版本
    plugin_version = "1.2.8"
    # 插件作者
    plugin_author = "KoWming"
    # 作者主页
    author_url = "https://github.com/KoWming"
    # 插件配置项ID前缀
    plugin_config_prefix = "groupchatzone_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    sites: SitesHelper = None
    siteoper: SiteOper = None
    sitechain: SiteChain = None
    
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _interval_cnt: int = 2
    _chat_sites: List[str] = []
    _sites_messages: str = ""
    _start_time: Optional[int] = None
    _end_time: Optional[int] = None
    _lock: Optional[threading.Lock] = None
    _running: bool = False
    
    # 缓存设置
    _cache_ttl: int = 3600  # 缓存过期时间（秒）
    _site_cache: Optional[TTLCache] = None
    _cache_initialized: bool = False

    def init_plugin(self, config: Optional[dict] = None):
        self._lock = threading.Lock()
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        self.sitechain = SiteChain()
        
        # 初始化缓存
        self._site_cache = TTLCache(maxsize=1, ttl=self._cache_ttl)
        self._cache_initialized = False

        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._cron = str(config.get("cron", ""))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._notify = bool(config.get("notify", False))
            self._interval_cnt = int(config.get("interval_cnt", 2))
            self._chat_sites = config.get("chat_sites", [])
            self._sites_messages = str(config.get("sites_messages", ""))

            # 过滤掉已删除的站点 - 只获取一次站点列表
            all_site_ids = self.__get_all_site_ids(log_update=False)
            self._chat_sites = [site_id for site_id in self._chat_sites if site_id in all_site_ids]

            # 保存配置，不主动刷新缓存
            self.__update_config(refresh_cache=False)

        # 加载模块
        if self._enabled or self._onlyonce:

            # 立即运行一次
            if self._onlyonce:
                try:
                    # 定时服务
                    self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                    logger.info("站点喊话服务启动，立即运行一次")
                    self._scheduler.add_job(func=self.send_site_messages, trigger='date',
                                            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="站点喊话服务")

                    # 关闭一次性开关
                    self._onlyonce = False
                    # 保存配置
                    self.__update_config(refresh_cache=False)

                    # 启动任务
                    if self._scheduler and self._scheduler.get_jobs():
                        self._scheduler.print_jobs()
                        self._scheduler.start()
                except Exception as e:
                    logger.error(f"启动一次性任务失败: {str(e)}")

    def __get_site_info(self, refresh=False, log_update=True):
        """
        获取站点信息并创建映射，支持缓存
        :param refresh: 是否强制刷新缓存
        :param log_update: 是否记录更新日志
        :return: 包含站点信息和映射的字典
        """
        # 如果需要强制刷新缓存，则清空缓存
        if refresh and self._site_cache:
            self._site_cache.clear()
            self._cache_initialized = False
            
        if not self._cache_initialized or not self._site_cache:
            try:
                # 获取所有站点信息
                all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
                
                # 创建映射
                site_id_to_name = {site.get("id"): site.get("name") for site in all_sites}
                site_id_to_obj = {site.get("id"): site for site in all_sites}
                site_name_to_obj = {site.get("name"): site for site in all_sites}
                all_site_ids = list(site_id_to_name.keys())
                
                # 更新缓存
                site_info = {
                    "all_sites": all_sites,
                    "site_id_to_name": site_id_to_name,
                    "site_id_to_obj": site_id_to_obj,
                    "site_name_to_obj": site_name_to_obj,
                    "all_site_ids": all_site_ids
                }
                
                # 存入缓存
                self._site_cache["site_info"] = site_info
                self._cache_initialized = True
                
                if log_update:
                    logger.debug(f"站点信息缓存已更新，共 {len(all_sites)} 个站点")
                    
                return site_info
            except Exception as e:
                logger.error(f"获取站点信息失败: {str(e)}")
                # 如果获取失败，返回空结构
                empty_info = {
                    "all_sites": [],
                    "site_id_to_name": {},
                    "site_id_to_obj": {},
                    "site_name_to_obj": {},
                    "all_site_ids": []
                }
                return empty_info
        
        # 从缓存中获取站点信息
        return self._site_cache.get("site_info", {})

    def __get_all_site_ids(self, log_update=True) -> List[str]:
        """
        获取所有站点ID（内置站点 + 自定义站点）
        :param log_update: 是否记录更新日志
        :return: 站点ID列表
        """
        site_info = self.__get_site_info(log_update=log_update)
        return site_info["all_site_ids"]

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self, refresh_cache=True):
        """
        更新配置
        :param refresh_cache: 是否刷新站点缓存
        """
        if refresh_cache:
            self.__get_site_info(refresh=True, log_update=True)
        
        # 保存配置
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "cron": self._cron,
                "onlyonce": self._onlyonce,
                "interval_cnt": self._interval_cnt,
                "chat_sites": self._chat_sites,
                "sites_messages": self._sites_messages
            }
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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
            try:
                # 检查是否为5位cron表达式
                if str(self._cron).strip().count(" ") == 4:
                    # 解析cron表达式
                    cron_parts = str(self._cron).strip().split()
                    
                    # 检查是否为每分钟执行一次 (分钟位为 * 或 */1)
                    if cron_parts[0] == "*" or cron_parts[0] == "*/1":
                        logger.warning("检测到每分钟执行一次的配置，已自动调整为默认随机执行")
                        # 使用随机调度
                        return self.__get_random_schedule()
                    
                    # 正常的cron表达式
                    return [{
                        "id": "GroupChatZone",
                        "name": "站点喊话服务",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.send_site_messages,
                        "kwargs": {}
                    }]
                else:
                    # 2.3/9-23
                    crons = str(self._cron).strip().split("/")
                    if len(crons) == 2:
                        # 2.3
                        cron = crons[0]
                        # 9-23
                        times = crons[1].split("-")
                        if len(times) == 2:
                            # 9
                            self._start_time = int(times[0])
                            # 23
                            self._end_time = int(times[1])
                        if self._start_time and self._end_time:
                            # 检查间隔是否过小（小于1小时）
                            interval_hours = float(str(cron).strip())
                            if interval_hours < 1:
                                logger.warning(f"检测到间隔过小 ({interval_hours}小时)，已自动调整为默认随机执行")
                                return self.__get_random_schedule()
                                
                            return [{
                                "id": "GroupChatZone",
                                "name": "站点喊话服务",
                                "trigger": "interval",
                                "func": self.send_site_messages,
                                "kwargs": {
                                    "hours": interval_hours,
                                }
                            }]
                        else:
                            logger.error("站点喊话服务启动失败，周期格式错误")
                            return self.__get_random_schedule()
                    else:
                        # 尝试解析为小时间隔
                        try:
                            interval_hours = float(str(self._cron).strip())
                            # 检查间隔是否过小（小于1小时）
                            if interval_hours < 1:
                                logger.warning(f"检测到间隔过小 ({interval_hours}小时)，已自动调整为默认随机执行")
                                return self.__get_random_schedule()
                                
                            # 默认0-24 按照周期运行
                            return [{
                                "id": "GroupChatZone",
                                "name": "站点喊话服务",
                                "trigger": "interval",
                                "func": self.send_site_messages,
                                "kwargs": {
                                    "hours": interval_hours,
                                }
                            }]
                        except ValueError:
                            logger.error(f"无法解析周期配置: {self._cron}，已自动调整为默认随机执行")
                            return self.__get_random_schedule()
            except Exception as err:
                logger.error(f"定时任务配置错误：{str(err)}")
                return self.__get_random_schedule()
        elif self._enabled:
            # 使用随机调度
            return self.__get_random_schedule()
        return []

    def __get_random_schedule(self) -> List[Dict[str, Any]]:
        """
        获取随机调度配置
        :return: 随机调度配置列表
        """
        # 随机时间
        triggers = TimerUtils.random_scheduler(num_executions=1,
                                               begin_hour=9,
                                               end_hour=23,
                                               max_interval=6 * 60,
                                               min_interval=2 * 60)
        ret_jobs = []
        for trigger in triggers:
            ret_jobs.append({
                "id": f"GroupChatZone|{trigger.hour}:{trigger.minute}",
                "name": "站点喊话服务",
                "trigger": "cron",
                "func": self.send_site_messages,
                "kwargs": {
                    "hour": trigger.hour,
                    "minute": trigger.minute
                }
            })
        return ret_jobs

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 使用缓存获取站点信息，但不强制刷新
        site_info = self.__get_site_info(refresh=False, log_update=False)
        all_sites = site_info["all_sites"]

        site_options = [{"title": site.get("name"), "value": site.get("id")} for site in all_sites]
        
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval_cnt',
                                            'label': '执行间隔',
                                            'placeholder': '多消息自动发送间隔时间（秒）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'chat_sites',
                                            'label': '选择站点',
                                            'items': site_options
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'sites_messages',
                                            'label': '发送消息',
                                            'rows': 6,
                                            'placeholder': '每一行一个配置，配置方式：\n'
                                                           '站点名称|消息内容1|消息内容2|消息内容3|...\n'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '配置注意事项：'
                                                    '1、消息发送执行间隔(秒)不能小于0，也不建议设置过大。1~5秒即可，设置过大可能导致线程运行时间过长；'
                                                    '2、如配置有全局代理，会默认调用全局代理执行。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '执行周期支持：'
                                                    '1、5位cron表达式；'
                                                    '2、配置间隔（小时），如2.3/9-23（9-23点之间每隔2.3小时执行一次）；'
                                                    '3、周期不填默认9-23点随机执行1次。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "cron": "",
            "onlyonce": False,
            "interval_cnt": 2,
            "chat_sites": [],
            "sites_messages": ""
        }

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def get_page(self) -> List[dict]:
        pass

    def send_site_messages(self):
        """
        自动向站点发送消息
        """
        if not self._lock:
            self._lock = threading.Lock()
            
        if not self._lock.acquire(blocking=False):
            logger.warning("已有任务正在执行，本次调度跳过！")
            return
            
        try:
            self._running = True
            if self._chat_sites:
                site_messages = self._sites_messages if isinstance(self._sites_messages, str) else ""
                self.__get_site_info(refresh=True, log_update=True)
                
                site_msgs = self.parse_site_messages(site_messages, refresh_cache=False)
                self.__send_msgs(do_sites=self._chat_sites, site_msgs=site_msgs)
        except Exception as e:
            logger.error(f"发送站点消息时发生异常: {str(e)}")
        finally:
            self._running = False
            if self._lock and hasattr(self._lock, 'locked') and self._lock.locked():
                try:
                    self._lock.release()
                except RuntimeError:
                    pass
            logger.debug("任务执行完成，锁已释放")

    def get_selected_sites(self) -> List[Dict[str, Any]]:
        """
        获取已选中的站点对象列表
        :return: 站点对象列表
        """
        site_info = self.__get_site_info(refresh=False, log_update=False)
        site_id_map = site_info.get("site_id_to_obj", {})
        
        # 过滤掉不存在的站点ID
        selected_sites = []
        for site_id in self._chat_sites:
            if site_id in site_id_map:
                selected_sites.append(site_id_map[site_id])
            else:
                logger.warning(f"站点ID {site_id} 不存在或已被删除")
        
        return selected_sites

    def parse_site_messages(self, site_messages: str, refresh_cache=False) -> Dict[str, List[str]]:
        """
        解析输入的站点消息
        :param site_messages: 多行文本输入
        :param refresh_cache: 是否刷新站点缓存
        :return: 字典，键为站点名称，值为该站点的消息
        """
        result = {}
        try:
            # 获取已选站点的名称集合
            selected_sites = self.get_selected_sites()
            valid_site_names = {site.get("name").strip() for site in selected_sites}
            
            logger.debug(f"有效站点名称列表: {valid_site_names}")

            # 按行解析配置
            for line_num, line in enumerate(site_messages.strip().splitlines(), 1):
                line = line.strip()
                if not line:
                    continue  # 跳过空行

                # 分割配置项
                parts = line.split("|")
                if len(parts) < 2:
                    logger.warning(f"第{line_num}行格式错误，缺少分隔符: {line}")
                    continue

                # 解析站点名称和消息
                site_name = parts[0].strip()
                messages = [msg.strip() for msg in parts[1:] if msg.strip()]
                
                if not messages:
                    logger.warning(f"第{line_num}行 [{site_name}] 没有有效消息内容")
                    continue

                # 验证站点有效性
                if site_name not in valid_site_names:
                    logger.warning(f"第{line_num}行 [{site_name}] 不在选中站点列表中")
                    continue

                # 合并相同站点的消息
                if site_name in result:
                    result[site_name].extend(messages)
                    logger.debug(f"合并站点 [{site_name}] 的消息，当前数量：{len(result[site_name])}")
                else:
                    result[site_name] = messages

        except Exception as e:
            logger.error(f"解析站点消息时出现异常: {str(e)}", exc_info=True)
        finally:
            logger.info(f"解析完成，共配置 {len(result)} 个有效站点的消息")
            return result

    def __send_msgs(self, do_sites: list, site_msgs: Dict[str, List[str]]):
        """
        发送消息逻辑
        """
        # 获取站点对象
        selected_sites = self.get_selected_sites()
        
        if not selected_sites:
            logger.info("没有需要发送消息的站点！")
            return

        # 执行站点发送消息
        site_results = {}
        for site in selected_sites:
            site_name = site.get("name")
            logger.info(f"开始处理站点: {site_name}")
            messages = site_msgs.get(site_name, [])

            if not messages:
                logger.warning(f"站点 {site_name} 没有需要发送的消息！")
                continue

            success_count = 0
            failure_count = 0
            failed_messages = []

            for i, message in enumerate(messages):
                try:
                    self.send_message_to_site(site, message)
                    success_count += 1
                except Exception as e:
                    logger.error(f"向站点 {site_name} 发送消息 '{message}' 失败: {str(e)}")
                    failure_count += 1
                    failed_messages.append(message)

                if i < len(messages) - 1:
                    logger.info(f"等待 {self._interval_cnt} 秒后继续发送下一条消息...")
                    start_time = time.time()
                    time.sleep(self._interval_cnt)
                    logger.debug(f"实际等待时间：{time.time() - start_time:.2f} 秒")
            
            site_results[site_name] = {
                "success_count": success_count,
                "failure_count": failure_count,
                "failed_messages": failed_messages
            }

        # 发送通知
        if self._notify:
            total_sites = len(selected_sites)
            notification_text = f"全部站点数量: {total_sites}\n"
            for site_name, result in site_results.items():
                success_count = result["success_count"]
                failure_count = result["failure_count"]
                failed_messages = result["failed_messages"]
                notification_text += f"【{site_name}】成功发送{success_count}条信息，失败{failure_count}条\n"
                if failed_messages:
                    notification_text += f"失败的消息: {', '.join(failed_messages)}\n"
            notification_text += f"\n{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}"

            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【执行喊话任务完成】:",
                text=notification_text
            )

        # 检查是否所有消息都发送成功
        all_successful = all(result["success_count"] == len(site_msgs.get(site_name, [])) 
                            for site_name, result in site_results.items())
        if all_successful:
            logger.info("所有站点的消息发送成功。")
        else:
            logger.info("部分消息发送失败！！！")

        self.__update_config(refresh_cache=False)

    def send_message_to_site(self, site_info: CommentedMap, message: str):
        """
        向站点发送消息
        """
        if not site_info:
            logger.error("无效的站点信息！")
            return

        # 站点信息
        site_name = site_info.get("name", "").strip()
        site_url = site_info.get("url", "").strip()
        site_cookie = site_info.get("cookie", "").strip()
        ua = site_info.get("ua", "").strip()
        proxies = settings.PROXY if site_info.get("proxy") else None

        if not all([site_name, site_url, site_cookie, ua]):
            logger.error(f"站点 {site_name} 缺少必要信息，无法发送消息！")
            return

        # 构建URL和请求参数
        send_url = urljoin(site_url, "/shoutbox.php")
        headers = {
            'User-Agent': ua,
            'Cookie': site_cookie,
            'Referer': site_url
        }
        params = {
            'shbox_text': message,
            'shout': '我喊',
            'sent': 'yes',
            'type': 'shoutbox'
        }

        # 配置重试策略
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[403, 404, 500, 502, 503, 504],
            allowed_methods=frozenset(['GET', 'POST']),
            raise_on_status=False
        )

        adapter = HTTPAdapter(max_retries=retries, pool_connections=1, pool_maxsize=1)

        with requests.Session() as session:
            session.headers.update(headers)
            if proxies:
                session.proxies = proxies
            session.mount('https://', adapter)
            session.mount('http://', adapter)
            
            try:
                response = session.get(
                    send_url, 
                    params=params,
                    timeout=(3.05, 10),
                    allow_redirects=False
                )
                response.raise_for_status()
                logger.info(f"向 {site_name} 发送消息 '{message}' 成功")
            except requests.exceptions.HTTPError as http_err:
                logger.error(f"向 {site_name} 发送消息 '{message}' 失败，HTTP 错误: {http_err}")
                raise
            except requests.exceptions.RequestException as req_err:
                logger.error(f"向 {site_name} 发送消息 '{message}' 失败，请求异常: {req_err}")
                raise

    def stop_service(self):
        """退出插件"""
        try:
            if self._scheduler:
                if self._lock and hasattr(self._lock, 'locked') and self._lock.locked():
                    logger.info("等待当前任务执行完成...")
                    try:
                        self._lock.acquire()
                        self._lock.release()
                    except:
                        pass
                if hasattr(self._scheduler, 'remove_all_jobs'):
                    self._scheduler.remove_all_jobs()
                if hasattr(self._scheduler, 'running') and self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            self._chat_sites = self.__remove_site_id(config.get("chat_sites") or [], site_id)
            # 保存配置，并刷新缓存
            self.__update_config(refresh_cache=True)

    def __remove_site_id(self, do_sites, site_id):
        if do_sites:
            if isinstance(do_sites, str):
                do_sites = [do_sites]
            # 删除对应站点
            if site_id:
                do_sites = [site for site in do_sites if int(site) != int(site_id)]
            else:
                # 清空
                do_sites = []
            # 若无站点，则停止
            if len(do_sites) == 0:
                self._enabled = False
        return do_sites