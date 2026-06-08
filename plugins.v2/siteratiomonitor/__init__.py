import threading
from typing import Any, List, Dict, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

lock = threading.Lock()

class SiteRatioMonitor(_PluginBase):
    # 插件名称
    plugin_name = "站点分享率提醒"
    # 插件描述
    plugin_desc = "自定义各站点分享率预警线，无论结果如何都将发送完整的执行报告。"
    # 插件图标
    plugin_icon = "" 
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "yuwancumian"
    # 插件配置项ID前缀
    plugin_config_prefix = "siteratiomonitor_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 2

    _config = {}

    def init_plugin(self, config: dict = None):
        if config:
            self._config = config
        
        if self._config.get("onlyonce"):
            self._config["onlyonce"] = False
            self.update_config(self._config)
            logger.info("立即运行一次站点分享率提醒服务")
            threading.Timer(3, self.check_ratio).start()

    def get_state(self) -> bool:
        return self._config.get("enabled", False)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        # 修复：严格返回空列表，避免前端渲染弹窗时因 NoneType 报错
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        # 修复：严格返回空列表
        return []

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，展示当前监控站点的分享率及状态
        """
        raw_thresholds = self._config.get("site_thresholds", "")
        if not raw_thresholds.strip():
            return [
                {
                    'component': 'div',
                    'text': '尚未配置任何站点阈值，请先点击右下角设置按钮进行配置。',
                    'props': {'class': 'text-center mt-4 text-disabled'}
                }
            ]

        # 1. 解析阈值配置
        threshold_map = {}
        for line in raw_thresholds.splitlines():
            line = line.strip()
            if not line:
                continue
            separator = ":" if ":" in line else ("：" if "：" in line else "=")
            if separator in line:
                parts = line.split(separator, 1)
                site_name = parts[0].strip()
                try:
                    threshold_map[site_name] = float(parts[1].strip())
                except ValueError:
                    pass

        if not threshold_map:
            return [
                {
                    'component': 'div',
                    'text': '站点阈值配置格式不正确，无法解析，请检查配置。',
                    'props': {'class': 'text-center mt-4 text-error'}
                }
            ]

        # 2. 从核心数据库获取最新数据
        try:
            from app.db.site_oper import SiteOper
            latest_data = SiteOper().get_userdata_latest()
        except Exception as e:
            return [
                {'component': 'div', 'text': f'无法获取站点数据: {e}', 'props': {'class': 'text-center mt-4 text-error'}}
            ]

        site_ratios = {data.name: data.ratio for data in (latest_data or []) if data and data.name and data.ratio is not None}

        # 3. 拼装表格行数据
        table_rows = []
        for site_name, threshold in threshold_map.items():
            ratio_val = site_ratios.get(site_name)
            
            if ratio_val is None:
                status_text = '未找到数据'
                status_class = 'text-disabled'
                current_ratio_text = '-'
            else:
                try:
                    current_ratio = float(ratio_val)
                    current_ratio_text = str(current_ratio)
                    if current_ratio < threshold:
                        status_text = '⚠️ 低于阈值'
                        status_class = 'text-error font-weight-bold'
                    else:
                        status_text = '✅ 正常'
                        status_class = 'text-success'
                except (ValueError, TypeError):
                    status_text = '数据异常'
                    status_class = 'text-warning'
                    current_ratio_text = str(ratio_val)
            
            table_rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td', 'text': site_name},
                    {'component': 'td', 'text': str(threshold)},
                    {'component': 'td', 'text': current_ratio_text, 'props': {'class': status_class}},
                    {'component': 'td', 'text': status_text, 'props': {'class': status_class}},
                ]
            })

        # 4. 返回可视化页面 (Vuetify Table)
        return [
            {
                'component': 'VCard',
                'props': {'variant': 'flat', 'class': 'mt-2'},
                'content': [
                    {
                        'component': 'VCardTitle',
                        'text': '监控站点实时状态'
                    },
                    {
                        'component': 'VTable',
                        'props': {'hover': True},
                        'content': [
                            {
                                'component': 'thead',
                                'content': [
                                    {
                                        'component': 'tr',
                                        'content': [
                                            {'component': 'th', 'text': '站点名称', 'props': {'class': 'text-left'}},
                                            {'component': 'th', 'text': '预警阈值', 'props': {'class': 'text-left'}},
                                            {'component': 'th', 'text': '当前分享率', 'props': {'class': 'text-left'}},
                                            {'component': 'th', 'text': '状态', 'props': {'class': 'text-left'}},
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'tbody',
                                'content': table_rows
                            }
                        ]
                    }
                ]
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VTextField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '例如：0 9 * * *'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [{'component': 'VTextarea', 'props': {'model': 'site_thresholds', 'label': '站点提醒阈值配置', 'rows': 5, 'placeholder': 'M-Team: 1.5\nHDCity: 2.0'}}]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "",
            "site_thresholds": ""
        }

    def get_service(self) -> List[Dict[str, Any]]:
        if self.get_state() and self._config.get("cron"):
            return [{
                "id": "SiteRatioMonitor",
                "name": "站点分享率提醒服务",
                "trigger": CronTrigger.from_crontab(self._config.get("cron")),
                "func": self.check_ratio,
                "kwargs": {}
            }]
        return []

    def stop_service(self):
        pass

    @eventmanager.register(EventType.PluginAction)
    def check_ratio(self, event: Event = None):
        if not self.get_state():
            return

        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "sitestatistic_refresh_complete":
                return
            logger.info("检测到站点数据统计刷新完成，开始验证分享率阈值...")

        with lock:
            raw_thresholds = self._config.get("site_thresholds", "")
            if not raw_thresholds.strip():
                logger.info("未配置站点阈值，跳过检查。")
                return

            threshold_map = {}
            for line in raw_thresholds.splitlines():
                line = line.strip()
                if not line:
                    continue
                separator = ":" if ":" in line else ("：" if "：" in line else "=")
                if separator in line:
                    parts = line.split(separator, 1)
                    site_name = parts[0].strip()
                    try:
                        threshold_map[site_name] = float(parts[1].strip())
                    except ValueError:
                        pass

            if not threshold_map:
                logger.warning("站点阈值配置格式不正确，无法提取有效数据。")
                return

            try:
                from app.db.site_oper import SiteOper
                latest_data = SiteOper().get_userdata_latest()
            except Exception as e:
                logger.error(f"无法调用核心数据库 SiteOper: {e}")
                return
                
            if not latest_data:
                logger.warning("核心数据库中没有站点统计数据。")
                return
                
            site_ratios = {data.name: data.ratio for data in latest_data if data and data.name and data.ratio is not None}
            
            report_lines = []
            alert_count = 0
            
            for site_name, threshold in threshold_map.items():
                ratio_val = site_ratios.get(site_name)
                
                if ratio_val is None:
                    report_lines.append(f"⚪ **{site_name}**：数据库未找到该站点")
                    continue

                try:
                    current_ratio = float(ratio_val)
                    if current_ratio < threshold:
                        report_lines.append(f"⚠️ **{site_name}**：`{current_ratio}` (预警线: `{threshold}`)")
                        alert_count += 1
                    else:
                        report_lines.append(f"✅ **{site_name}**：`{current_ratio}` (正常)")
                except (ValueError, TypeError):
                    report_lines.append(f"⚪ **{site_name}**：分享率数据格式异常")

            msg_title = f"{'🚨' if alert_count > 0 else '📊'} 站点分享率执行报告"
            
            header = f"本次共检查了 {len(threshold_map)} 个站点，其中 {alert_count} 个低于预警线。\n"
            if alert_count > 0:
                header += "💡 *建议关注标记 ⚠️ 的站点并购买上传量。*\n"
            header += "————————————————————\n"
            
            msg_body = header + "\n".join(report_lines)
            
            logger.info(f"发送分享率报告，包含 {len(report_lines)} 个站点信息。")
            self.post_message(mtype=NotificationType.Plugin, title=msg_title, text=msg_body)