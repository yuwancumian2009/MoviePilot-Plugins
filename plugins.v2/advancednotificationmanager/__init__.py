# 概念性插件代码结构

# 从 MoviePilot 导入必要的模块
# (基于您提供的示例插件)
from app.plugins import _PluginBase
from app.core.plugin import PluginManager  # 用于列出插件
from app.core.event import eventmanager  # 用于事件处理
from app.schemas.types import EventType, NotificationType  # 可能用到的类型定义
from app.log import logger  # 用于日志记录
# from app.utils.http import RequestUtils # 如果自定义渠道需要发送HTTP请求（例如Webhook）

import json  # 用于处理复杂的配置数据，例如列表/字典

class AdvancedNotificationManager(_PluginBase):
    plugin_name = "自定义通知"
    plugin_desc = "允许自定义通知渠道，并为每个插件指定其通知的发送方式。"
    plugin_icon = "https://github.com/yuwancumian2009/MoviePilot-Plugins/blob/main/icons/Filebrowser.png"  # 替换为您的插件图标URL
    plugin_version = "1.0.0"
    plugin_author = "yuwan"  # 替换为您的名字
    author_url = "https://github.com/yuwancumian2009"  # 替换
    plugin_config_prefix = "advancednotificationmanager_"  # 插件配置项ID前缀
    plugin_order = 99  # 插件加载顺序，建议较后加载，以便能拦截其他插件的通知
    auth_level = 1  # 可使用的用户级别

    # 私有属性
    _enabled = False  # 插件启用状态
    # 存储自定义渠道配置: 列表，每个元素是字典，例如 {'id': 'unique_id', 'name': '用户自定义渠道名', 'type': 'webhook', 'details': {'url': '...'}}
    _custom_channels_config = []
    # 存储插件与渠道的映射关系: 字典，例如 {'某个插件的ID': '自定义渠道ID或系统渠道名'}
    _plugin_mappings_config = {}

    def init_plugin(self, config: dict = None):
        """
        初始化插件并加载其配置。
        """
        if not config:
            config = {}  # 确保config是一个字典

        self._enabled = config.get("enabled", False)  # 从配置中读取启用状态，默认为False

        # 加载自定义渠道配置
        # 如果在VTextarea中管理，配置通常存储为JSON字符串
        custom_channels_str = config.get("custom_channels", "[]")
        try:
            self._custom_channels_config = json.loads(custom_channels_str)
            if not isinstance(self._custom_channels_config, list):  # 基本验证
                self._custom_channels_config = []
                logger.warning(f"{self.plugin_name}: 自定义渠道配置格式无效（不是列表），已重置为空列表。")
        except json.JSONDecodeError:
            self._custom_channels_config = []
            logger.error(f"{self.plugin_name}: 解析自定义渠道配置 (custom_channels) 的JSON时失败。")

        # 加载插件到渠道的映射配置
        plugin_mappings_str = config.get("plugin_mappings", "{}")
        try:
            self._plugin_mappings_config = json.loads(plugin_mappings_str)
            if not isinstance(self._plugin_mappings_config, dict):  # 基本验证
                self._plugin_mappings_config = {}
                logger.warning(f"{self.plugin_name}: 插件映射配置格式无效（不是字典），已重置为空字典。")
        except json.JSONDecodeError:
            self._plugin_mappings_config = {}
            logger.error(f"{self.plugin_name}: 解析插件映射配置 (plugin_mappings) 的JSON时失败。")

        if self._enabled:
            logger.info(f"{self.plugin_name} 插件已启用。")
            # 如果插件启用，注册事件监听器
            # 假设有一个通用的通知事件可以被拦截。
            # WeChatForward 插件使用了 EventType.NoticeMessage。
            # 需要确认这是否是拦截所有插件通知并能在它们被系统默认处理器分发前捕获的正确事件。
            eventmanager.register(EventType.NoticeMessage, self.handle_notification_event)
        else:
            logger.info(f"{self.plugin_name} 插件已禁用。")

    def get_state(self) -> bool:
        """
        获取插件的启用状态。
        :return: 插件是否启用 (True/False)
        """
        return self._enabled

    def get_form(self) -> tuple[list[dict], dict[str, any]]:
        """
        定义插件的配置页面UI。
        需要返回两块数据：1、页面配置 (UI布局)；2、数据结构 (表单的初始值)。
        """
        # 1. 获取所有已安装的插件列表
        all_installed_plugins = []
        try:
            # 类似于 PluginReOrder (插件自定义排序) 插件的做法
            local_plugins = PluginManager().get_local_plugins()
            all_installed_plugins = [{"id": p.id, "name": p.plugin_name} for p in local_plugins if p.installed]
        except Exception as e:
            logger.error(f"{self.plugin_name}: 获取已安装插件列表失败: {e}")

        # 2. 为每个插件的渠道选择准备选项
        # 系统渠道 - 这部分是假设性的。MoviePilot 可能有方法列出它们，
        # 或者它们是预定义的 (例如, "default", "email", "wechat")。
        # 此示例假设有几个已知的系统渠道，并加上用户定义的渠道。
        system_channel_options = [
            {"value": "system_default", "text": "系统默认通知"},
            # 可以添加其他已知的系统渠道，如果可以识别的话，例如:
            # {"value": "system_email", "text": "系统邮件"},
            # {"value": "system_wechat", "text": "系统微信"},
        ]
        user_defined_channel_options = [
            {"value": ch.get('id', ''), "text": ch.get('name', '未命名渠道')}
            for ch in self._custom_channels_config if ch.get('id') and ch.get('name') # 确保id和name存在
        ]
        combined_channel_options = system_channel_options + user_defined_channel_options

        # --- UI 结构 ---
        # 这是一个简化版的表单结构描述。
        # 实际实现中，会像示例插件那样使用 MoviePilot 的 VForm 组件 (VRow, VCol, VSelect 等)。
        # 由于这部分代码会很长，这里主要描述表单应包含的内容。

        # 定义自定义渠道的UI部分:
        # - 一个区域，使用 VTextarea (或者如果可能，使用更结构化的列表编辑器)
        #   来定义自定义渠道。每个渠道可能包含: 名称, 类型 (例如 Webhook), 以及详细信息 (例如 URL)。
        #   为简单起见，我们假设 `custom_channels` 是一个在 VTextarea 中管理的 JSON 字符串。
        #   示例: [{"id": "ch1", "name": "Webhook 提醒", "type": "webhook", "details": {"url": "http://..."}}, ...]

        # 将插件映射到渠道的UI部分:
        # - 一个所有已安装插件的列表/表格。
        # - 对每个插件，提供一个 VSelect 下拉菜单，包含 `combined_channel_options` 中的所有选项。
        #   `plugin_mappings` 配置会存储 {'插件ID': '选择的渠道ID或系统渠道名'}。
        #   为简单起见，这里也假设它是在 VTextarea 中管理的 JSON 字符串。

        ui_layout = [
            {
                'component': 'VForm',
                'content': [
                    # 标准的启用插件开关
                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}},
                    
                    {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 
                     'text': '您可以在此配置自定义的通知渠道，并为系统中的每个插件指定其发送通知时所使用的渠道。'}},

                    # 自定义通知渠道配置区域
                    {'component': 'VLabel', 'text': '自定义通知渠道配置 (JSON格式):'},
                    {'component': 'VTextarea', 'props': {
                        'model': 'custom_channels', 'label': '渠道定义列表', 'rows': 6,  # 调整行数以适应内容
                        'placeholder': '[{"id": "my_unique_channel_id", "name": "我的Webhook通知", "type": "webhook", "details": {"url": "https://example.com/webhook"}}, ...]'
                    }},
                    {'component': 'VAlert', 'props': {'type': 'info', 'dense': True, 'text': '每个渠道都需要一个唯一的 "id" (英文或数字，不要包含特殊字符), 一个 "name" (显示名称), "type" (例如 "webhook"), 以及 "details" (例如 {"url": "https://..."}).'}},

                    # 插件到渠道的映射配置区域
                    {'component': 'VLabel', 'text': '插件通知渠道映射 (JSON格式):'},
                    {'component': 'VTextarea', 'props': {
                        'model': 'plugin_mappings', 'label': '插件映射配置', 'rows': 8, # 调整行数
                        'placeholder': '{"PluginId1": "custom_channel_id_or_system_default", "AnotherPluginID": "system_email", ...}'
                    }},
                     {'component': 'VAlert', 'props': {'type': 'info', 'dense': True, 'text': '请使用下面列出的插件ID，以及您在上面定义的自定义渠道的ID，或者系统渠道的名称 (例如 system_default)。'}},

                    # 显示可用的插件和渠道选项，供用户参考
                    {'component': 'VExpansionPanels', 'props': {'class': 'mt-4'}, 'content': [ # 添加上边距
                        {'component': 'VExpansionPanel', 'content': [
                            {'component': 'VExpansionPanelTitle', 'text': '可用插件列表 (供参考)'},
                            {'component': 'VExpansionPanelText', 'content': [
                                {'component': 'VList', 'props': {'dense': True}, 'items': [ # 使用 dense 使列表更紧凑
                                    f"{p['name']} (ID: {p['id']})" for p in all_installed_plugins
                                ]}
                            ]}
                        ]},
                        {'component': 'VExpansionPanel', 'content': [
                            {'component': 'VExpansionPanelTitle', 'text': '当前可用渠道选项 (供参考)'},
                            {'component': 'VExpansionPanelText', 'content': [
                                {'component': 'VList', 'props': {'dense': True}, 'items': [
                                    f"{ch['text']} (实际值/ID: {ch['value']})" for ch in combined_channel_options
                                ]}
                            ]}
                        ]}
                    ]}
                ]
            }
        ]

        # 表单的初始数据
        initial_data = {
            "enabled": self._enabled,
            # 使用 ensure_ascii=False 以正确显示中文字符, indent=2 使JSON更易读
            "custom_channels": json.dumps(self._custom_channels_config, indent=2, ensure_ascii=False),
            "plugin_mappings": json.dumps(self._plugin_mappings_config, indent=2, ensure_ascii=False)
        }
        return ui_layout, initial_data

    def handle_notification_event(self, event):
        """
        处理传入的通知事件，以拦截并重定向它们。
        """
        if not self._enabled:
            return # 如果插件未启用，则不执行任何操作

        # 从事件中提取通知数据
        # `event.event_data` 的结构需要根据 MoviePilot 发送通知的方式来确定。
        # WeChatForward 插件的示例中获取了: title, text, image, userid, channel
        event_data = getattr(event, 'event_data', {}) # 安全地获取event_data
        if not isinstance(event_data, dict): # 确保event_data是字典
            return


        # 关键步骤: 识别此通知的来源插件。
        # 这个信息必须在事件数据中可用。
        # 让我们假设 `event.event_data` 中可能包含一个 `source_plugin_id`。
        # 如果这个信息不可用，此插件的核心概念将难以实现。
        # --- 这个部分高度依赖于 MoviePilot 事件系统的具体细节 ---
        source_plugin_id = event_data.get("source_plugin_id")  # 假设的键名 - 这是必需的

        if not source_plugin_id:
            # logger.debug(f"{self.plugin_name}: 通知事件中未找到 source_plugin_id，跳过处理。")
            return # 如果无法确定来源插件，则无法处理

        original_title = event_data.get("title", "通知") # 获取原始标题，提供默认值
        original_text = event_data.get("text", "")   # 获取原始文本
        # original_channel = event_data.get("channel") # 原始的目标渠道

        # 从配置中查找此插件被分配到的渠道ID或系统渠道名
        assigned_channel_key = self._plugin_mappings_config.get(source_plugin_id)

        if not assigned_channel_key:
            # logger.debug(f"{self.plugin_name}: 插件 {source_plugin_id} 没有特定的渠道映射，允许默认处理。")
            return # 没有为此插件进行特定映射，让通知按原计划通过原始渠道或默认机制处理

        # --- 拦截逻辑 ---
        # 如果我们正在处理此通知，需要尝试阻止原始通知事件的默认行为。
        # 如何做到这一点取决于 MoviePilot 的事件系统 (例如，调用 event.stop_propagation() 或从处理器返回 False)。
        # 目前，我们假设需要明确地表示事件已被处理。
        # 这是一个关键部分：如果我们自己处理通知，必须阻止原始通知的发送。
        # 例如: if hasattr(event, 'prevent_default'): event.prevent_default()
        # 或者: if hasattr(event, 'stop_propagation'): event.stop_propagation()

        logger.info(f"{self.plugin_name}: 已拦截来自插件 {source_plugin_id} 的通知，准备通过渠道 '{assigned_channel_key}' 处理。")


        # 检查分配的是否是系统渠道 (我们应该直接放行，或者我们的映射意味着“使用这个特定的系统渠道”)
        if assigned_channel_key.startswith("system_"):
            # 如果映射到系统渠道，我们可能需要修改事件的目标渠道（如果MoviePilot的事件系统允许），
            # 或者如果映射意味着“对于这个特定的系统渠道不进行覆盖”，则直接放行。
            # 目前，假设 "system_default" 意味着不干预。
            # 如果 assigned_channel_key 是例如 "system_email"，我们可能希望确保 event.data.channel 被设置为 'email'。
            # 这部分需要更多关于 MoviePilot 如何处理 `EventType.NoticeMessage` 及其 `channel` 字段的信息。
            logger.info(f"{self.plugin_name}: 插件 {source_plugin_id} 被映射到系统渠道 {assigned_channel_key}。具体操作取决于MoviePilot内部机制。")
            # 潜在的操作：
            # event_data["channel"] = assigned_channel_key.replace("system_", "") # 例如，将 "system_email" 改为 "email"
            # 如果我们已经阻止了原始事件，并且现在想通过特定的系统通道重新发送它：
            # self.post_message(mtype=..., title=original_title, text=original_text, channel=event_data["channel"])
            return # 暂时直接返回，表示让系统处理或需要更复杂的重定向逻辑

        # 分配的是我们插件定义的一个自定义渠道
        custom_channel_definition = next((ch for ch in self._custom_channels_config if ch.get('id') == assigned_channel_key), None)

        if not custom_channel_definition:
            logger.warning(f"{self.plugin_name}: 插件 {source_plugin_id} 被映射到一个未知的自定义渠道ID '{assigned_channel_key}'。")
            return

        channel_name = custom_channel_definition.get("name", "未命名自定义渠道")
        channel_type = custom_channel_definition.get("type")
        channel_details = custom_channel_definition.get("details", {}) # 获取渠道的详细配置，例如URL

        logger.info(f"{self.plugin_name}: 正在为插件 {source_plugin_id} 通过自定义渠道 '{channel_name}' (类型: {channel_type}) 处理通知。")

        # 根据 custom_channel_definition['type'] 处理通知
        if channel_type == 'webhook':
            webhook_url = channel_details.get('url')
            if webhook_url:
                # 实现 Webhook 发送逻辑 (例如，使用 RequestUtils，像 cf订阅 插件那样)
                # from app.utils.http import RequestUtils # 如果未在顶部导入，则在此处导入
                payload = {
                    'source_plugin_id': source_plugin_id,
                    'title': original_title,
                    'text': original_text,
                    'custom_channel_name': channel_name # 可以附加一些额外信息
                }
                try:
                    # RequestUtils().post(webhook_url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers={'Content-Type': 'application/json; charset=utf-8'})
                    logger.info(f"已通过 Webhook渠道 '{channel_name}' 为插件 {source_plugin_id} 发送通知。")
                except Exception as e:
                    logger.error(f"发送通知到 Webhook渠道 '{channel_name}' (URL: {webhook_url}) 失败: {e}")
                # 注意：这里需要实际的发送代码，上面是注释掉的示例。
            else:
                logger.error(f"{self.plugin_name}: Webhook类型的自定义渠道 '{channel_name}' 未配置URL。")
        
        # elif channel_type == '转发到系统邮件并加前缀':
            # new_title = f"[{channel_name}] {original_title}"
            # self.post_message(mtype=NotificationType.Email, title=new_title, text=original_text) # 假设 NotificationType.Email 是邮件类型
            # logger.info(f"已将来自 {source_plugin_id} 的通知通过邮件渠道 '{channel_name}' (添加前缀后) 发送。")

        # 你可以在这里添加对其他自定义渠道类型的处理逻辑
        # elif custom_channel_definition['type'] == 'some_other_handler_type':
            # pass

        else:
            logger.warning(f"{self.plugin_name}: 自定义渠道 '{channel_name}' 的处理器类型 '{channel_type}' 未知或未实现。")
        
        # 重要提示：事件处理机制需要允许此插件有效地“消费”或“重定向”通知。
        # 如果 EventType.NoticeMessage 只是一个事后通知的事件，那么这种拦截和重定向的方法将无法工作。
        # 它需要是一个在通知分发到原始渠道之前发生的事件。

    def stop_service(self):
        """
        退出插件时的清理操作。
        """
        try:
            # 注销事件监听器
            # 需要确认 MoviePilot 的 eventmanager是如何注销事件的。示例插件中未显示注销操作。
            # 可能是自动的，或者需要特定的方法。
            if self._enabled:
                # eventmanager.unregister(EventType.NoticeMessage, self.handle_notification_event) # 检查 MoviePilot API 文档
                pass # 暂时留空，因为不确定如何正确注销
            logger.info(f"{self.plugin_name} 插件已停止。")
        except Exception as e:
            logger.error(f"{self.plugin_name} 退出插件失败：%s" % str(e))

    # 其他用于通过新的自定义渠道类型发送通知的辅助方法可以放在这里。
    # def send_via_my_custom_type(self, details, title, text):
    #     pass
