import traceback
import time
from typing import Any, List, Dict, Tuple

from app.log import logger
from app.plugins import _PluginBase

try:
    from app.schemas import NotificationType
except ImportError:
    try:
        from app.schemas.types import NotificationType
    except ImportError:
        NotificationType = None


class MessageRouterVue(_PluginBase):
    plugin_name = "插件消息重定向 (Vue版)"
    plugin_desc = "接管并重定向其他插件的消息发送类型，支持可视化动态无限规则配置。"
    plugin_icon = "https://raw.githubusercontent.com/yuwancumian2009/MoviePilot-Plugins/main/icons/messagerouter.png"
    plugin_version = "1.0.0"
    plugin_author = "yuwancumian"
    author_url = "https://github.com/yuwancumian2009/MoviePilot-Plugins"
    plugin_config_prefix = "messageroutervue_"
    plugin_order = 21
    auth_level = 1

    _enabled = False
    _plugin_mapping = {}
    _intercept_logs = []

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._intercept_logs = []

        if config:
            self._enabled = config.get("enabled", False)
            rules = config.get("rules", [])
            self._plugin_mapping = {
                r.get("plugin"): r.get("target") 
                for r in rules if r.get("plugin") and r.get("target")
            }
        else:
            self._enabled = False
            self._plugin_mapping = {}

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
                "其他": getattr(NotificationType, "Other", None) 
            }

        if self._enabled:
            self._add_log("🚀 插件已启动，开始监听底层消息通道...")
            self._patch_plugin_base()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_render_mode(self) -> Tuple[str, str]:
        return "vue", "dist/assets"

    def _add_log(self, msg: str):
        if not hasattr(self, '_intercept_logs'):
            self._intercept_logs = []
        now = time.strftime("%H:%M:%S", time.localtime())
        self._intercept_logs.insert(0, f"[{now}] {msg}")
        if len(self._intercept_logs) > 50:
            self._intercept_logs.pop()

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/plugins",
                "endpoint": self.api_get_plugins,
                "methods": ["GET"],
                "auth": "bear",  
                "summary": "获取已安装插件列表"
            },
            {
                "path": "/logs",
                "endpoint": self.api_get_logs,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取当前拦截日志"
            }
        ]

    def api_get_plugins(self) -> Dict[str, Any]:
        plugin_names = set()
        try:
            try:
                from app.core.plugin import PluginManager
            except ImportError:
                from app.plugins import PluginManager
            pm = PluginManager()
            plugins = []
            if hasattr(pm, 'get_local_plugins'):
                plugins = pm.get_local_plugins() or []
            elif hasattr(pm, 'get_plugins'):
                raw_plugins = pm.get_plugins()
                plugins = list(raw_plugins.values()) if isinstance(raw_plugins, dict) else raw_plugins
            for p in plugins:
                name = getattr(p, 'plugin_name', getattr(p, 'id', ''))
                if name and name != self.plugin_name:
                    plugin_names.add(name)
        except Exception as e:
            self._add_log(f"读取系统插件列表失败: {e}")
        return {"success": True, "data": sorted(list(plugin_names))}

    def api_get_logs(self) -> Dict[str, Any]:
        return {
            "success": True,
            "data": {
                "rules": self._plugin_mapping,
                "logs": self._intercept_logs
            }
        }

    def _patch_plugin_base(self):
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
                        if 'mtype' in kwargs:
                            kwargs['mtype'] = target_mtype
                            replaced = True
                        else:
                            new_args = list(args)
                            for i, arg in enumerate(new_args):
                                if NotificationType and isinstance(arg, type(target_mtype)):
                                    new_args[i] = target_mtype
                                    args = tuple(new_args)
                                    replaced = True
                                    break
                                    
                        if not replaced:
                            kwargs['mtype'] = target_mtype
                            replaced = True
                            
                        if replaced:
                            self._add_log(f"✅ 拦截成功 | {plugin_id} | 类型已变更为 [{target_type_str}]")
                    else:
                        self._add_log(f"❌ 无法识别 | 类型 [{target_type_str}] 不支持")
                else:
                    pass 

            except Exception as e:
                self._add_log(f"❌ 拦截异常: {str(e)}\n{traceback.format_exc()}")

            return _PluginBase.original_post_message(plugin_self, *args, **kwargs)

        _PluginBase.post_message = hooked_post_message

    def stop_service(self):
        if hasattr(_PluginBase, 'original_post_message'):
            _PluginBase.post_message = _PluginBase.original_post_message
            delattr(_PluginBase, 'original_post_message')
            self._add_log("🛑 插件已停用，底层通道已还原。")
