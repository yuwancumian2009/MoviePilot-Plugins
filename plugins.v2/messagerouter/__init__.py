import inspect
import json
import re
import time
import traceback
import requests
import importlib
import asyncio
from datetime import datetime
from typing import Any, List, Dict, Tuple, Optional

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
    plugin_desc = "全面接管并重定向系统消息与插件通知。支持自定义消息类型修改、独立推送到企业微信。全兼容同步与异步(Async)发信架构，杜绝一切漏网之鱼。"
    plugin_icon = "https://raw.githubusercontent.com/yuwancumian2009/MoviePilot-Plugins/main/icons/messagerouter.png"
    plugin_version = "2.2.0" # 引入 Asyncio 异步函数自适应兼容，解除总线频道限制，彻底接管系统官方通知
    plugin_author = "yuwancumian"
    author_url = "https://github.com/yuwancumian2009/MoviePilot-Plugins"
    plugin_config_prefix = "messagerouter_"
    plugin_order = 20
    auth_level = 1

    _enabled = False
    _block_system = False 
    _plugin_mapping_str = ""
    
    _plugin_routes = {}  
    _tokens_cache = {}   
    _intercept_logs = []
    
    _apps_profile_cache = {}
    _apps_profile_last_update = 0

    _send_msg_url = "%s/cgi-bin/message/send?access_token=%s"
    _token_url = "%s/cgi-bin/gettoken?corpid=%s&corpsecret=%s"

    _active_hooks = []
    _pushed_msg_cache = {}

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._intercept_logs = []
        self._plugin_routes = {}
        self._apps_profile_cache = {}
        self._apps_profile_last_update = 0
        self._active_hooks = []
        self._pushed_msg_cache = {}
        
        self._tokens_cache = self.get_data("wecom_tokens") or {}

        if config:
            self._enabled = config.get("enabled", False)
            self._block_system = config.get("block_system", False)
            self._plugin_mapping_str = config.get("plugin_mapping", "")

        if self._plugin_mapping_str:
            for line in str(self._plugin_mapping_str).split('\n'):
                line = line.strip()
                if not line or line.startswith('#'): continue
                parts = line.split(':')
                if len(parts) >= 2:
                    plugin_name = parts[0].strip()
                    target_type = parts[1].strip()
                    target_app = parts[2].strip() if len(parts) > 2 else ""
                    self._plugin_routes[plugin_name] = {"type": target_type, "app": target_app}

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
            self._add_log(f"🚀 插件启动，正在初始化异步全兼容消息路由系统...")
            self._patch_plugin_base()    
            self._patch_event_bus()      
            self._patch_message_utils()  

    def _get_system_wechat_apps(self) -> dict:
        now = time.time()
        if now - self._apps_profile_last_update < 30 and self._apps_profile_cache:
            return self._apps_profile_cache
        apps = {}
        try:
            from app.db.systemconfig_oper import SystemConfigOper
            from app.schemas.types import SystemConfigKey
            notifies = SystemConfigOper().get(SystemConfigKey.Notifications) or []
            
            for conf in notifies:
                conf_dict = conf.dict() if hasattr(conf, "dict") else (conf.model_dump() if hasattr(conf, "model_dump") else (conf if isinstance(conf, dict) else vars(conf)))
                c_type = str(conf_dict.get("type", "")).lower()
                c_name = str(conf_dict.get("name", ""))
                
                if "wechat" in c_type or "wecom" in c_type:
                    config_data = conf_dict.get("config") or conf_dict
                    if isinstance(config_data, str):
                        try: config_data = json.loads(config_data)
                        except: config_data = {}
                            
                    flat_config = {str(k).lower().replace('_', ''): v for k, v in config_data.items()}
                    corpid = config_data.get("WECHAT_CORPID") or flat_config.get("corpid") or flat_config.get("wechatcorpid")
                    secret = config_data.get("WECHAT_APP_SECRET") or flat_config.get("wechatappsecret") or flat_config.get("corpsecret") or flat_config.get("secret")
                    agentid = config_data.get("WECHAT_APP_ID") or flat_config.get("wechatappid") or flat_config.get("agentid") or flat_config.get("appid")
                    
                    if corpid and secret and str(agentid).strip() != "":
                        apps[c_name] = {"corpid": str(corpid), "secret": str(secret), "appid": int(agentid) if str(agentid).isdigit() else str(agentid), "proxy": config_data.get("WECHAT_PROXY") or flat_config.get("wechatproxy") or flat_config.get("proxy") or "https://qyapi.weixin.qq.com"}
            
            self._apps_profile_cache = apps
            self._apps_profile_last_update = now
        except Exception as e: pass
        return apps

    def get_state(self) -> bool: return self._enabled
    @staticmethod
    def get_command() -> List[Dict[str, Any]]: return []
    def get_api(self) -> List[Dict[str, Any]]: return []
    def get_service(self) -> List[Dict[str, Any]]: return []
    def get_render_mode(self) -> Tuple[str, str]: return "vuetify", ""

    def _add_log(self, msg: str):
        if not hasattr(self, '_intercept_logs'): self._intercept_logs = []
        now = time.strftime("%H:%M:%S", time.localtime())
        self._intercept_logs.insert(0, f"[{now}] {msg}")
        if len(self._intercept_logs) > 50: self._intercept_logs.pop()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [{
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VSwitch", "props": {"model": "enabled", "label": "启用高级路由与企微直推"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [{"component": "VSwitch", "props": {"model": "block_system", "label": "直推后阻断系统默认广播 (防止重复通知)"}}]}
                    ]
                },
                {
                    "component": "VRow",
                    "content": [{"component": "VCol", "props": {"cols": 12}, "content": [{"component": "VTextarea", "props": {"model": "plugin_mapping", "label": "高级消息路由映射规则", "rows": 10, "placeholder": "语法：[插件名称/关键字] : [目标消息类型] : [系统微信通知名称]\n\n【配置说明】：\n1. 匹配优先级：精确匹配优先于标题/正文关键字模糊匹配。\n2. 系统通知与特殊插件配置：接管系统官方消息（如添加订阅）或特殊插件时，请直接使用其通知标题中包含的关键字（如“添加订阅”、“115网盘”）。\n3. 执行优先级：若同时配置了通知类型修改和企微通知渠道，系统将优先执行拦截与企微直推。\n\n【配置示例】 (中间不改类型请留空)：\n115网盘::通知1\n添加订阅:其他:通知2\n豆瓣同步:整理入库:"}}]}]
                }
            ]
        }], {
            "enabled": True if self._enabled else False,
            "block_system": True if self._block_system else False,
            "plugin_mapping": str(self._plugin_mapping_str) if self._plugin_mapping_str else ""
        }

    def get_page(self) -> List[dict]:
        if not self._enabled: return [{'component': 'VAlert', 'props': {'type': 'warning', 'text': '插件未启用，请前往配置开启。', 'class': 'mt-5'}}]
        self._apps_profile_last_update = 0 
        current_apps = self._get_system_wechat_apps()
        rules_text = "【当前生效的规则】\n"
        if not self._plugin_routes: rules_text += "暂无规则\n"
        else:
            for plugin, route in self._plugin_routes.items():
                t_type = route.get("type", "")
                t_app = route.get("app", "")
                desc = []
                if t_type and t_type not in ["", "原类型", "不修改"]: desc.append(f"改类型 ➔ [{t_type}]")
                if t_app:
                    if t_app in current_apps: desc.append(f"企微直推 ➔ [{t_app}]")
                    else: desc.append(f"❌ 企微 [{t_app}] 未在系统中找到")
                if not desc: desc.append("无动作")
                rules_text += f" • {plugin} : {' | '.join(desc)}\n"
                
        apps_text = "\n【系统通知提取状态】\n" + (f"✅ 成功获取 {len(current_apps)} 个系统微信通知配置: {', '.join(current_apps.keys())}\n" if current_apps else "⚠️ 警告: 目前未获取到任何微信通知配置。\n")
        logs_text = "【实时路由监控日志 (最近50条)】\n" + ("\n".join(self._intercept_logs) if getattr(self, '_intercept_logs', []) else "暂无日志。")

        return [
            {'component': 'VCard', 'props': {'class': 'mb-4'}, 'content': [{'component': 'VCardText', 'text': rules_text + apps_text, 'props': {'style': 'white-space: pre-wrap; font-size: 15px; font-weight: bold; color: #1976D2;'}}]},
            {'component': 'VCard', 'content': [{'component': 'VCardText', 'text': logs_text, 'props': {'style': 'white-space: pre-wrap; font-family: monospace; max-height: 400px; overflow-y: auto;'}}]}
        ]

    def __get_access_token(self, app_alias: str, current_apps: dict, force: bool = False) -> str:
        app_info = current_apps.get(app_alias)
        if not app_info: return ""
        now_ts = int(time.time())
        if not force and app_alias in self._tokens_cache and self._tokens_cache[app_alias]["expires_at"] > now_ts + 60:
            return self._tokens_cache[app_alias]["access_token"]
        try:
            res = requests.get(self._token_url % (app_info.get("proxy").rstrip('/'), app_info.get("corpid"), app_info.get("secret")), timeout=15)
            if res.status_code == 200 and res.json().get('errcode') == 0:
                self._tokens_cache[app_alias] = {"access_token": res.json().get('access_token'), "expires_at": now_ts + res.json().get('expires_in', 7200)}
                self.save_data("wecom_tokens", self._tokens_cache)
                return self._tokens_cache[app_alias]["access_token"]
        except: pass
        return ""

    def __send_wechat_msg(self, app_alias: str, current_apps: dict, title: str, text: str, image_url: str, userid: str, retry: int = 0):
        app_info = current_apps.get(app_alias)
        token = self.__get_access_token(app_alias, current_apps, force=(retry > 0))
        if not app_info or not token: return

        title_safe = str(title).strip() if title else "系统通知"
        text_safe = str(text).replace("\n\n", "\n").replace('&&', '').strip() if text else ""
        image_url = image_url if image_url and str(image_url).startswith('http') else ""

        req_json = {"touser": userid if userid else "@all", "agentid": app_info.get("appid"), "msgtype": "news" if image_url else "text"}
        if image_url: req_json["news"] = {"articles": [{"title": title_safe, "description": text_safe, "picurl": image_url, "url": ''}]}
        else: req_json["text"] = {"content": f"{title_safe}\n{text_safe}".strip()}

        try:
            res = requests.post(self._send_msg_url % (app_info.get("proxy").rstrip('/'), token), json=req_json, timeout=15)
            if res.status_code == 200:
                if res.json().get('errcode') == 0: self._add_log(f"✅ 直推成功 | 目标: [{app_alias}] | 标题: {title_safe[:20]}...")
                elif res.json().get('errcode') in [42001, 40014] and retry < 2: self.__send_wechat_msg(app_alias, current_apps, title, text, image_url, userid, retry + 1)
                else: self._add_log(f"❌ 企微报错 [{app_alias}]: {res.json().get('errmsg')}")
        except Exception as e: pass

    def _extract_msg_args(self, *args, **kwargs) -> dict:
        result = {"title": "", "text": "", "image": "", "userid": None, "plugin_id": ""}
        def _extract_from_obj(obj):
            try:
                if hasattr(obj, 'title') and hasattr(obj, 'text'):
                    result['title'] = getattr(obj, 'title', '') or ''
                    result['text'] = getattr(obj, 'text', '') or ''
                    result['image'] = getattr(obj, 'image', '') or ''
                    result['userid'] = getattr(obj, 'userid', None)
                    result['plugin_id'] = getattr(obj, 'plugin_id', getattr(obj, 'source', '')) or ''
                    return True
                elif isinstance(obj, dict):
                    msg = obj.get('message')
                    if msg and hasattr(msg, 'title'):
                        result['title'] = getattr(msg, 'title', '') or ''
                        result['text'] = getattr(msg, 'text', '') or ''
                        result['image'] = getattr(msg, 'image', '') or ''
                        result['userid'] = getattr(msg, 'userid', None)
                        result['plugin_id'] = getattr(msg, 'plugin_id', getattr(msg, 'source', '')) or ''
                        return True
                    elif 'title' in obj or 'text' in obj:
                        result['title'] = obj.get('title', '') or ''
                        result['text'] = obj.get('text', '') or ''
                        result['image'] = obj.get('image', '') or ''
                        result['userid'] = obj.get('userid', None)
                        result['plugin_id'] = obj.get('plugin_id', obj.get('source', '')) or ''
                        return True
            except: pass
            return False

        for arg in args:
            if _extract_from_obj(arg): return result
        for v in kwargs.values():
            if _extract_from_obj(v): return result

        result['title'] = kwargs.get('title', '') or ''
        result['text'] = kwargs.get('text', '') or ''
        result['image'] = kwargs.get('image', '') or ''
        result['userid'] = kwargs.get('userid', None)
        result['plugin_id'] = kwargs.get('plugin_id', kwargs.get('source', '')) or ''

        if not result['title'] and args:
            str_args = [a for a in args if isinstance(a, str)]
            if len(str_args) > 0: result['title'] = str_args[0]
            if len(str_args) > 1: result['text'] = str_args[1]
        return result

    def _process_intercept(self, msg_data: dict, args, kwargs, layer_name: str):
        plugin_id_lower = str(msg_data.get('plugin_id') or "").strip().lower()
        title_lower = str(msg_data.get('title') or "").lower()
        text_lower = str(msg_data.get('text') or "").lower()
        
        routes_lower = {k.lower(): v for k, v in self._plugin_routes.items()}
        matched_route_key = None

        if plugin_id_lower and plugin_id_lower in routes_lower:
            matched_route_key = plugin_id_lower
        else:
            for k in routes_lower.keys():
                if (plugin_id_lower and k in plugin_id_lower) or (k in title_lower) or (k in text_lower):
                    matched_route_key = k
                    break

        if matched_route_key:
            route_info = routes_lower[matched_route_key]
            target_app_alias = route_info.get("app", "")
            target_type_str = route_info.get("type", "")

            # 去重：15秒内同标题消息不再重复触发直推和修改
            msg_hash = hash(f"{msg_data['title']}_{msg_data['text']}")
            now = time.time()
            if not hasattr(self, '_pushed_msg_cache'): self._pushed_msg_cache = {}
            self._pushed_msg_cache = {k: v for k, v in self._pushed_msg_cache.items() if now - v < 15}
            already_processed = msg_hash in self._pushed_msg_cache

            current_apps = self._get_system_wechat_apps()
            
            action_taken = False
            is_direct_pushed = False

            # 【通道一】：企微直推
            if target_app_alias and target_app_alias in current_apps:
                is_direct_pushed = True
                if not already_processed:
                    self.__send_wechat_msg(app_alias=target_app_alias, current_apps=current_apps, title=msg_data['title'], text=msg_data['text'], image_url=msg_data['image'], userid=msg_data['userid'])
                    self._add_log(f"🛡️ 命中规则 [{matched_route_key}] ➔ 已接管通知 ({layer_name})")
                    action_taken = True

            # 【通道二】：修改消息类型 (深度递归修改)
            if target_type_str and target_type_str not in ["", "原类型", "不修改"]:
                target_mtype = self._type_map.get(target_type_str)
                if target_mtype is not None and not already_processed:
                    def _change_mtype(obj):
                        changed = False
                        try:
                            if hasattr(obj, 'mtype'): 
                                setattr(obj, 'mtype', target_mtype)
                                changed = True
                        except: pass
                        try:
                            if isinstance(obj, dict):
                                if 'mtype' in obj: 
                                    obj['mtype'] = target_mtype
                                    changed = True
                                if 'message' in obj:
                                    if _change_mtype(obj['message']):
                                        changed = True
                        except: pass
                        return changed

                    type_changed = False
                    for a in args: 
                        if _change_mtype(a): type_changed = True
                    for v in kwargs.values(): 
                        if _change_mtype(v): type_changed = True
                    if 'mtype' in kwargs: 
                        kwargs['mtype'] = target_mtype
                        type_changed = True

                    if type_changed:
                        self._add_log(f"✅ [{layer_name}] 消息类型已修改为 ➔ [{target_type_str}]")
                        action_taken = True

            # 记录缓存状态
            if action_taken and not already_processed:
                self._pushed_msg_cache[msg_hash] = now

            # 【通道三】：拦截原通知 (仅在开启阻断开关且配置了直推时生效)
            if self._block_system and is_direct_pushed:
                if not already_processed:
                    self._add_log(f"🛡️ 阻断广播 | 原消息已被拦截并重置，阻止系统继续推送")
                
                # 物理清空源参数内存
                def _destroy(obj):
                    try:
                        if hasattr(obj, 'title'): setattr(obj, 'title', '')
                        if hasattr(obj, 'text'): setattr(obj, 'text', '')
                        if hasattr(obj, 'targets'): setattr(obj, 'targets', {})
                        if hasattr(obj, 'userid'): setattr(obj, 'userid', None)
                        if hasattr(obj, 'channel'): setattr(obj, 'channel', None)
                    except: pass
                    try:
                        if isinstance(obj, dict):
                            if 'title' in obj: obj['title'] = ''
                            if 'text' in obj: obj['text'] = ''
                            if 'message' in obj: _destroy(obj['message'])
                    except: pass
                
                for a in args: _destroy(a)
                for v in kwargs.values(): _destroy(v)

                return True # 返回 True 阻断底层的后续动作

        return False # 不阻断，带着修改后的类型放行给系统

    def _patch_plugin_base(self):
        try:
            if hasattr(_PluginBase, 'original_post_message'): return
            _PluginBase.original_post_message = _PluginBase.post_message
            def hooked_post_message(plugin_self, *args, **kwargs):
                msg_data = self._extract_msg_args(*args, **kwargs)
                if not msg_data['plugin_id']: msg_data['plugin_id'] = getattr(plugin_self, 'plugin_name', plugin_self.__class__.__name__)
                if self._process_intercept(msg_data, args, kwargs, "常规通道"): return True
                return _PluginBase.original_post_message(plugin_self, *args, **kwargs)
            _PluginBase.post_message = hooked_post_message
        except: pass

    def _patch_event_bus(self):
        try:
            from app.core.event import eventmanager
            publish_method_name = next((m for m in ['send_event', 'publish_event', 'publish'] if hasattr(eventmanager, m)), None)
            if not publish_method_name or hasattr(eventmanager, 'original_publish_event_router'): return
            
            original_publish = getattr(eventmanager, publish_method_name)
            setattr(eventmanager, 'original_publish_event_router', original_publish)
            
            # 兼容异步事件总线 (如果存在的话)
            if asyncio.iscoroutinefunction(original_publish):
                async def hooked_publish_event(*args, **kwargs):
                    try:
                        msg_data = self._extract_msg_args(*args, **kwargs)
                        if msg_data['title'] or msg_data['text']:
                            if self._process_intercept(msg_data, args, kwargs, "异步事件总线"): return True
                    except: pass
                    return await getattr(eventmanager, 'original_publish_event_router')(*args, **kwargs)
                setattr(eventmanager, publish_method_name, hooked_publish_event)
            else:
                def hooked_publish_event(*args, **kwargs):
                    try:
                        # 全盘扫描，不再局限于特定 event_type
                        msg_data = self._extract_msg_args(*args, **kwargs)
                        if msg_data['title'] or msg_data['text']:
                            if self._process_intercept(msg_data, args, kwargs, "事件总线"): return True
                    except: pass
                    return getattr(eventmanager, 'original_publish_event_router')(*args, **kwargs)
                setattr(eventmanager, publish_method_name, hooked_publish_event)
        except: pass

    def _patch_message_utils(self):
        import sys
        hook_count = 0
        
        # 1. 挂载 MoviePilot 官方系统的核心类方法链路 (针对 添加订阅、整理入库 等官方通知)
        class_candidates = [
            ("app.chain.message", ["MessageChain"]),
            ("app.modules.message", ["MessageModule", "Message"]), 
            ("app.utils.message", ["Message", "MessageUtils"]),
            ("app.helper.message", ["MessageHelper", "Message"]), 
            ("app.core.message", ["Message", "MessageManager"]),
            ("app.core.notification", ["Notification"]),
            ("app.helper.notification", ["NotificationHelper"]),
            ("app.modules.telegram", ["TelegramModule", "Telegram"]),
            ("app.modules.wechat", ["WechatModule", "Wechat"]),
            ("app.modules.bark", ["BarkModule", "Bark"])
        ]
        methods_to_find = ['process', 'add_message', 'send_message', 'send_msg', 'send', 'post_message', 'notify', 'put']
        
        for mod_name, class_names in class_candidates:
            try:
                mod = importlib.import_module(mod_name)
                for cls_name in class_names:
                    if hasattr(mod, cls_name):
                        target_class = getattr(mod, cls_name)
                        for method_name in methods_to_find:
                            if hasattr(target_class, method_name) and callable(getattr(target_class, method_name)):
                                self._apply_deep_hook(target_class, method_name, mod_name, is_module=False)
                                hook_count += 1
            except Exception: pass

        # 2. 挂载全局裸函数模块 (针对野生插件)
        for mod_name, mod in list(sys.modules.items()):
            if not (mod_name.startswith('app.') or mod_name.startswith('plugins.')): continue
            if 'messagerouter' in mod_name.lower(): continue
            
            for func_name in ['post_message', 'send_message', 'send_msg']:
                if hasattr(mod, func_name) and callable(getattr(mod, func_name)):
                    self._apply_deep_hook(mod, func_name, mod_name, is_module=True)
                    hook_count += 1
                    
        self._add_log(f"✅ 底层路由开启：已成功挂载 {hook_count} 个系统与插件发信枢纽")

    def _apply_deep_hook(self, target_obj, method_name, mod_name, is_module=False):
        hook_attr_name = f"_original_{method_name}_router"
        if hasattr(target_obj, hook_attr_name): return
        
        original_method = getattr(target_obj, method_name)
        setattr(target_obj, hook_attr_name, original_method)
        
        if not hasattr(self, '_active_hooks'): self._active_hooks = []
        self._active_hooks.append((target_obj, method_name, hook_attr_name))
        
        # 智能探测并兼容异步方法 (asyncio)
        if asyncio.iscoroutinefunction(original_method):
            async def hooked_send_msg_async(*args, **kwargs):
                try:
                    msg_data = self._extract_msg_args(*args, **kwargs)
                    if msg_data['title'] or msg_data['text']:
                        display_name = f"{mod_name}.{method_name}" if is_module else f"{target_obj.__name__}.{method_name}"
                        if self._process_intercept(msg_data, args, kwargs, f"底层异步模块: {display_name}"):
                            return True
                except: pass
                return await getattr(target_obj, hook_attr_name)(*args, **kwargs)
            setattr(target_obj, method_name, hooked_send_msg_async)
        else:
            def hooked_send_msg_sync(*args, **kwargs):
                try:
                    msg_data = self._extract_msg_args(*args, **kwargs)
                    if msg_data['title'] or msg_data['text']:
                        display_name = f"{mod_name}.{method_name}" if is_module else f"{target_obj.__name__}.{method_name}"
                        if self._process_intercept(msg_data, args, kwargs, f"底层模块: {display_name}"):
                            return True
                except: pass
                return getattr(target_obj, hook_attr_name)(*args, **kwargs)
            setattr(target_obj, method_name, hooked_send_msg_sync)

    def stop_service(self):
        try:
            if hasattr(_PluginBase, 'original_post_message'):
                _PluginBase.post_message = _PluginBase.original_post_message
                delattr(_PluginBase, 'original_post_message')
        except: pass
        try:
            from app.core.event import eventmanager
            if hasattr(eventmanager, 'original_publish_event_router'):
                for method in ['send_event', 'publish_event', 'publish']:
                    if hasattr(eventmanager, method):
                        setattr(eventmanager, method, eventmanager.original_publish_event_router)
                        break
                delattr(eventmanager, 'original_publish_event_router')
        except: pass
        try:
            if hasattr(self, '_active_hooks'):
                for target_obj, method_name, hook_attr_name in self._active_hooks:
                    if hasattr(target_obj, hook_attr_name):
                        setattr(target_obj, method_name, getattr(target_obj, hook_attr_name))
                        delattr(target_obj, hook_attr_name)
                self._active_hooks = []
        except: pass
        self._add_log("🛑 插件已停用，所有拦截路由已安全撤销。")
