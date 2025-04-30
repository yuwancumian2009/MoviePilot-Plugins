import re
import pytz
import requests
import time

from lxml import etree
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler

from app.log import logger
from app.core.config import settings
from app.plugins import _PluginBase
from app.schemas import NotificationType

class ContentFilter:

    @staticmethod
    def lxml_get_HTML(response):
        return etree.HTML(response.text)

    @staticmethod
    def lxml_get_text(response, xpath, split_str=""):
        return split_str.join(etree.HTML(response.text).xpath(xpath))

    @staticmethod
    def lxml_get_texts(response, xpath, split_str=""):
        return [split_str.join(item.xpath(".//text()")) for item in etree.HTML(response.text).xpath(xpath)]

    @staticmethod
    def re_get_text(response, pattern, group=0):
        match = re.search(pattern, response.text)
        return match.group(group) if match else None

    @staticmethod
    def re_get_texts(response, pattern, group=0):
        return [match.group(group) for match in re.finditer(pattern, response.text)]

    @staticmethod
    def re_get_match(response, pattern):
        match = re.search(pattern, response.text)
        return match

class VicomoVS(_PluginBase):
    # 插件名称
    plugin_name = "象岛传说竞技场"
    # 插件描述
    plugin_desc = "象岛传说竞技场，对战boss。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins/main/icons/Vicomovs.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "KoWming"
    # 作者主页
    author_url = "https://github.com/KoWming"
    # 插件配置项ID前缀
    plugin_config_prefix = "vicomovs_"
    # 加载顺序
    plugin_order = 24
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = False

    # 任务执行间隔
    _cron: Optional[str] = None
    _cookie: Optional[str] = None
    _history_count: Optional[int] = None

    # 对战参数
    _vs_boss_count: int = 3
    _vs_boss_interval: int = 15
    _vs_site_url: str = "https://ptvicomo.net/"
    
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """
        初始化插件
        """
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._cron = config.get("cron")
            self._cookie = config.get("cookie")
            self._notify = config.get("notify", False)
            self._onlyonce = config.get("onlyonce", False)
            self._history_count = int(config.get("history_count", 10))
            self._vs_boss_count = int(config.get("vs_boss_count", 3))
            self._vs_boss_interval = int(config.get("vs_boss_interval", 15))

        if self._onlyonce:
            try:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"象岛传说竞技场服务启动，立即运行一次")
                self._scheduler.add_job(func=self._battle_task, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="象岛传说竞技场")
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "cron": self._cron,
                    "enabled": self._enabled,
                    "cookie": self._cookie,
                    "notify": self._notify,
                    "history_count": self._history_count,
                    "vs_boss_count": self._vs_boss_count,
                    "vs_boss_interval": self._vs_boss_interval
                })

                # 启动任务
                if self._scheduler.get_jobs():
                   self._scheduler.print_jobs()
                   self._scheduler.start()
            except Exception as e:
                logger.error(f"象岛传说竞技场服务启动失败: {str(e)}")

    def vs_boss(self):
        """对战boss"""

        self.vs_boss_url = self._vs_site_url + "/customgame.php?action=exchange"
        self.headers = {
            "cookie": self._cookie,
            "referer": self._vs_site_url,
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0"
        }
        
        if datetime.today().weekday() in [0, 2]:
            vs_boss_data = "option=1&vs_member_name=0&submit=%E9%94%8B%E8%8A%92%E4%BA%A4%E9%94%99+-+1v1"  # Monday Wednesday
        elif datetime.today().weekday() in [1, 3]:
            vs_boss_data = "option=1&vs_member_name=0%2C1%2C2%2C3%2C4&submit=%E9%BE%99%E4%B8%8E%E5%87%A4%E7%9A%84%E6%8A%97%E8%A1%A1+-+%E5%9B%A2%E6%88%98+5v5"  # Thuesday Thursday
        elif datetime.today().weekday() in [4, 5, 6]:
            vs_boss_data = "option=1&vs_member_name=0%2C1%2C2%2C3%2C4%2C5%2C6%2C7%2C8%2C9%2C10%2C11%2C12%2C13%2C14%2C15%2C16&submit=%E4%B8%96%E7%95%8Cboss+-+%E5%AF%B9%E6%8A%97Sysrous"
        self.headers.update({
            "content-type": "application/x-www-form-urlencoded",
            "pragma": "no-cache",
        })
        response = requests.post(self.vs_boss_url, headers=self.headers, data=vs_boss_data)

        # 从响应中提取重定向 URL
        redirect_url = None
        match = ContentFilter.re_get_match(response, r"window\.location\.href\s*=\s*'([^']+战斗结果[^']+)'")
        if match:
            redirect_url = match.group(1)
            print(f"提取到的战斗结果重定向 URL: {redirect_url}")
        else:
            print("未找到战斗结果重定向 URL")
            return None

        # 访问重定向 URL
        battle_result_response = requests.get(redirect_url, headers=self.headers)
        print(f"战斗结果重定向页面状态码: {battle_result_response.status_code}")
        # print(battle_result_response.text)  # 可选：调试时查看响应内容

        # 解析战斗结果页面并提取 battleMsgInput
        parsed_html = ContentFilter.lxml_get_HTML(battle_result_response)
        battle_msg_input = parsed_html.xpath('//*[@id="battleMsgInput"]')
        if battle_msg_input:
            battle_info = parsed_html.xpath('//*[@id="battleResultStringLastShow"]/div[1]//text()')
            battle_text = ' '.join([text.strip() for text in battle_info if text.strip()])
            print("找到Battle Info:", battle_text)
            print("找到Battle Result:",
                parsed_html.xpath('//*[@id="battleResultStringLastShow"]/div[2]/text()')[0].strip())
            return parsed_html.xpath('//*[@id="battleResultStringLastShow"]/div[2]/text()')[0].strip()
        else:
            print("未找到Battle Result")
            return None

    def _battle_task(self):
        """
        执行对战任务
        """
        try:
            # 开始执行对战
            logger.info("开始执行对战...")
            battle_results = []
            for i in range(self._vs_boss_count):
                logger.info(f"执行第 {i+1} 次对战")
                battle_result = self.vs_boss()
                if battle_result:
                    battle_results.append(battle_result)
                    logger.info(f"第 {i+1} 次对战结果：{battle_result}")
                if i < self._vs_boss_count - 1:  # 如果不是最后一次对战
                    logger.info(f"等待 {self._vs_boss_interval} 秒后执行下一次对战")
                    time.sleep(self._vs_boss_interval)

            # 生成报告
            logger.info("开始生成报告...")
            rich_text_report = self.generate_rich_text_report(battle_results)
            logger.info(f"报告生成完成：\n{rich_text_report}")

            # 保存历史记录
            sign_dict = {
                "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                "battle_results": battle_results
            }

            # 读取历史记录
            history = self.get_data('sign_dict') or []
            history.append(sign_dict)
            
            # 只保留最新的N条记录
            if len(history) > self._history_count:
                history = sorted(history, key=lambda x: x.get("date") or "", reverse=True)[:self._history_count]
            
            self.save_data(key="sign_dict", value=history)

            # 发送通知
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【象岛传说竞技场】对战任务完成：",
                    text=f"{rich_text_report}")

        except Exception as e:
            logger.error(f"执行对战任务时发生异常: {e}")

    def generate_rich_text_report(self, battle_results: List[str]) -> str:
        """生成对战报告"""
        try:
            report = f"对战次数：{len(battle_results)}\n"
            report += "对战结果：\n"
            
            for i, result in enumerate(battle_results, 1):
                report += f"第 {i} 次：{result}\n"
            
            return report
        except Exception as e:
            logger.error(f"生成报告时发生异常: {e}")
            return "象岛传说竞技场\n生成报告时发生错误，请检查日志以获取更多信息。"

    def get_state(self) -> bool:
        """获取插件状态"""
        return bool(self._enabled)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """获取命令"""
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """获取API"""
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        service = []
        if self._cron:
            service.append({
                "id": "VicomoVS",
                "name": "象岛传说竞技场 - 定时任务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self._battle_task,
                "kwargs": {}
            })

        if service:
            return service

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    # 基本设置
                    {
                        'component': 'VCard',
                        'props': {
                            'variant': 'flat',
                            'class': 'mb-6',
                            'color': 'surface'
                        },
                        'content': [
                            {
                                'component': 'VCardItem',
                                'props': {
                                    'class': 'pa-6'
                                },
                                'content': [
                                    {
                                        'component': 'VCardTitle',
                                        'props': {
                                            'class': 'd-flex align-center text-h6'
                                        },
                                        'content': [
                                            {
                                                'component': 'VIcon',
                                                'props': {
                                                    'color': 'primary',
                                                    'class': 'mr-3',
                                                    'size': 'default'
                                                },
                                                'text': 'mdi-cog'
                                            },
                                            {
                                                'component': 'span',
                                                'text': '基本设置'
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VCardText',
                                'props': {
                                    'class': 'px-6 pb-6'
                                },
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'sm': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'enabled',
                                                            'label': '启用插件',
                                                            'color': 'primary',
                                                            'hide-details': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'sm': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'notify',
                                                            'label': '开启通知',
                                                            'color': 'primary',
                                                            'hide-details': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'sm': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'onlyonce',
                                                            'label': '立即运行一次',
                                                            'color': 'primary',
                                                            'hide-details': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    # 功能设置
                    {
                        'component': 'VCard',
                        'props': {
                            'variant': 'flat',
                            'class': 'mb-6',
                            'color': 'surface'
                        },
                        'content': [
                            {
                                'component': 'VCardItem',
                                'props': {
                                    'class': 'pa-6'
                                },
                                'content': [
                                    {
                                        'component': 'VCardTitle',
                                        'props': {
                                            'class': 'd-flex align-center text-h6'
                                        },
                                        'content': [
                                            {
                                                'component': 'VIcon',
                                                'props': {
                                                    'color': 'primary',
                                                    'class': 'mr-3',
                                                    'size': 'default'
                                                },
                                                'text': 'mdi-sword-cross'
                                            },
                                            {
                                                'component': 'span',
                                                'text': '功能设置'
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VCardText',
                                'props': {
                                    'class': 'px-6 pb-6'
                                },
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'sm': 6
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'cookie',
                                                            'label': '站点Cookie',
                                                            'variant': 'outlined',
                                                            'color': 'primary',
                                                            'hide-details': True,
                                                            'class': 'mt-2'
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'sm': 6
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'cron',
                                                            'label': '签到周期(cron)',
                                                            'variant': 'outlined',
                                                            'color': 'primary',
                                                            'hide-details': True,
                                                            'placeholder': '5位cron表达式，默认每天9点执行',
                                                            'class': 'mt-2'
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
                                                    'sm': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'model': 'vs_boss_count',
                                                            'label': '对战次数(秒)',
                                                            'variant': 'outlined',
                                                            'color': 'primary',
                                                            'hide-details': True,
                                                            'class': 'mt-2',
                                                            'items': [
                                                                {'title': '1次', 'value': 1},
                                                                {'title': '2次', 'value': 2},
                                                                {'title': '3次', 'value': 3}
                                                            ]
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'sm': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'model': 'vs_boss_interval',
                                                            'label': '对战间隔(秒)',
                                                            'variant': 'outlined',
                                                            'color': 'primary',
                                                            'hide-details': True,
                                                            'placeholder': '对战间隔(秒)',
                                                            'class': 'mt-2',
                                                            'items': [
                                                                {'title': '5秒', 'value': 5},
                                                                {'title': '10秒', 'value': 10},
                                                                {'title': '15秒', 'value': 15},
                                                                {'title': '20秒', 'value': 20}
                                                            ]
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'sm': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'history_count',
                                                            'label': '保留历史条数',
                                                            'variant': 'outlined',
                                                            'color': 'primary',
                                                            'hide-details': True,
                                                            'class': 'mt-2'
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    # 使用说明
                    {
                        'component': 'VCard',
                        'props': {
                            'variant': 'flat',
                            'class': 'mb-6',
                            'color': 'surface'
                        },
                        'content': [
                            {
                                'component': 'VCardItem',
                                'props': {
                                    'class': 'pa-6'
                                },
                                'content': [
                                    {
                                        'component': 'VCardTitle',
                                        'props': {
                                            'class': 'd-flex align-center text-h6'
                                        },
                                        'content': [
                                            {
                                                'component': 'VIcon',
                                                'props': {
                                                    'color': 'primary',
                                                    'class': 'mr-3',
                                                    'size': 'default'
                                                },
                                                'text': 'mdi-treasure-chest'
                                            },
                                            {
                                                'component': 'span',
                                                'text': '使用说明'
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VCardText',
                                'props': {
                                    'class': 'px-6 pb-6'
                                },
                                'content': [
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'text-body-1'
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'mb-4'
                                                },
                                                'text': '每人每天拥有三次参战机会，每场战斗最长持续30回合，击溃敌方全体角色获得胜利。'
                                            },
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'mb-4'
                                                },
                                                'text': '周一和周三是锋芒交错的时刻，1v1的激烈对决等着您。'
                                            },
                                            {
                                                'component': 'div',
                                                'props': {
                                                    'class': 'mb-4'
                                                },
                                                'text': '周二周四上演龙与凤的抗衡，5v5的团战战场精彩纷呈。'
                                            },
                                            {
                                                'component': 'div',
                                                'text': '周五、周六和周日，世界boss【Sysrous】将会降临，勇士们齐心协力，挑战最强BOSS，获得奖励Sysrous魔力/200000+总伤害/4的象草。'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "cookie": "",
            "history_count": 10,
            "cron": "0 9 * * *",
            "vs_boss_count": 3,
            "vs_boss_interval": 15
        }

    def get_page(self) -> List[dict]:
        # 查询同步详情
        historys = self.get_data('sign_dict')
        if not historys:
            return [
                {
                    'component': 'VCard',
                    'props': {
                        'variant': 'flat',
                        'class': 'mb-4'
                    },
                    'content': [
                        {
                            'component': 'VCardItem',
                            'props': {
                                'class': 'pa-6'
                            },
                            'content': [
                                {
                                    'component': 'VCardTitle',
                                    'props': {
                                        'class': 'd-flex align-center text-h6'
                                    },
                                    'content': [
                                        {
                                            'component': 'VIcon',
                                            'props': {
                                                'color': 'primary',
                                                'class': 'mr-3',
                                                'size': 'default'
                                            },
                                            'text': 'mdi-chart-line'
                                        },
                                        {
                                            'component': 'span',
                                            'text': '象草趋势'
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            'component': 'VCardText',
                            'content': [
                                {
                                    'component': 'div',
                                    'props': {
                                        'class': 'text-center py-4'
                                    },
                                    'content': [
                                        {
                                            'component': 'VIcon',
                                            'props': {
                                                'icon': 'mdi-database-remove',
                                                'size': '48',
                                                'color': 'grey'
                                            }
                                        },
                                        {
                                            'component': 'div',
                                            'props': {
                                                'class': 'text-subtitle-1 mt-2'
                                            },
                                            'text': '暂无历史记录'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]

        if not isinstance(historys, list):
            return [
                {
                    'component': 'VCard',
                    'props': {
                        'variant': 'flat',
                        'class': 'mb-4'
                    },
                    'content': [
                        {
                            'component': 'VCardItem',
                            'props': {
                                'class': 'pa-6'
                            },
                            'content': [
                                {
                                    'component': 'VCardTitle',
                                    'props': {
                                        'class': 'd-flex align-center text-h6'
                                    },
                                    'content': [
                                        {
                                            'component': 'VIcon',
                                            'props': {
                                                'color': 'primary',
                                                'class': 'mr-3',
                                                'size': 'default'
                                            },
                                            'text': 'mdi-chart-line'
                                        },
                                        {
                                            'component': 'span',
                                            'text': '象草趋势'
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            'component': 'VCardText',
                            'content': [
                                {
                                    'component': 'div',
                                    'props': {
                                        'class': 'text-center py-4'
                                    },
                                    'content': [
                                        {
                                            'component': 'VIcon',
                                            'props': {
                                                'icon': 'mdi-alert-circle',
                                                'size': '48',
                                                'color': 'error'
                                            }
                                        },
                                        {
                                            'component': 'div',
                                            'props': {
                                                'class': 'text-subtitle-1 mt-2'
                                            },
                                            'text': '数据格式错误，请检查日志以获取更多信息。'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]

        # 按照签到时间倒序并限制显示条数
        historys = sorted(historys, key=lambda x: x.get("date") or "", reverse=True)
        if self._history_count:
            historys = historys[:self._history_count]

        # 准备图表数据
        chart_data = []
        for history in historys:
            chart_data.append({
                'date': history.get('date'),
                'bonus': history.get('bonus', 0),
                'skill_bonus': history.get('skill_release_bonus', 0)
            })

        # 反转数据以便按时间顺序显示
        chart_data.reverse()

        # 拼装页面
        return [
            # 趋势卡片
            {
                'component': 'VCard',
                'props': {
                    'variant': 'flat',
                    'class': 'mb-4'
                },
                'content': [
                    {
                        'component': 'VCardItem',
                        'props': {
                            'class': 'pa-6'
                        },
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {
                                    'class': 'd-flex align-center text-h6'
                                },
                                'content': [
                                    {
                                        'component': 'VIcon',
                                        'props': {
                                            'color': 'primary',
                                            'class': 'mr-3',
                                            'size': 'default'
                                        },
                                        'text': 'mdi-chart-line'
                                    },
                                    {
                                        'component': 'span',
                                        'text': '象草趋势'
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-6'
                        },
                        'content': [
                            {
                                'component': 'VApexChart',
                                'props': {
                                    'type': 'area',
                                    'height': 300,
                                    'options': {
                                        'chart': {
                                            'type': 'area',
                                            'toolbar': {
                                                'show': True,
                                                'tools': {
                                                    'download': True,
                                                    'selection': True,
                                                    'zoom': True,
                                                    'zoomin': True,
                                                    'zoomout': True,
                                                    'pan': True,
                                                    'reset': True,
                                                    'home': True
                                                },
                                                'position': 'top',
                                                'autoSelected': 'zoom'
                                            },
                                            'stacked': False
                                        },
                                        'responsive': [
                                            {
                                                'breakpoint': 740,
                                                'options': {
                                                    'chart': {
                                                        'toolbar': {
                                                            'show': False
                                                        }
                                                    },
                                                    'xaxis': {
                                                        'categories': [item['date'].split()[0].split('-')[2] + '日' for item in chart_data],
                                                        'labels': {
                                                            'style': {
                                                                'fontSize': '12px'
                                                            }
                                                        }
                                                    },
                                                    'yaxis': {
                                                        'labels': {
                                                            'show': False
                                                        },
                                                        'title': {
                                                            'text': '获得象草',
                                                            'style': {
                                                                'color': '#66DA26'
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        ],
                                        'colors': ['#66DA26'],
                                        'dataLabels': {
                                            'enabled': False
                                        },
                                        'stroke': {
                                            'curve': 'smooth',
                                            'width': 2
                                        },
                                        'fill': {
                                            'type': 'gradient',
                                            'gradient': {
                                                'shadeIntensity': 1,
                                                'opacityFrom': 0.45,
                                                'opacityTo': 0.05,
                                                'stops': [0, 90, 100]
                                            }
                                        },
                                        'grid': {
                                            'borderColor': 'rgba(0,0,0,0.1)',
                                            'strokeDashArray': 6,
                                            'xaxis': {'lines': {'show': True}},
                                            'yaxis': {'lines': {'show': True}}
                                        },
                                        'xaxis': {
                                            'categories': [item['date'].split()[0] for item in chart_data],
                                            'labels': {
                                                'style': {
                                                    'fontSize': '12px'
                                                },
                                                'datetimeFormatter': {
                                                    'year': 'yyyy',
                                                    'month': 'MM',
                                                    'day': 'dd',
                                                    'hour': 'HH:mm'
                                                }
                                            }
                                        },
                                        'yaxis': {
                                            'title': {
                                                'text': '获得象草',
                                                'style': {
                                                    'color': '#66DA26'
                                                }
                                            },
                                            'labels': {
                                                'show': True,
                                                'style': {
                                                    'fontSize': '12px'
                                                }
                                            }
                                        },
                                        'tooltip': {
                                            'theme': 'light',
                                            'shared': True,
                                            'x': {
                                                'show': True
                                            },
                                            'y': {
                                                'formatter': lambda val: f"{val} 个"
                                            }
                                        }
                                    },
                                    'series': [
                                        {
                                            'name': '获得象草',
                                            'data': [sum(1 for result in history.get("battle_results", []) if "象草" in result) for history in chart_data]
                                        }
                                    ]
                                },
                                'on': {
                                    'mounted': 'function() { this.$nextTick(() => { const display = useDisplay(); this.chart.updateOptions({ yaxis: [{ labels: { show: !display.mdAndDown.value } }] }); }) }'
                                }
                            }
                        ]
                    }
                ]
            },
            # 历史记录表格
            {
                'component': 'VCard',
                'props': {
                    'variant': 'flat',
                    'class': 'mb-4'
                },
                'content': [
                    {
                        'component': 'VCardItem',
                        'props': {
                            'class': 'pa-6'
                        },
                        'content': [
                            {
                                'component': 'VCardTitle',
                                'props': {
                                    'class': 'd-flex align-center text-h6'
                                },
                                'content': [
                                    {
                                        'component': 'VIcon',
                                        'props': {
                                            'color': 'primary',
                                            'class': 'mr-3',
                                            'size': 'default'
                                        },
                                        'text': 'mdi-history'
                                    },
                                    {
                                        'component': 'span',
                                        'text': '历史记录'
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-6'
                        },
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'tr',
                                                'content': [
                                                    {
                                                        'component': 'th',
                                                        'props': {
                                                            'class': 'text-center text-body-1 font-weight-bold'
                                                        },
                                                        'text': '执行时间'
                                                    },
                                                    {
                                                        'component': 'th',
                                                        'props': {
                                                            'class': 'text-center text-body-1 font-weight-bold'
                                                        },
                                                        'text': '战斗次数'
                                                    },
                                                    {
                                                        'component': 'th',
                                                        'props': {
                                                            'class': 'text-center text-body-1 font-weight-bold'
                                                        },
                                                        'text': '战斗结果'
                                                    },
                                                    {
                                                        'component': 'th',
                                                        'props': {
                                                            'class': 'text-center text-body-1 font-weight-bold'
                                                        },
                                                        'text': '获得象草'
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': [
                                            {
                                                'component': 'tr',
                                                'props': {
                                                    'class': 'text-sm'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'td',
                                                        'props': {
                                                            'class': 'text-center text-high-emphasis'
                                                        },
                                                        'text': history.get("date")
                                                    },
                                                    {
                                                        'component': 'td',
                                                        'props': {
                                                            'class': 'text-center text-high-emphasis'
                                                        },
                                                        'text': len(history.get("battle_results", []))
                                                    },
                                                    {
                                                        'component': 'td',
                                                        'props': {
                                                            'class': 'text-center text-high-emphasis'
                                                        },
                                                        'text': "、".join(history.get("battle_results", []))
                                                    },
                                                    {
                                                        'component': 'td',
                                                        'props': {
                                                            'class': 'text-center text-high-emphasis'
                                                        },
                                                        'text': sum(1 for result in history.get("battle_results", []) if "象草" in result)
                                                    }
                                                ]
                                            } for history in historys
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'div',
                                'props': {
                                    'class': 'text-caption text-grey mt-2'
                                },
                                'text': f'共显示 {len(historys)} 条记录'
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self) -> None:
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
