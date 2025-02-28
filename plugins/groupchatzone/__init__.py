import pytz
import time
import requests
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap

from app.chain.site import SiteChain
from app.core.config import settings
from app.core.event import EventManager, eventmanager, Event
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
    plugin_version = "1.2.2"
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
    # 事件管理器
    event: EventManager = None
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _interval_cnt: int = 2
    _chat_sites: list = []
    _sites_messages: list = []
    _start_time: int = None
    _end_time: int = None

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        self.event = EventManager()
        self.sitechain = SiteChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._interval_cnt = config.get("interval_cnt", 2)
            self._chat_sites = config.get("chat_sites", [])
            self._sites_messages = config.get("sites_messages", "")


            # 过滤掉已删除的站点
            all_sites = [site.id for site in self.siteoper.list_order_by_pri()] + [site.get("id") for site in self.__custom_sites()]
            self._chat_sites = [site_id for site_id in all_sites if site_id in self._chat_sites]

            # 保存配置
            self.__update_config()

        # 加载模块
        if self._enabled or self._onlyonce:

            # 立即运行一次
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("站点喊话服务启动，立即运行一次")
                self._scheduler.add_job(func=self.send_site_messages, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点喊话服务")

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
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
            return [{
                "id": "GroupChatZone",
                "name": "站点喊话服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.send_site_messages,
                "kwargs": {}
            }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项（内置站点 + 自定义站点）
        customSites = self.__custom_sites()

        site_options = ([{"title": site.name, "value": site.id}
                         for site in self.siteoper.list_order_by_pri()]
                        + [{"title": site.get("name"), "value": site.get("id")}
                           for site in customSites])
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
                                            'rows': 8,
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
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '配置注意事项：'
                                                    '1、注意定时任务设置，避免每分钟执行一次导致频繁请求。'
                                                    '2、消息发送执行间隔(秒)不能小于0，也不建议设置过大。1~5秒即可，设置过大可能导致线程运行时间过长。'
                                                    '3、如配置有全局代理，会默认调用全局代理执行。'
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

    @eventmanager.register(EventType.PluginAction)
    def send_site_messages(self, event: Event = None):
        """
        自动向站点发送消息
        """
        if self._chat_sites:
            site_msgs = self.parse_site_messages(self._sites_messages)
            self.__send_msgs(do_sites=self._chat_sites, site_msgs=site_msgs, event=event)

    def parse_site_messages(self, site_messages: str) -> Dict[str, List[str]]:
        """
        解析输入的站点消息
        :param site_messages: 多行文本输入
        :return: 字典，键为站点名称，值为该站点的消息
        """
        result = {}
        try:
            # 获取所有选中的站点名称
            all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
            selected_site_names = {site.get("name") for site in all_sites if site.get("id") in self._chat_sites}
            logger.info(f"获取到的选中站点名称列表: {selected_site_names}")

            # 按行分割配置
            for line in site_messages.strip().splitlines():
                parts = line.split("|")
                if len(parts) > 1:
                    site_name = parts[0].strip()
                    if site_name in selected_site_names:
                        messages = [msg.strip() for msg in parts[1:] if msg.strip()]
                        if messages:
                            result[site_name] = messages
                        else:
                            logger.warn(f"站点 {site_name} 没有有效的消息内容")
                    else:
                        logger.warn(f"站点 {site_name} 不在选中列表中")
                else:
                    logger.warn(f"配置行格式错误，缺少分隔符: {line}")
        except Exception as e:
            logger.error(f"解析站点消息时出现异常: {str(e)}")
        logger.info(f"站点消息解析完成，解析结果: {result}")
        return result

    def __send_msgs(self, do_sites: list, site_msgs: Dict[str, List[str]], event: Event = None):
        """
        发送消息逻辑
        """
        # 查询所有站点
        all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
        # 过滤掉没有选中的站点
        do_sites = [site for site in all_sites if site.get("id") in do_sites] if do_sites else all_sites

        if not do_sites:
            logger.info("没有需要发送消息的站点")
            return

        # 执行站点发送消息
        site_results = {}
        for site in do_sites:
            site_name = site.get("name")
            logger.info(f"开始处理站点: {site_name}")
            messages = site_msgs.get(site_name, [])
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
                    logger.info(f"等待 {self._interval_cnt} 秒...")
                    time.sleep(self._interval_cnt)
            
            site_results[site_name] = {
                "success_count": success_count,
                "failure_count": failure_count,
                "failed_messages": failed_messages
            }

        # 发送通知
        if self._notify:
            total_sites = len(do_sites)
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
        all_successful = all(result["success_count"] == len(messages) for site_name, messages in site_msgs.items() if (result := site_results.get(site_name)))
        if all_successful:
            logger.info("所有站点的消息发送成功。")
        else:
            logger.info("部分消息发送失败！！！")

        self.__update_config()

    def send_message_to_site(self, site_info: CommentedMap, message: str):
        site_name = site_info.get("name", "").strip()
        site_url = site_info.get("url", "").strip()
        site_cookie = site_info.get("cookie", "").strip()
        ua = site_info.get("ua", "").strip()
        proxies = settings.PROXY if site_info.get("proxy") else None

        if not all([site_name, site_url, site_cookie, ua]):
            logger.error(f"站点 {site_name} 缺少必要信息，无法发送消息")
            return

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
            total=3,  # 总重试次数
            backoff_factor=1,  # 重试间隔时间因子
            status_forcelist=[403, 404, 500, 502, 503, 504],  # 需要重试的状态码
            allowed_methods=["GET"],  # 需要重试的HTTP方法
            raise_on_status=False  # 不在重试时抛出异常，手动处理
        )
        adapter = HTTPAdapter(max_retries=retries)

        # 使用 Session 对象复用，创建会话对象
        with requests.Session() as session:
            session.headers.update(headers)
            session.proxies = proxies
            session.mount('https://', adapter)
            
            attempt = 0
            while attempt < retries.total:
                try:
                    response = session.get(send_url, params=params, timeout=(3.05, 10))
                    response.raise_for_status()  # 自动处理 4xx/5xx 状态码
                    logger.info(f"向 {site_name} 发送消息 '{message}' 成功")
                    break
                except requests.exceptions.HTTPError as http_err:
                    logger.error(f"向 {site_name} 发送消息 '{message}' 失败，HTTP 错误: {http_err}")
                except requests.exceptions.RequestException as req_err:
                    logger.error(f"向 {site_name} 发送消息 '{message}' 失败，请求异常: {req_err}")
                
                attempt += 1
                if attempt < retries.total:
                    backoff_time = retries.get_backoff_time(attempt)
                    logger.info(f"重试 {attempt}/{retries.total}，将在 {backoff_time} 秒后重试...")
                    time.sleep(backoff_time)
                else:
                    logger.error(f"向 {site_name} 发送消息 '{message}' 失败，重试次数已达上限")

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            self._chat_sites = self.__remove_site_id(config.get("chat_sites") or [], site_id)
            # 保存配置
            self.__update_config()

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