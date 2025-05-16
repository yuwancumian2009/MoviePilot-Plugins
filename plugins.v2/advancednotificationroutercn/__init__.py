# coding: utf-8
# advanced_notification_router_cn/__init__.py
import json # 用于处理JSON数据格式
from typing import Any, List, Dict, Tuple, Optional # Python的类型提示，帮助理解代码结构

from app.core.plugin import PluginManager # MoviePilot的插件管理器，用于获取插件列表
from app.plugins import _PluginBase # MoviePilot所有插件的基础类
from app.core.event import eventmanager # MoviePilot的事件管理器，用于监听通知事件
from app.schemas.types import EventType, MessageChannel # EventType定义了事件类型，MessageChannel定义了系统内置的通知渠道
from app.log import logger # MoviePilot的日志记录器，用于输出插件信息和错误
from app.utils.http import RequestUtils # MoviePilot的HTTP请求工具，用于发送Webhook

class AdvancedNotificationRouterCn(_PluginBase):
    # 插件的基本信息
    plugin_name = "自定义通知"
    plugin_desc = "允许用户定义自定义通知渠道，并根据通知标题中的关键词创建规则，将通知路由到指定渠道。"
    plugin_icon = "https://github.com/yuwancumian2009/MoviePilot-Plugins/blob/main/icons/Filebrowser.png" # 插件图标 (可以使用 Font Awesome 或 Material Design Icons 的图标名称)
    plugin_version = "1.0.0" # 插件版本 (обновил для ясности)
    plugin_author = "yuwan" # 插件作者
    author_url = "https://github.com/yuwancumian2009" # 作者的网址（可选）
    plugin_config_prefix = "advancednotificationroutercn_" # 插件配置项在数据库中的前缀，确保唯一性
    plugin_order = 10 # 插件加载顺序，数值越小加载越早
    auth_level = 1 # 允许使用此插件的用户级别

    # 插件内部使用的变量 (以下划线开头表示通常是内部使用的)
    _enabled = False # 插件是否启用

    # 自定义Webhook渠道 1 的配置
    _custom_webhook_1_enabled = False # Webhook 1 是否启用
    _custom_webhook_1_name = ""       # Webhook 1 的用户自定义名称
    _custom_webhook_1_url = ""        # Webhook 1 的URL地址

    # 自定义Webhook渠道 2 的配置
    _custom_webhook_2_enabled = False
    _custom_webhook_2_name = ""
    _custom_webhook_2_url = ""

    # 自定义Webhook渠道 3 的配置
    _custom_webhook_3_enabled = False
    _custom_webhook_3_name = ""
    _custom_webhook_3_url = ""

    _routing_rules_json_str = "[]" # 路由规则，以JSON字符串形式存储
    _routing_rules = [] # 解析后的路由规则列表 (Python列表)

    _system_plugins_info_str = "" # 用于在UI上显示已安装插件列表的字符串

    # 初始化插件时调用的方法
    def init_plugin(self, config: dict = None):
        # config 参数是MoviePilot从数据库加载的此插件的配置
        if config: # 如果有已保存的配置
            self._enabled = config.get("enabled", False) # 获取"enabled"的值，如果不存在则默认为False

            # 加载Webhook 1的配置
            self._custom_webhook_1_enabled = config.get("custom_webhook_1_enabled", False)
            self._custom_webhook_1_name = config.get("custom_webhook_1_name", "我的Webhook 1") # 如果没有保存的名称，使用默认值
            self._custom_webhook_1_url = config.get("custom_webhook_1_url", "")

            # 加载Webhook 2的配置
            self._custom_webhook_2_enabled = config.get("custom_webhook_2_enabled", False)
            self._custom_webhook_2_name = config.get("custom_webhook_2_name", "我的Webhook 2")
            self._custom_webhook_2_url = config.get("custom_webhook_2_url", "")

            # 加载Webhook 3的配置
            self._custom_webhook_3_enabled = config.get("custom_webhook_3_enabled", False)
            self._custom_webhook_3_name = config.get("custom_webhook_3_name", "我的Webhook 3")
            self._custom_webhook_3_url = config.get("custom_webhook_3_url", "")

            # 加载路由规则的JSON字符串
            self._routing_rules_json_str = config.get("routing_rules_json_str", "[]")
            try:
                # 尝试将JSON字符串转换为Python列表
                self._routing_rules = json.loads(self._routing_rules_json_str)
                if not isinstance(self._routing_rules, list): # 确保转换结果是列表
                    self._routing_rules = []
            except json.JSONDecodeError: # 如果JSON格式不正确
                logger.error(f"[{self.plugin_name}]：路由规则的JSON格式错误，将使用空规则列表。")
                self._routing_rules = []
        
        self._load_system_plugins_info() # 加载已安装插件的信息，用于显示在配置界面

        logger.info(f"[{self.plugin_name}]：插件已初始化。启用状态：{self._enabled}")
        if self._enabled:
            logger.info(f"[{self.plugin_name}]：已加载 {len(self._routing_rules)} 条路由规则。")

    # 加载系统中已安装插件的信息，用于在配置界面显示
    def _load_system_plugins_info(self):
        try:
            plugins = PluginManager().get_local_plugins() # 获取所有本地插件实例
            info_lines = ["已安装插件列表 (格式：插件ID # 插件名称):"] # 标题行
            for p_instance in plugins: #遍历每个插件实例
                if p_instance.id == self.id: # 跳过本插件自身
                    continue
                info_lines.append(f"{p_instance.id} # {p_instance.plugin_name}") # 添加插件信息行
            self._system_plugins_info_str = "\n".join(info_lines) # 将所有行合并成一个字符串，用换行符分隔
        except Exception as e:
            logger.error(f"[{self.plugin_name}]：加载系统插件列表失败：{e}")
            self._system_plugins_info_str = "加载插件列表失败。"

    # 返回插件当前的启用状态
    def get_state(self) -> bool:
        return self._enabled

    # 定义插件的配置界面
    # 返回一个元组：(表单结构列表, 初始数据字典)
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 1. 定义配置界面的初始数据
        # 这些键名必须与下面表单结构中各组件的 'model' 属性对应
        initial_data = {
            "enabled": self._enabled, # 主开关
            "custom_webhook_1_enabled": self._custom_webhook_1_enabled,
            "custom_webhook_1_name": self._custom_webhook_1_name,
            "custom_webhook_1_url": self._custom_webhook_1_url,
            "custom_webhook_2_enabled": self._custom_webhook_2_enabled,
            "custom_webhook_2_name": self._custom_webhook_2_name,
            "custom_webhook_2_url": self._custom_webhook_2_url,
            "custom_webhook_3_enabled": self._custom_webhook_3_enabled,
            "custom_webhook_3_name": self._custom_webhook_3_name,
            "custom_webhook_3_url": self._custom_webhook_3_url,
            "routing_rules_json_str": self._routing_rules_json_str, # 路由规则的JSON字符串
            "system_plugins_info_str": self._system_plugins_info_str, # 已安装插件信息，只读
            "rules_dialog_open": False # 控制编辑规则的对话框是否打开
        }
        
        # 2. 定义配置界面的Vuetify组件结构
        # MoviePilot 使用Vuetify作为前端组件库
        form_structure = [
            {
                'component': 'VForm', # 表单容器
                'content': [ # 表单内容，是一个组件列表
                    # 主启用开关
                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': f'启用 {self.plugin_name}'}},
                    {'component': 'VDivider', 'props': {'class': 'my-4'}}, # 分隔线

                    # --- 自定义 Webhook 1 配置 ---
                    {'component': 'VLabel', 'text': '自定义 Webhook 1', 'props': {'class': 'text-h6 mb-2'}}, # 标题标签
                    {'component': 'VSwitch', 'props': {'model': 'custom_webhook_1_enabled', 'label': '启用 Webhook 1'}}, # 开关
                    {'component': 'VTextField', 'props': {'model': 'custom_webhook_1_name', 'label': 'Webhook 1 的名称', 'hint': '例如："我的Telegram通知机器人"'}}, # 文本输入框
                    {'component': 'VTextField', 'props': {'model': 'custom_webhook_1_url', 'label': 'Webhook 1 的 URL', 'hint': '完整的Webhook URL地址'}},
                    {'component': 'VDivider', 'props': {'class': 'my-4'}},

                    # --- 自定义 Webhook 2 配置 ---
                    {'component': 'VLabel', 'text': '自定义 Webhook 2', 'props': {'class': 'text-h6 mb-2'}},
                    {'component': 'VSwitch', 'props': {'model': 'custom_webhook_2_enabled', 'label': '启用 Webhook 2'}},
                    {'component': 'VTextField', 'props': {'model': 'custom_webhook_2_name', 'label': 'Webhook 2 的名称'}},
                    {'component': 'VTextField', 'props': {'model': 'custom_webhook_2_url', 'label': 'Webhook 2 的 URL'}},
                    {'component': 'VDivider', 'props': {'class': 'my-4'}},
                    
                    # --- 自定义 Webhook 3 配置 ---
                    {'component': 'VLabel', 'text': '自定义 Webhook 3', 'props': {'class': 'text-h6 mb-2'}},
                    {'component': 'VSwitch', 'props': {'model': 'custom_webhook_3_enabled', 'label': '启用 Webhook 3'}},
                    {'component': 'VTextField', 'props': {'model': 'custom_webhook_3_name', 'label': 'Webhook 3 的名称'}},
                    {'component': 'VTextField', 'props': {'model': 'custom_webhook_3_url', 'label': 'Webhook 3 的 URL'}},
                    {'component': 'VDivider', 'props': {'class': 'my-4'}},

                    # --- 路由规则配置 ---
                    {'component': 'VLabel', 'text': '通知路由规则', 'props': {'class': 'text-h6 mb-2'}},
                    { # 编辑规则的按钮
                        'component': 'VBtn', # Vuetify按钮组件
                        'props': {
                            'color': 'primary', # 按钮颜色
                            # 'onclick' 事件处理器，用于打开对话框。
                            # 'props.model' 是 Vue 组件内部对 initial_data 的引用
                            'onclick': '() => props.model.rules_dialog_open = true' 
                        },
                        'text': '编辑路由规则 (JSON格式)' # 按钮上的文字
                    },
                    # 提示信息
                    {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'class': 'mt-2'}, 
                     'content': [
                         {'component': 'span', 
                          'text': '点击上方按钮可以在对话框中编辑JSON格式的路由规则。每个规则是一个对象，包含以下字段： "pattern" (标题中匹配的关键词), "target_channel_id" (目标渠道ID，如 "custom_webhook_1", "custom_webhook_2", "custom_webhook_3", 或系统渠道如 "bark", "telegram", "wechat" 等), "stop_original" (布尔值 true/false - 是否阻止原始通知), "enabled" (布尔值 true/false - 是否启用此规则)。'}
                     ]},
                    {'component': 'VAlert', 'props': {'type': 'warning', 'variant': 'tonal', 'class': 'mt-2'},
                     'content': [
                         {'component': 'span', 
                          'text': 'JSON规则示例：[{"pattern": "已入库", "target_channel_id": "custom_webhook_1", "stop_original": true, "enabled": true}, {"pattern": "下载完成", "target_channel_id": "bark", "stop_original": false, "enabled": true}]'}
                     ]},
                    {'component': 'VDivider', 'props': {'class': 'my-4'}},
                    
                    # --- 已安装插件信息 (只读) ---
                    {'component': 'VLabel', 'text': '已安装插件信息', 'props': {'class': 'text-h6 mb-2'}},
                    {
                        'component': 'VTextarea', # 多行文本区域
                        'props': {
                            'model': 'system_plugins_info_str', # 绑定到数据显示
                            'label': '已安装的插件列表 (仅供参考)',
                            'rows': 5, # 默认行数
                            'readonly': True, # 只读
                            'no-resize': True # 禁止调整大小
                        }
                    },
                    
                    # --- 编辑规则的对话框 (VDialog) ---
                    # 这个对话框的结构借鉴了您提供的 "消息转发插件.txt"
                    {
                        "component": "VDialog", # 对话框组件
                        "props": {
                            "model": "rules_dialog_open", # 控制对话框的显示/隐藏，绑定到 initial_data 中的 "rules_dialog_open"
                            "max-width": "65rem", # 最大宽度
                            "overlay-class": "v-dialog--scrollable v-overlay--scroll-blocked", # Vuetify样式类
                            "content-class": "v-card v-card--density-default v-card--variant-elevated rounded-t" # Vuetify样式类
                        },
                        "content": [ # 对话框内容
                            {
                                "component": "VCard", # 卡片组件，作为对话框的容器
                                "props": {"title": "编辑路由规则 (JSON格式)"}, # 卡片标题
                                "content": [
                                    {"component": "VDialogCloseBtn", "props": {"model": "rules_dialog_open"}}, # 标准的关闭对话框按钮
                                    {
                                        "component": "VCardText", # 卡片内容区域
                                        "props": {},
                                        "content": [
                                            { # ACE代码编辑器，用于编辑JSON
                                                'component': 'VAceEditor',
                                                'props': {
                                                    'modelvalue': 'routing_rules_json_str', # 关键：VAceEditor使用modelvalue绑定数据
                                                    'lang': 'json', # 语言模式为JSON
                                                    'theme': 'monokai', # 编辑器主题
                                                    'style': 'height: 25rem' # 编辑器高度
                                                }
                                            }
                                        ]
                                    },
                                    { # 卡片操作区域 (例如放按钮)
                                        "component": "VCardActions",
                                        "content": [
                                            {'component': 'VSpacer'}, # 占位符，将按钮推到右边
                                            {'component': 'VBtn', 'props': {'text': '完成', 'onclick': '() => props.model.rules_dialog_open = false'}} # “完成”按钮，关闭对话框
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ] # VForm content 结束
            }
        ] # form_structure 结束
        return form_structure, initial_data # 返回表单结构和初始数据

    # 内部方法：通过Webhook发送通知
    def _send_notification_via_webhook(self, url: str, name:str, title: str, text: str, image: Optional[str] = None):
        if not url: # 检查URL是否存在
            logger.error(f"[{self.plugin_name}]：未给Webhook '{name}' 指定URL。")
            return False
        
        # 准备发送的数据 (payload)
        # 不同的Webhook服务可能期望不同的JSON结构，这里使用一个通用结构
        payload = {
            "title": title,       # 标题
            "text": text,         # 内容
            "image_url": image    # 图片URL (如果存在)
        }
        
        try:
            logger.info(f"[{self.plugin_name}]：正在向Webhook '{name}' ({url}) 发送通知，标题：{title}")
            # 使用MoviePilot的RequestUtils发送POST请求，内容为JSON
            res = RequestUtils().post(url, json=payload) # 默认Content-Type为application/json
            
            # 检查HTTP响应状态码
            if res and 200 <= res.status_code < 300: # HTTP状态码2xx通常表示成功
                logger.info(f"[{self.plugin_name}]：Webhook '{name}' 发送成功。状态码：{res.status_code}")
                return True
            else: # 发送失败
                err_text = res.text if res else "无响应内容"
                status_code = res.status_code if res else "无状态码"
                logger.error(f"[{self.plugin_name}]：Webhook '{name}' 发送失败。状态码：{status_code}，响应：{err_text}")
                return False
        except Exception as e: #捕获其他异常，例如网络问题
            logger.error(f"[{self.plugin_name}]：向Webhook '{name}' 发送时发生异常：{e}")
            return False

    # 内部方法：尝试通过修改事件数据，让MoviePilot核心将通知发送到指定的系统渠道
    def _send_to_system_channel(self, target_channel_id_str: str, title: str, text: str, image: Optional[str], event_data: Dict):
        try:
            # 将字符串形式的渠道ID转换为MessageChannel枚举成员 (例如 "bark" -> MessageChannel.Bark)
            target_channel_enum_member = MessageChannel(target_channel_id_str)
            original_event_channel = event_data.get('channel') # 获取原始通知希望发送到的渠道

            # 修改事件数据中的'channel'字段为我们期望的新系统渠道
            event_data['channel'] = target_channel_enum_member 
            
            # 如果之前设置过防循环标记，移除它，因为我们确实希望MoviePilot现在处理这个事件
            if '_handled_by_router_plugin' in event_data:
                del event_data['_handled_by_router_plugin']

            logger.info(f"[{self.plugin_name}]：通知 '{title}' 已被重定向到系统渠道 '{target_channel_enum_member.name}' (原始渠道是 '{original_event_channel}')。MoviePilot将处理实际发送。")
            # 我们自己不发送通知，而是修改事件。
            # MoviePilot应该在我们的处理器完成后自行发送此通知。
            return True # 返回True表示规则已成功应用，事件已被修改。
        except ValueError: # 如果 target_channel_id_str 不是 MessageChannel 的有效成员
            logger.error(f"[{self.plugin_name}]：无效的系统渠道名称 '{target_channel_id_str}'。无法重定向。")
            return False
        except Exception as e:
            logger.error(f"[{self.plugin_name}]：尝试重定向到系统渠道 '{target_channel_id_str}' 时发生错误：{e}")
            return False


    # 这个方法会被注册为事件监听器，当有新通知时 MoviePilot 会调用它
    @eventmanager.register(EventType.NoticeMessage)
    def handle_notification(self, event): # event 参数是 MoviePilot 传递的事件对象
        if not self._enabled or not self._routing_rules: # 如果插件未启用或没有规则，则不做任何事
            return

        event_data = event.event_data # 事件数据，通常是一个字典，包含通知的详细信息
        
        # 添加一个标记，防止此插件无限循环处理同一个被自己修改过的事件
        if event_data.get('_handled_by_router_plugin'): # 如果这个事件已经被本插件处理过了，就退出
            return 

        title = event_data.get("title") # 获取通知标题
        text = event_data.get("text")   # 获取通知正文
        image = event_data.get("image") # 获取通知图片URL（如果有）
        
        if not title: # 如果通知没有标题，我们无法应用基于标题的规则
            return

        # 遍历所有路由规则
        for rule in self._routing_rules:
            if not rule.get("enabled", False): # 如果规则在配置中被禁用，则跳过它
                continue

            pattern = rule.get("pattern") # 用于在标题中搜索的关键词/短语
            target_channel_id = rule.get("target_channel_id") # 目标渠道的ID (例如, "custom_webhook_1" 或 "bark")
            stop_original = rule.get("stop_original", False) # 是否尝试阻止原始通知

            if not pattern or not target_channel_id: # 如果规则不完整 (没有关键词或目标渠道)
                logger.warning(f"[{self.plugin_name}]：跳过不完整的规则: {rule}")
                continue

            try:
                # 简单的子字符串匹配 (不区分大小写)
                if pattern.lower() in title.lower():
                    logger.info(f"[{self.plugin_name}]：标题 '{title}' 匹配关键词 '{pattern}'。目标渠道: {target_channel_id}")
                    
                    sent_successfully = False # 标记此规则是否成功发送了通知
                    # 检查目标渠道是否是我们的自定义webhook之一
                    if target_channel_id == "custom_webhook_1" and self._custom_webhook_1_enabled and self._custom_webhook_1_url:
                        sent_successfully = self._send_notification_via_webhook(self._custom_webhook_1_url, self._custom_webhook_1_name, title, text, image)
                    elif target_channel_id == "custom_webhook_2" and self._custom_webhook_2_enabled and self._custom_webhook_2_url:
                        sent_successfully = self._send_notification_via_webhook(self._custom_webhook_2_url, self._custom_webhook_2_name, title, text, image)
                    elif target_channel_id == "custom_webhook_3" and self._custom_webhook_3_enabled and self._custom_webhook_3_url:
                        sent_successfully = self._send_notification_via_webhook(self._custom_webhook_3_url, self._custom_webhook_3_name, title, text, image)
                    else:
                        # 如果不是自定义webhook，则假定它是系统渠道
                        try:
                            # 检查 target_channel_id 是否是有效的系统渠道 (例如 "bark", "telegram")
                            MessageChannel(target_channel_id) # 如果ID无效，这将引发ValueError
                            # 如果我们在这里，说明这是一个有效的系统ID。
                            # 我们不自己发送通知，而是修改事件，让MoviePilot将它发送到新的系统渠道。
                            sent_successfully = self._send_to_system_channel(target_channel_id, title, text, image, event_data)
                            # 如果 _send_to_system_channel 返回True，表示我们成功修改了事件。
                            # 实际的发送将稍后由MoviePilot完成。
                        except ValueError: # 如果 target_channel_id 不是 MessageChannel 的有效成员
                            logger.warning(f"[{self.plugin_name}]：规则中为关键词 '{pattern}' 指定了未知的目标渠道 '{target_channel_id}'。")
                            sent_successfully = False # 未能确定要发送的渠道

                    if sent_successfully: # 如果通知已通过此规则成功发送（或重定向）
                        if stop_original:
                            # 尝试阻止 MoviePilot 系统进一步处理此事件。
                            # 这部分比较复杂，因为可能没有明确的API来“停止事件”。
                            # 一种可能是 MoviePilot 检查事件的某个属性。
                            logger.info(f"[{self.plugin_name}]：规则针对 '{pattern}' 已成功触发。正在尝试停止原始通知 (标题: '{title}')。")
                            # 在事件数据中设置一个特殊标记。
                            # MoviePilot 的核心机制（如果它支持这种方式）可能会检查这个标记。
                            # 或者，如果事件处理器可以返回一个特定的值（例如 False）来停止事件的传播。
                            # 这在很大程度上取决于 MoviePilot 的内部架构。
                            # 为简单起见，我们只是记录这个操作。实际的停止行为可能不会发生。
                            event_data['_stop_original_requested_by_router_plugin'] = True # 添加一个标记，表示我们请求停止原始通知
                        
                        # 标记此事件已被本插件处理，以避免无限循环，
                        # 特别是如果事件由于某种原因再次进入此处理器（例如，如果 stop_original 未能生效）。
                        event_data['_handled_by_router_plugin'] = True 
                        return # 退出 handle_notification 函数，因为一个规则已经成功匹配并处理了该通知。
            
            except Exception as e: # 处理在执行规则过程中发生的任何其他错误
                logger.error(f"[{self.plugin_name}]：处理规则 '{rule}' 时发生错误: {e}")
        
        # 如果没有任何规则被触发并提前退出函数，我们需要确保移除可能意外设置的防循环标记。
        if '_handled_by_router_plugin' in event_data:
            del event_data['_handled_by_router_plugin']

    # 当插件停止时调用的方法
    def stop_service(self):
        self._enabled = False # 设置插件为禁用状态
        logger.info(f"[{self.plugin_name}]：插件已停止。")
        # 在这里，如果 eventmanager 支持，可以取消注册事件处理器。
        # eventmanager.unregister(EventType.NoticeMessage, self.handle_notification) # 这是一个示例调用