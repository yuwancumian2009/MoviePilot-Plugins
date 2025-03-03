from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.schemas import NotificationType
from app.core.config import settings
from app import schemas
from pydantic import BaseModel

class NotifyRequest(BaseModel):
    title: str
    text: str

class MsgNotify(_PluginBase):
    # 插件名称
    plugin_name = "外部消息转发"
    # 插件描述
    plugin_desc = "接收外部应用自定义消息并推送。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins/main/icons/MsgNotify.png"
    # 插件版本
    plugin_version = "1.3.1"
    # 插件作者
    plugin_author = "KoWming"
    # 作者主页
    author_url = "https://github.com/KoWming"
    # 插件配置项ID前缀
    plugin_config_prefix = "msgnotify_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 任务执行间隔
    _enabled = False
    _notify = False
    _msgtype = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._msgtype = config.get("msgtype")

    def msg_notify_json(self, apikey: str, request: NotifyRequest) -> schemas.Response:
        """
        post 方式发送通知
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API令牌错误!")

        title = request.title
        text = request.text
        logger.info(f"收到以下消息:\n{title}\n{text}")
        if self._enabled and self._notify:
            mtype = NotificationType.Manual
            if self._msgtype:
                mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
            self.post_message(mtype=mtype,
                            title=title,
                            text=text)

        return schemas.Response(
            success=True,
            message="发送成功"
        )

    
    def msg_notify_form(self, apikey: str, title: str, text: str) -> schemas.Response:
        """
        get 方式发送通知
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API令牌错误!")

        logger.info(f"收到以下消息:\n{title}\n{text}")
        if self._enabled and self._notify:
            mtype = NotificationType.Manual
            if self._msgtype:
                mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
            self.post_message(mtype=mtype,
                            title=title,
                            text=text)

        return schemas.Response(
            success=True,
            message="发送成功"
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

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
        return [{
            "path": "/send_json",
            "endpoint": self.msg_notify_json,
            "methods": ["POST"],
            "summary": "外部应用自定义消息接口使用的API",
            "description": "接受自定义消息webhook通知并推送",
        },
        {
            "path": "/send_form",
            "endpoint": self.msg_notify_form,
            "methods": ["GET"],
            "summary": "外部应用自定义消息接口使用的API",
            "description": "接受自定义消息webhook通知并推送",
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 编历 NotificationType 枚举，生成消息类型选项
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
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
                                    'md': 6
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'msgtype',
                                            'label': '消息类型',
                                            'items': MsgTypeOptions,
                                            'hint': '如不选择，消息类型默认为[手动处理]。',
                                            'persistent-hint': True
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
                                            'type': 'success',
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'text': 'GET_API接口地址：http://moviepilot_ip:port/api/v1/plugin/MsgNotify/send_form?apikey=api_token'
                                                    },
                                                    {
                                                        'component': 'br'
                                                    },
                                                    {
                                                        'component': 'span',
                                                        'text': 'POST_API接口地址：http://moviepilot_ip:port/api/v1/plugin/MsgNotify/send_json?apikey=api_token'
                                                    }
                                                ]
                                            }
                                        ]
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
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'text': '此插件安装完后API未生效需要重启生效API。'
                                                    },
                                                    {
                                                        'component': 'br'
                                                    },
                                                    {
                                                        'component': 'span',
                                                        'text': '其中moviepilot_ip:port为MoviePilot的IP地址和端口号，api_token为MoviePilot的API令牌。'
                                                    },
                                                    {
                                                        'component': 'br'
                                                    },
                                                    {
                                                        'component': 'span',
                                                        'text': '请求方法：GET；必要参数：apikey={API_TOKEN}；title=消息标题；text=消息内容'
                                                    },
                                                    {
                                                        'component': 'br'
                                                    },
                                                    {
                                                        'component': 'span',
                                                        'text': '请求方法：POST；请求类型：application/json；请求体：{"title": "{title}", "text": "{content}"}'
                                                    },
                                                    {
                                                        'component': 'br'
                                                    },
                                                    {
                                                        'component': 'span',
                                                        'text': '必要参数或请求体可用变量请根据你使用的第三方应用要求填写！'
                                                    }
                                                ]
                                            }
                                        ]
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
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'span',
                                                'text': '参考了 '
                                            },
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://github.com/thsrite/MoviePilot-Plugins/',
                                                    'target': '_blank',
                                                    'style': 'text-decoration: underline;'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'u',
                                                        'text': 'thsrite/MoviePilot-Plugins'
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'span',
                                                'text': ' 项目，实现了插件的相关功能。特此感谢 '
                                            },
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://github.com/thsrite',
                                                    'target': '_blank',
                                                    'style': 'text-decoration: underline;'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'u',
                                                        'text': 'thsrite'
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'span',
                                                'text': ' 大佬！ '
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
            "notify": False,
            "msgtype": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
