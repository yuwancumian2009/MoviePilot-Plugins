import json
from typing import Any, List, Dict, Tuple, Optional

from app.core.plugin import PluginManager # 用于获取插件列表
from app.plugins import _PluginBase
from app.core.event import eventmanager
from app.schemas.types import EventType, MessageChannel # MessageChannel 可以看到系统已有的渠道类型
from app.log import logger
from app.utils.http import RequestUtils # 如果自定义渠道需要发HTTP请求

# MoviePilot 的通知服务，可能需要导入后调用其发送方法
# from app.core.notification import NotificationService # (假设有这样一个服务)
# 或者直接调用全局的通知发送函数
# from app.helper.pusher import GlobalPusher # (假设有这样一个推送器)


class AdvancedNotificationRouter(_PluginBase):
    # 插件名称
    plugin_name = "自定义通知渠道"
    # 插件描述
    plugin_desc = "允许自定义通知渠道，并为每个插件指定特定的通知发送渠道。"
    # 插件图标 (可以自行替换为一个合适的图标URL或文件名)
    plugin_icon = "mdi-router-network"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "yuwan" # 替换为您的名字
    # 作者主页
    author_url = "https://github.com/YourGitHub" # 替换为您的GitHub
    # 插件配置项ID前缀
    plugin_config_prefix = "advancednotificationrouter_"
    # 加载顺序，建议较高，以便在其他插件发送通知前加载和注册事件
    plugin_order = 5 # 或更小的值，确保先于多数插件加载
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _custom_channels_config = [] # 存储自定义渠道的配置
    _plugin_channel_mapping = {} # 存储插件ID到渠道ID的映射
    _all_plugins_cache = [] # 缓存插件列表，用于UI

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            # 注意：从配置中读取的 JSON 字符串需要解析
            custom_channels_str = config.get("custom_channels_config_str", "[]")
            try:
                self._custom_channels_config = json.loads(custom_channels_str)
            except json.JSONDecodeError:
                logger.error(f"{self.plugin_name}: 自定义渠道配置格式错误，请检查JSON。")
                self._custom_channels_config = []

            plugin_mapping_str = config.get("plugin_channel_mapping_str", "{}")
            try:
                self._plugin_channel_mapping = json.loads(plugin_mapping_str)
            except json.JSONDecodeError:
                logger.error(f"{self.plugin_name}: 插件渠道映射配置格式错误，请检查JSON。")
                self._plugin_channel_mapping = {}

        # 预加载插件列表以供UI使用
        self._load_all_plugins_for_ui()
        logger.info(f"{self.plugin_name} 初始化完成。启用状态: {self._enabled}")
        if self._enabled:
            logger.info(f"自定义渠道数量: {len(self._custom_channels_config)}")
            logger.info(f"插件路由规则数量: {len(self._plugin_channel_mapping)}")


    def _load_all_plugins_for_ui(self):
        """加载系统中的所有插件信息，用于配置界面的下拉列表"""
        try:
            # 注意: PluginManager().get_local_plugins() 返回的是 Plugin 类实例列表
            # 我们需要的是插件的 ID 和名称
            plugins = PluginManager().get_local_plugins()
            self._all_plugins_cache = []
            for p_instance in plugins:
                # 排除自身，避免循环配置
                if p_instance.id == self.id:
                    continue
                self._all_plugins_cache.append({
                    "id": p_instance.id, # 通常是插件类名或定义的唯一ID
                    "name": p_instance.plugin_name,
                    "version": p_instance.plugin_version,
                    "author": p_instance.plugin_author
                })
        except Exception as e:
            logger.error(f"{self.plugin_name}: 获取插件列表失败: {e}")
            self._all_plugins_cache = []

    def get_state(self) -> bool:
        return self._enabled

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        # 获取系统内置的通知渠道信息 (需要MoviePilot提供相关接口或自行枚举)
        # MessageChannel 是一个 Enum，可以这样获取
        system_channels_options = [{"value": channel.value, "text": channel.name} for channel in MessageChannel]

        # 准备自定义渠道的选项
        custom_channels_options = [
            {"value": ch_conf.get("id"), "text": ch_conf.get("name")}
            for ch_conf in self._custom_channels_config if ch_conf.get("id") and ch_conf.get("name")
        ]

        all_available_channels = system_channels_options + custom_channels_options

        # 构建插件路由配置的UI部分
        plugin_routing_ui_content = []
        if not self._all_plugins_cache: # 如果启动时加载失败，尝试再次加载
             self._load_all_plugins_for_ui()

        for plugin_info in self._all_plugins_cache:
            plugin_id = plugin_info.get("id")
            plugin_display_name = f"{plugin_info.get('name', plugin_id)} (ID: {plugin_id})"
            # model key 应该是 plugin_channel_mapping_str 内JSON的键
            # 例如: plugin_channel_mapping.PluginID
            model_key = f"plugin_channel_mapping.{plugin_id}"

            plugin_routing_ui_content.append({
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 4},
                        'content': [{'component': 'VLabel', 'text': plugin_display_name, 'props': {'class': 'pt-5'}}] # 使用VLabel显示插件名
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 8},
                        'content': [
                            {
                                'component': 'VSelect',
                                'props': {
                                    'model': model_key, # 指向数据结构中对应插件的配置
                                    'label': '选择通知渠道',
                                    'items': all_available_channels, # value 和 text 字段
                                    'item-title': 'text', # Vuetify 3 VSelect 使用 item-title
                                    'item-value': 'value', # Vuetify 3 VSelect 使用 item-value
                                    'clearable': True,
                                    'placeholder': '默认 (不覆盖)'
                                }
                            }
                        ]
                    }
                ]
            })


        return [
            {
                'component': 'VForm',
                'content': [
                    # 启用插件
                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用高级通知路由插件'}},
                    {'component': 'VDivider', 'props': {'class': 'my-4'}},

                    # 自定义通知渠道配置
                    {'component': 'VLabel', 'text': '自定义通知渠道配置', 'props': {'class': 'text-h6 mb-2'}},
                    {
                        'component': 'VTextarea',
                        'props': {
                            'model': 'custom_channels_config_str',
                            'label': '自定义渠道 (JSON格式)',
                            'rows': 8,
                            'placeholder': json.dumps([{"id": "custom_webhook_1", "name": "我的Webhook", "type": "webhook", "config": {"url": "https://example.com/hook", "method": "POST"}}], indent=2, ensure_ascii=False),
                            'hint': '每行一个JSON对象数组，包含id, name, type (如webhook, script), config (对应类型的配置)'
                        }
                    },
                    {'component': 'VDivider', 'props': {'class': 'my-4'}},

                    # 插件通知路由配置
                    {'component': 'VLabel', 'text': '插件通知路由规则', 'props': {'class': 'text-h6 mb-2'}},
                    # 将plugin_routing_ui_content数组的内容解包到这里
                    *plugin_routing_ui_content,

                     # 帮助信息和调试信息
                    {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'class': 'mt-4'},
                     'content': [
                         {'component': 'span', 'text': '说明: \n1. 自定义渠道ID需唯一。\n2. 为插件选择通知渠道后，该插件发出的通知将尝试通过指定渠道发送。\n3. 如果选择“默认”，则本插件不干预该插件的通知。\n4. 要使路由生效，目标插件的通知必须能被本插件正确识别来源。'},
                         {'component': 'span', 'text': f"\n当前已识别的可配置插件数量: {len(self._all_plugins_cache)}"}
                     ]}
                ]
            }
        ], {
            # 初始数据结构
            "enabled": self._enabled,
            "custom_channels_config_str": json.dumps(self._custom_channels_config, indent=2, ensure_ascii=False),
            # 注意：这里的 plugin_channel_mapping 需要转换成适合VForm绑定的扁平结构或者前端做处理
            # 简单起见，我们将其整体作为一个JSON字符串，或者在前端VForm中直接绑定深层对象 (如 props.model = 'plugin_channel_mapping.PluginID')
            # 如果VForm支持深层绑定，这里可以直接传对象：
            # "plugin_channel_mapping": self._plugin_channel_mapping,
            # 为简单起见，也存为字符串，让用户在UI上交互来构建这个对象
            "plugin_channel_mapping_str": json.dumps(self._plugin_channel_mapping, indent=2, ensure_ascii=False),
            # VForm数据结构中，直接将 self._plugin_channel_mapping 放入，让VSelect直接绑定
            "plugin_channel_mapping": self._plugin_channel_mapping,
        }

    def _send_via_custom_channel(self, channel_id: str, title: str, text: str, image: Optional[str] = None, event_data: Dict = None):
        """根据channel_id通过自定义渠道发送通知"""
        target_channel_conf = next((ch for ch in self._custom_channels_config if ch.get("id") == channel_id), None)

        if not target_channel_conf:
            logger.error(f"{self.plugin_name}: 未找到自定义渠道配置: {channel_id}")
            return False

        channel_type = target_channel_conf.get("type")
        channel_config = target_channel_conf.get("config", {})
        channel_name = target_channel_conf.get("name", channel_id)

        logger.info(f"{self.plugin_name}: 尝试通过自定义渠道 '{channel_name}' ({channel_type}) 发送通知: {title}")

        if channel_type == "webhook":
            url = channel_config.get("url")
            method = channel_config.get("method", "POST").upper()
            headers = channel_config.get("headers", {})
            body_template = channel_config.get("body", {"title": "{title}", "text": "{text}", "image": "{image}"})

            if not url:
                logger.error(f"{self.plugin_name}: Webhook渠道 '{channel_name}' 未配置URL。")
                return False

            # 替换模板变量
            def format_template(template_item, title, text, image):
                if isinstance(template_item, str):
                    return template_item.format(title=title, text=text, image=image or "")
                elif isinstance(template_item, dict):
                    return {k: format_template(v, title, text, image) for k, v in template_item.items()}
                elif isinstance(template_item, list):
                    return [format_template(i, title, text, image) for i in template_item]
                return template_item

            payload = format_template(body_template, title, text, image)

            try:
                req = RequestUtils(content_type=headers.get('Content-Type', 'application/json'))
                if method == "POST":
                    response = req.post(url, json=payload if isinstance(payload, dict) else {'data': payload}, headers=headers)
                elif method == "GET":
                    response = req.get(url, params=payload if isinstance(payload, dict) else {'data': payload}, headers=headers)
                else:
                    logger.error(f"{self.plugin_name}: Webhook渠道 '{channel_name}' 不支持的方法: {method}")
                    return False

                if response and 200 <= response.status_code < 300:
                    logger.info(f"{self.plugin_name}: Webhook '{channel_name}' 发送成功: {title}")
                    return True
                else:
                    logger.error(f"{self.plugin_name}: Webhook '{channel_name}' 发送失败: {response.status_code if response else 'No response'} - {response.text if response else 'N/A'}")
                    return False
            except Exception as e:
                logger.error(f"{self.plugin_name}: Webhook '{channel_name}' 发送异常: {e}")
                return False

        # elif channel_type == "script":
        #     script_path = channel_config.get("path")
        #     # 实现执行脚本的逻辑，注意安全性
        #     logger.info(f"执行脚本: {script_path} (未实现)")
        #     return False # 示例，未实现

        else:
            logger.warning(f"{self.plugin_name}: 不支持的自定义渠道类型: {channel_type}")
            return False

    @eventmanager.register(EventType.NoticeMessage)
    def handle_notification(self, event):
        if not self._enabled:
            return

        event_data = event.event_data
        original_title = event_data.get("title")
        original_text = event_data.get("text")
        original_image = event_data.get("image")
        # original_channel = event_data.get("channel") # 原始期望渠道

        # --- 关键：识别通知来源插件 ---
        # MoviePilot 的 EventType.NoticeMessage 的 event_data 中是否包含来源插件信息？
        # 常见的做法是事件源会把自己的一些标识放进去。
        # 假设 event_data 中有一个 'source_plugin_id' 字段。这需要确认！
        source_plugin_id = event_data.get("source_plugin_id") # 理想情况

        # 如果没有直接的 source_plugin_id，我们可能需要一些启发式方法，但这很不稳定。
        # 例如，如果某些插件的通知标题有固定前缀。
        # 或者，如果MoviePilot的事件系统允许，事件对象event本身可能包含来源信息。
        # 在此，我们优先使用假设的 source_plugin_id。

        if not source_plugin_id:
            # 退路：如果无法确定插件ID，我们能否根据原始channel进行更通用的配置？
            # 例如，所有发往 MessageChannel.Wechat 的消息都重定向。
            # 但这不符合“为每个插件指定”的需求。
            # logger.debug(f"{self.plugin_name}: 无法确定通知来源插件ID，跳过路由。标题: {original_title}")
            # return

            # !! 重要变通 !!
            # 如果无法获取来源插件ID，此插件的核心功能“为每个插件指定渠道”将无法完美实现。
            # 一个可能的、但不完美的替代方案是：让用户基于 “原始通知渠道” + “标题/内容关键词” 来配置转发规则。
            # 但这里我们先按最初需求，假设能拿到 source_plugin_id。
            # 为了让示例能跑起来，可以尝试从通知标题中提取信息（非常不推荐，仅为演示）
            # 或者，如果事件是由插件的 pushover 函数触发的，event.event_data['pushover_id'] 可能有用，但这特定于pushover
            # print(f"DEBUG: Event data keys: {event_data.keys()}") # 打印看看有什么
            pass # 暂时放过，等待下面检查 _plugin_channel_mapping

        # 遍历路由规则，寻找匹配的插件ID
        target_channel_id_for_plugin = None
        if source_plugin_id and source_plugin_id in self._plugin_channel_mapping:
            target_channel_id_for_plugin = self._plugin_channel_mapping.get(source_plugin_id)
        # else:
            # 如果没有 source_plugin_id，可以尝试更通用的匹配，但这超出了最初的需求。
            # 比如，用户可以配置 "所有来自'下载器'模块且标题包含'完成'的通知，都发到某某渠道"
            # 为了演示，我们假设 source_plugin_id 就是我们要找的
            # if source_plugin_id: logger.debug(f"{self.plugin_name}: 插件 {source_plugin_id} 未配置特定路由规则。")

        # 全局覆盖逻辑 (如果上面没有根据插件ID匹配到)
        # 也可以设计一个 "default_override_channel" 配置项
        # 此处简化为：如果 plugin_id 没在 mapping 里，则不处理

        if target_channel_id_for_plugin:
            logger.info(f"{self.plugin_name}: 插件 '{source_plugin_id}' 的通知 '{original_title}' 匹配到路由规则，目标渠道: {target_channel_id_for_plugin}")

            # 检查目标渠道是自定义的还是系统的
            is_custom = any(ch.get("id") == target_channel_id_for_plugin for ch in self._custom_channels_config)

            if is_custom:
                if self._send_via_custom_channel(target_channel_id_for_plugin, original_title, original_text, original_image, event_data):
                    logger.info(f"{self.plugin_name}: 通知已通过自定义渠道 '{target_channel_id_for_plugin}' 发送。")
                    # !!! 关键：如何阻止原始通知？!!!
                    # 如果 MoviePilot 的事件系统支持，这里应该阻止事件进一步传播
                    # 例如: event.handled = True 或 event.stop_propagation()
                    # 如果不支持，可能会有重复通知。
                    # 这需要查阅 MoviePilot 事件系统的文档。
                    # 假设可以这样：
                    if hasattr(event, 'prevent_default'): # 这是一个假设的API
                         event.prevent_default()
                         logger.info(f"{self.plugin_name}: 尝试阻止原始通知事件传播。")
                    # 或者，如果事件处理器返回特定值可以阻止传播
                    # return False # 假设返回False可以阻止
                else:
                    logger.error(f"{self.plugin_name}: 通过自定义渠道 '{target_channel_id_for_plugin}' 发送失败。原始通知可能仍会发送。")
            else:
                # 目标是系统渠道
                # 我们需要将通知重新发送到这个系统渠道
                # 这需要 MoviePilot 提供一个发送通知的公共API
                logger.info(f"{self.plugin_name}: 目标为系统渠道 '{target_channel_id_for_plugin}'。尝试重定向...")
                try:
                    # 假设 MoviePilot 有一个全局推送服务
                    # from app.helper.pusher import GlobalPusher # (在顶部导入)
                    # GlobalPusher().push_message(
                    # title=original_title,
                    # text=original_text,
                    # image=original_image,
                    # channel=MessageChannel(target_channel_id_for_plugin), # 将字符串转为Enum成员
                    # userid=event_data.get("userid"), # 保留原始userid
                    # # ... 其他可能的参数，如 pushover_id, event_type 等
                    # )
                    # logger.info(f"{self.plugin_name}: 通知已尝试重定向到系统渠道 '{target_channel_id_for_plugin}'。")
                    # 同样，需要阻止原始事件
                    # if hasattr(event, 'prevent_default'): event.prevent_default()

                    # ---- 更简单的方式：直接修改事件数据中的channel ----
                    # 如果事件处理器在 MoviePilot 的通知分发逻辑 *之前* 执行，
                    # 且后续的分发逻辑会读取 event.event_data.get("channel")，
                    # 那么直接修改它可能是最简单有效的方法。
                    original_event_channel = event_data.get('channel')
                    try:
                        new_channel_enum = MessageChannel(target_channel_id_for_plugin)
                        event_data['channel'] = new_channel_enum
                        logger.info(f"{self.plugin_name}: 已将事件通知渠道从 '{original_event_channel}' 修改为 '{new_channel_enum.name}'. 让MoviePilot核心处理后续发送。")
                        # 这种情况下，不需要阻止事件传播，因为我们只是修改了它的目标。
                    except ValueError:
                        logger.error(f"{self.plugin_name}: 无效的系统渠道名称 '{target_channel_id_for_plugin}'。无法重定向。")

                except Exception as e:
                    logger.error(f"{self.plugin_name}: 重定向到系统渠道 '{target_channel_id_for_plugin}' 失败: {e}")
        else:
            # logger.debug(f"{self.plugin_name}: 通知 '{original_title}' 无匹配路由规则，按原计划发送。")
            pass # 没有匹配规则，不处理，让 MoviePilot 按原计划发送

    def stop_service(self):
        """
        退出插件
        """
        self._enabled = False
        logger.info(f"{self.plugin_name} 服务已停止。")
        # 注销事件监听器 (如果 eventmanager 支持的话)
        # eventmanager.unregister(EventType.NoticeMessage, self.handle_notification) # 假设有此API

# 注意： MoviePilot 的插件系统如何加载和实例化插件很重要。
# _PluginBase 的子类通常会被 PluginManager 发现和管理。