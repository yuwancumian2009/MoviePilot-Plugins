import traceback
import time
from typing import Any, List, Dict, Tuple

from app.log import logger
from app.plugins import _PluginBase

# 兼容各种版本的导包路径
try:
    from app.schemas import NotificationType
except ImportError:
    try:
        from app.schemas.types import NotificationType
    except ImportError:
        NotificationType = None


class MessageRouter(_PluginBase):
    plugin_name = "插件消息重定向"
    plugin_desc = "接管并重定向其他插件的消息发送类型，自带实时拦截日志。"
    plugin_icon = "setting.png"
    plugin_version = "1.0.0"
    plugin_author = "Custom"
    author_url = ""
    plugin_config_prefix = "messagerouter_"
    plugin_order = 20
    auth_level = 1

    _enabled = False
    _plugin_mapping_str = ""
    _plugin_mapping = {}
    _intercept_logs = []

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._intercept_logs = []

        if config:
            self._enabled = config.get("enabled", False)
            self._plugin_mapping_str = config.get("plugin_mapping", "")

        # 解析用户填写的映射规则
        self._plugin_mapping = {}
        if self._plugin_mapping_str:
            for line in str(self._plugin_mapping_str).split('\n'):
                line = line.strip()
                if ':' in line:
                    k, v = line.split(':', 1)
                    self._plugin_mapping[k.strip()] = v.strip()

        # 映射字典 (严格按照官方底层 NotificationType 枚举值定义)
        self._type_map = {}
        if NotificationType:
            self._type_map = {
                "资源下载": getattr(NotificationType, "Download", None),
                "整理入库": getattr(NotificationType, "Organize", getattr(NotificationType, "Library", None)),
                "订阅": getattr(NotificationType, "Subscribe", None),
                "站点": getattr(NotificationType, "SiteMessage", None),
                "媒体服务器": getattr(NotificationType, "MediaServer", None),
                "手动处理": getattr(NotificationType, "Manual", None),
                "插件": getattr(NotificationType, "Plugin", None),
                "其它": getattr(NotificationType, "Other", None),
                "其他": getattr(NotificationType, "Other", None) # 兼容日常打字的错别字
            }

        if self._enabled:
            self._add_log("🚀 插件已启动，开始监听底层消息通道...")
            self._patch_plugin_base()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_render_mode(self) -> Tuple[str, str]:
        """强制申明渲染模式为 vuetify 原生配置"""
        return "vuetify", ""

    def _add_log(self, msg: str):
        """安全添加内存日志（最多保留 50 条）"""
        if not hasattr(self, '_intercept_logs'):
            self._intercept_logs = []
        now = time.strftime("%H:%M:%S", time.localtime())
        self._intercept_logs.insert(0, f"[{now}] {msg}")
        if len(self._intercept_logs) > 50:
            self._intercept_logs.pop()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """规范化 VForm 结构"""
        return [{
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
                                        "label": "启用路由拦截"
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
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "plugin_mapping",
                                        "label": "插件消息路由映射规则",
                                        "rows": 7,
                                        "placeholder": "格式：插件名称:消息类型\n(可用类型：资源下载、整理入库、订阅、站点、媒体服务器、手动处理、插件、其它)\n\n例如：\n自动签到:订阅\n日志清理vue:其它\n豆瓣同步:整理入库"
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }], {
            "enabled": True if self._enabled else False,
            "plugin_mapping": str(self._plugin_mapping_str) if self._plugin_mapping_str else ""
        }

    def get_page(self) -> List[dict]:
        """组装纯文本日志页面"""
        if not self._enabled:
            return [{'component': 'VAlert', 'props': {'type': 'warning', 'text': '插件未启用，请前往配置开启。', 'class': 'mt-5'}}]

        rules_text = "【当前生效的规则】\n"
        if not self._plugin_mapping:
            rules_text += "暂无规则\n"
        else:
            for k, v in self._plugin_mapping.items():
                rules_text += f" • {k}  ➔  {v}\n"

        logs_text = "【实时监控日志 (最近50条)】\n"
        if not getattr(self, '_intercept_logs', []):
            logs_text += "暂无日志，请去手动运行一次其他插件触发通知。\n"
        else:
            logs_text += "\n".join(self._intercept_logs)

        return [
            {
                'component': 'VCard',
                'props': {'class': 'mb-4'},
                'content': [
                    {
                        'component': 'VCardText',
                        'text': rules_text,
                        'props': {'style': 'white-space: pre-wrap; font-size: 15px; font-weight: bold;'}
                    }
                ]
            },
            {
                'component': 'VCard',
                'content': [
                    {
                        'component': 'VCardText',
                        'text': logs_text,
                        'props': {'style': 'white-space: pre-wrap; font-family: monospace; max-height: 400px; overflow-y: auto;'}
                    }
                ]
            }
        ]

    def _patch_plugin_base(self):
        """核心拦截逻辑：绝对无损参数透传，防 Pydantic 崩溃"""
        if hasattr(_PluginBase, 'original_post_message'):
            return

        _PluginBase.original_post_message = _PluginBase.post_message
        
        def hooked_post_message(plugin_self, *args, **kwargs):
            try:
                plugin_id = getattr(plugin_self, 'plugin_name', plugin_self.__class__.__name__)
                
                if plugin_id and plugin_id in self._plugin_mapping:
                    target_type_str = self._plugin_mapping[plugin_id]
                    target_mtype = self._type_map.get(target_type_str)
                    
                    if target_mtype is not None:
                        replaced = False
                        
                        # 场景1：原调用显式指定了 mtype 的 kwargs (例如: mtype=NotificationType.Plugin)
                        if 'mtype' in kwargs:
                            kwargs['mtype'] = target_mtype
                            replaced = True
                            
                        # 场景2：原调用把 mtype 当作位置参数传进来了 (例如: self.post_message(NotificationType.Plugin, "标题"))
                        else:
                            new_args = list(args)
                            for i, arg in enumerate(new_args):
                                # 识别该参数是否为 NotificationType 的枚举实例
                                if NotificationType and isinstance(arg, type(target_mtype)):
                                    new_args[i] = target_mtype
                                    args = tuple(new_args)
                                    replaced = True
                                    break
                                    
                        # 场景3：原调用根本没传 mtype，依靠系统默认值，那我们就强行追加进去
                        if not replaced:
                            kwargs['mtype'] = target_mtype
                            replaced = True
                            
                        if replaced:
                            self._add_log(f"✅ 拦截成功 | {plugin_id} | 类型已变更为 [{target_type_str}]")
                    else:
                        self._add_log(f"❌ 无法识别 | 你填写的类型 [{target_type_str}] 不支持")
                else:
                    pass # 不匹配的不打日志，保持整洁

            except Exception as e:
                self._add_log(f"❌ 拦截异常: {str(e)}\n{traceback.format_exc()}")

            # 完美兼容，带着修复好的参数回到底层去
            return _PluginBase.original_post_message(plugin_self, *args, **kwargs)

        _PluginBase.post_message = hooked_post_message

    def stop_service(self):
        """卸载/停用插件时恢复原状"""
        if hasattr(_PluginBase, 'original_post_message'):
            _PluginBase.post_message = _PluginBase.original_post_message
            delattr(_PluginBase, 'original_post_message')
            self._add_log("🛑 插件已停用，底层通道已还原。")