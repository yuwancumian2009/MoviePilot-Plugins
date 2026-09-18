"""
幽灵整理记录（GhostTransferCleaner）

场景说明
--------
MoviePilot 在整理入库成功后会写入一条「整理记录」（transferhistory）。
自动整理的判定逻辑是：**只要存在一条整理成功的历史记录，就跳过整理**
（v2 源码 app/chain/transfer.py: "已成功转移过，如需重新处理，请删除历史记录"）。

于是会出现这种情况：媒体文件被手工误删后，整理记录仍然留在库里。
此后再次下载同一资源，MP 会因为「已有整理记录」而直接跳过整理，无法自动入库。

本插件用于体检这类「幽灵整理记录」：整理记录还在，但它对应的媒体文件
实际上已经不在媒体库里了。

判定依据：只比对 NAS 本地 strm 文件
-----------------------------------
媒体库为 strm 形态时，**一个 strm 文件对应一个云端（115）媒体文件**，
两者默认一一对应。因此判断一条整理记录是否还有效，只需要看它记录的目标
路径在本地 strm 目录里还能不能找到对应文件，**完全不需要（也不应该）去
访问云端**——这既避免了 115 风控，也更快、更稳。

匹配方式为「后缀匹配」：把整理记录的目标路径逐级剥掉前导目录，拼到每个
已配置的 strm 根目录下查找同名 .strm，命中即认为媒体还在。
这样可以兼容「记录里是 115 路径、本地是 strm 目录」这类路径前缀不一致的情况。

判定分级
--------
- 整部缺失：strm 目录树里连该节目目录都找不到 —— 整部媒体都没了，
  属于高可信幽灵记录，可安全清理。
- 局部缺失：节目目录还在，但该文件对应的 strm 不存在 —— 可能只是删了
  其中几集，也可能是记录已过时，需要人工确认后再清理。

安全设计
--------
1. **绝不访问云端 / 远端存储**：不做任何 115、WebDAV、存储链（StorageChain）
   查询，只读取本地文件系统与整理记录本身，从根上规避 115 风控。
2. 只读取整理记录，不触碰任何媒体文件；清理仅删除数据库里的整理记录。
3. 一键清理需要先在插件配置中显式打开「允许一键清理」开关。
4. 若超过八成的记录都被判定为丢失，视为「strm 目录配置有误或媒体库结构
   发生变化」的异常信号，自动禁止清理并给出提示。
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
from app.db import SessionFactory
from app.db.models.transferhistory import TransferHistory
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

# 缺失分级
LEVEL_WHOLE = "whole"
LEVEL_PART = "part"
LEVEL_TEXT = {
    LEVEL_WHOLE: "整部缺失",
    LEVEL_PART: "局部缺失",
}

# 单条路径的探测结果
STATE_OK = "ok"
STATE_UNKNOWN = "unknown"

# strm 扩展名
STRM_SUFFIX = ".strm"
# 判定引擎版本：本地 strm 比对（1=旧的存储层查询）
ENGINE_VERSION = 2
# 单次扫描的最长耗时（秒）
SCAN_DEADLINE = 60
# 页面最多展示条数
PAGE_LIMIT = 200
# 最多缓存的幽灵记录条数
CACHE_LIMIT = 1000
# 判定为「异常占比过高」的最低样本量
SUSPICIOUS_MIN_SAMPLE = 20
# 判定为「异常占比过高」的比例
SUSPICIOUS_RATIO = 0.8
# 后缀匹配时最多回溯的目录层数（防止异常记录产生大量探测）
MAX_TAIL_DEPTH = 8
# 目录内容缓存的最大条目数
DIR_CACHE_LIMIT = 20000


class GhostTransferCleaner(_PluginBase):
    """幽灵整理记录：体检并清理「整理记录还在、媒体文件已丢失」的记录。"""

    # 插件名称
    plugin_name = "幽灵整理记录"
    # 插件描述
    plugin_desc = "体检「整理记录还在、媒体文件已丢失」的幽灵记录，支持一键清理，恢复正常自动整理入库。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/clean.png"
    # 插件版本
    plugin_version = "1.1.0"
    # 插件作者
    plugin_author = "呵呵"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "ghosttransfercleaner_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # ---- 配置项 ----
    _enabled = False
    _notify = True
    _strm_paths: List[str] = []
    _path_map: List[Tuple[str, str]] = []
    _check_files = False
    _only_whole = True
    _allow_clean = False
    _auto_clean = False
    _min_age_days = 0
    _max_records = 5000
    _cron = ""

    # ---- 运行状态 ----
    _ghosts: List[Dict[str, Any]] = []
    _stats: Dict[str, Any] = {}
    _scanning = False
    _lock = threading.Lock()
    # 目录内容缓存：{目录绝对路径: {小写名称: 是否目录}}，None 表示该目录不可访问
    _dir_cache: Dict[str, Optional[Dict[str, bool]]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None):
        """根据插件配置初始化运行状态。"""
        # 重置配置
        self._enabled = False
        self._notify = True
        self._strm_paths = []
        self._path_map = []
        self._check_files = False
        self._only_whole = True
        self._allow_clean = False
        self._auto_clean = False
        self._min_age_days = 0
        self._max_records = 5000
        self._cron = ""
        self._dir_cache = {}

        scan_now = False
        clean_now = False
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._strm_paths = self.__parse_lines(config.get("strm_paths"))
            self._path_map = self.__parse_path_map(config.get("path_map"))
            self._check_files = bool(config.get("check_files", False))
            self._only_whole = bool(config.get("only_whole", True))
            self._allow_clean = bool(config.get("allow_clean", False))
            self._auto_clean = bool(config.get("auto_clean", False))
            self._min_age_days = self.__to_int(config.get("min_age_days"), 0)
            self._max_records = max(1, self.__to_int(config.get("max_records"), 5000))
            self._cron = str(config.get("cron") or "").strip()
            scan_now = bool(config.get("scan_now"))
            clean_now = bool(config.get("clean_now"))
            if scan_now or clean_now:
                # 复位一次性开关，避免插件重载后重复执行
                reset = dict(config)
                reset["scan_now"] = False
                reset["clean_now"] = False
                self.update_config(reset)

        # 读取最近一次扫描结果
        self.__load()

        if self._enabled and (scan_now or clean_now):
            # 放到后台线程执行，避免阻塞插件配置保存的请求
            threading.Thread(
                target=self.__scan_task,
                kwargs={"clean": clean_now, "notify": self._notify},
                daemon=True,
            ).start()

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enabled

    def stop_service(self):
        """停止插件服务。"""
        self._enabled = False

    # ------------------------------------------------------------------
    # 远程命令
    # ------------------------------------------------------------------
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。"""
        return [
            {
                "cmd": "/ghost",
                "event": EventType.PluginAction,
                "desc": "体检幽灵整理记录",
                "category": "插件",
                "data": {"action": "ghosttransfercleaner_scan"},
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        """响应 /ghost 远程命令，执行一次体检。"""
        try:
            data = (event.event_data if event else None) or {}
            if data.get("action") != "ghosttransfercleaner_scan":
                return
            self.__scan(clean=False, notify=True)
        except Exception as err:
            logger.error(f"幽灵整理记录：处理远程命令失败：{err}")

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 路由列表。"""
        pid = self.__class__.__name__
        return [
            {
                "path": f"/{pid}/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "summary": "查询扫描状态与幽灵记录",
                "description": "返回最近一次扫描的统计信息与幽灵整理记录清单",
            },
            {
                "path": f"/{pid}/scan",
                "endpoint": self.api_scan,
                "methods": ["GET", "POST"],
                "summary": "立即扫描幽灵整理记录",
                "description": "扫描整理记录，找出目标媒体文件已不存在的幽灵记录",
            },
            {
                "path": f"/{pid}/clean",
                "endpoint": self.api_clean,
                "methods": ["GET", "POST"],
                "summary": "清理幽灵整理记录",
                "description": "删除扫描到的幽灵整理记录（需先在插件配置中开启「允许一键清理」）",
            },
        ]

    def api_status(self) -> Dict[str, Any]:
        """查询最近一次扫描状态与幽灵记录清单。"""
        return {
            "success": True,
            "message": "ok",
            "data": {
                "scanning": self._scanning,
                "stats": self._stats or {},
                "ghosts": self._ghosts or [],
            },
        }

    def api_scan(self) -> Dict[str, Any]:
        """立即扫描幽灵整理记录。"""
        try:
            stats = self.__scan(clean=False, notify=False)
        except Exception as err:
            logger.error(f"幽灵整理记录：扫描失败：{err}")
            return {"success": False, "message": f"扫描失败：{err}", "data": {}}
        return {
            "success": True,
            "message": f"扫描完成，发现 {stats.get('ghost', 0)} 条幽灵整理记录",
            "data": stats,
        }

    def api_clean(self) -> Dict[str, Any]:
        """清理扫描到的幽灵整理记录。"""
        if not self._allow_clean:
            return {
                "success": False,
                "message": "为避免误删，请先在插件配置中开启「允许一键清理」开关后再操作",
                "data": {},
            }
        try:
            stats = self.__scan(clean=True, notify=self._notify)
        except Exception as err:
            logger.error(f"幽灵整理记录：清理失败：{err}")
            return {"success": False, "message": f"清理失败：{err}", "data": {}}
        return {
            "success": True,
            "message": stats.get("last_action") or "清理完成",
            "data": stats,
        }

    # ------------------------------------------------------------------
    # 定时服务
    # ------------------------------------------------------------------
    def get_service(self) -> List[Dict[str, Any]]:
        """返回定时扫描服务列表。"""
        if self._enabled and self._cron:
            return [
                {
                    "id": "GhostTransferCleanerScan",
                    "name": "幽灵整理记录体检",
                    "trigger": "cron",
                    "func": self.scheduled_scan,
                    "kwargs": {"cron": self._cron},
                }
            ]
        return []

    def scheduled_scan(self):
        """定时任务入口：执行一次扫描，并按配置决定是否自动清理。"""
        try:
            self.__scan(clean=self._auto_clean, notify=self._notify)
        except Exception as err:
            logger.error(f"幽灵整理记录：定时扫描失败：{err}")

    # ------------------------------------------------------------------
    # 配置页面
    # ------------------------------------------------------------------
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
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
                                            "model": "strm_paths",
                                            "label": "strm 媒体库根目录（每行一个，容器内路径）",
                                            "placeholder": "/media/strm\n/mnt/media/strm",
                                            "rows": 3,
                                            "hint": "填存放 strm 文件的本地目录，例如 /media/strm。"
                                                    "本插件只比对这些目录下的 .strm 文件，不会访问 115 或任何云端接口；"
                                                    "请勿填 115/WebDAV 等网络挂载路径。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
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
                                            "model": "path_map",
                                            "label": "路径前缀映射（可选，每行一条：记录中的前缀=本地实际前缀）",
                                            "placeholder": "/115/电影=/media/strm/电影",
                                            "rows": 2,
                                            "hint": "仅在整理记录里的路径与本地 strm 目录前缀对不上时才需要填。"
                                                    "不填时插件会自动做「后缀匹配」，多数情况无需配置。",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "扫描后发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "check_files",
                                            "label": "深度检查（按记录内的文件清单逐个核对 strm）",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
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
                                            "model": "only_whole",
                                            "label": "只清理「整部缺失」的记录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "allow_clean",
                                            "label": "允许一键清理（数据页面按钮生效）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "auto_clean",
                                            "label": "定时扫描时自动清理",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_age_days",
                                            "label": "只检查 N 天前的整理记录，0 表示全部",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_records",
                                            "label": "单次最多检查记录数",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "定时扫描 cron 表达式，留空则不定时",
                                            "placeholder": "0 */6 * * *",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "scan_now",
                                            "label": "保存后立刻扫描一次（自动复位）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clean_now",
                                            "label": "保存后立刻清理一次（自动复位，需先开允许清理）",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "本插件只读取整理记录、并比对 NAS 本地的 strm 文件，"
                                                    "不访问 115 或任何云端接口（避免风控），也不会删除任何媒体文件。"
                                                    "「清理」仅删除数据库中的整理记录，清理后重新下载相同资源即可正常自动整理入库。"
                                                    "建议先看一遍数据页面列出的清单，确认无误再清理。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "strm_paths": "",
            "path_map": "",
            "check_files": False,
            "only_whole": True,
            "allow_clean": False,
            "auto_clean": False,
            "min_age_days": 0,
            "max_records": 5000,
            "cron": "",
            "scan_now": False,
            "clean_now": False,
        }

    # ------------------------------------------------------------------
    # 数据页面
    # ------------------------------------------------------------------
    def get_page(self) -> List[dict]:
        """返回插件数据页面。"""
        pid = self.__class__.__name__
        if not self._enabled:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "text": "插件未启用，请先在插件配置中启用并保存。",
                    },
                }
            ]

        stats = self._stats if isinstance(self._stats, dict) else {}
        ghosts = self._ghosts if isinstance(self._ghosts, list) else []

        if self._scanning:
            head = "正在扫描整理记录，请稍后刷新本页查看结果…"
            head_type = "info"
        elif not stats:
            head = "尚未扫描，点击下方「立即扫描」开始体检。"
            head_type = "info"
        else:
            head = (
                f"最近扫描 {stats.get('scan_time', '-')}｜"
                f"已检查成功整理记录 {stats.get('scanned', 0)} 条"
                f"（库中共 {stats.get('total', 0)} 条）｜"
                f"发现幽灵记录 {stats.get('ghost', 0)} 条"
                f"（整部缺失 {stats.get('whole', 0)}、局部缺失 {stats.get('part', 0)}）｜"
                f"耗时 {stats.get('duration', 0)} 秒"
            )
            if self._strm_paths:
                head += f"｜比对 strm 目录 {len(self._strm_paths)} 个"
            if stats.get("truncated"):
                head += "｜⚠️ 本次扫描因超时提前结束，结果可能不完整"
            if stats.get("unknown"):
                head += f"｜{stats.get('unknown')} 条记录无法判断（已跳过）"
            head_type = "warning" if stats.get("ghost") else "success"

        content: List[dict] = [
            {
                "component": "VAlert",
                "props": {"type": head_type, "variant": "tonal", "text": head},
            }
        ]

        if not self._strm_paths:
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "error",
                        "variant": "tonal",
                        "text": "尚未配置「strm 媒体库根目录」，插件无法判断整理记录是否还有效。"
                                "请先到插件配置中填写存放 strm 文件的本地目录（例如 /media/strm）再扫描；"
                                "在配置完成前，扫描不会得出任何结论。",
                    },
                }
            )

        if stats.get("last_action"):
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": f"上次操作：{stats['last_action']}",
                    },
                }
            )

        if stats.get("suspicious"):
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "error",
                        "variant": "tonal",
                        "text": "⚠️ 超过八成整理记录都找不到对应的 strm 文件，这更像 strm 目录配置有误"
                                "（例如根目录填错、媒体库目录结构变过），而不是文件真的被删。"
                                "已自动禁止清理，请先核对上面配置的 strm 根目录再操作。",
                    },
                }
            )

        content.append(
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "class": "d-flex ga-2 mb-2"},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "tonal",
                                    "size": "small",
                                    "prepend-icon": "mdi-magnify",
                                },
                                "text": "立即扫描",
                                "events": {
                                    "click": {"api": f"/plugin/{pid}/scan", "method": "POST"}
                                },
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "error",
                                    "variant": "tonal",
                                    "size": "small",
                                    "prepend-icon": "mdi-delete-sweep",
                                },
                                "text": "清理幽灵记录",
                                "events": {
                                    "click": {"api": f"/plugin/{pid}/clean", "method": "POST"}
                                },
                            },
                        ],
                    }
                ],
            }
        )

        items = []
        for ghost in ghosts[:PAGE_LIMIT]:
            title = f"{ghost.get('title', '')} {ghost.get('year', '')}".strip()
            season_episode = f"{ghost.get('seasons', '') or ''}{ghost.get('episodes', '') or ''}".strip()
            items.append(
                {
                    "title": title or "-",
                    "type": ghost.get("type", "-"),
                    "season_episode": season_episode or "-",
                    "level_text": ghost.get("level_text", "-"),
                    "date": ghost.get("date", "-"),
                    "dest": ghost.get("dest", "-"),
                }
            )

        content.append(
            {
                "component": "VDataTable",
                "props": {
                    "headers": [
                        {"title": "标题", "key": "title"},
                        {"title": "类型", "key": "type"},
                        {"title": "季集", "key": "season_episode"},
                        {"title": "缺失程度", "key": "level_text"},
                        {"title": "整理时间", "key": "date"},
                        {"title": "原目标路径", "key": "dest"},
                    ],
                    "items": items,
                    "items-per-page": -1,
                    "hide-default-footer": True,
                    "density": "compact",
                },
            }
        )

        foot = (
            f"共 {len(ghosts)} 条幽灵记录，页面最多展示 {PAGE_LIMIT} 条。"
            "判定依据是本地 strm 文件：「整部缺失」表示 strm 目录树里连该节目目录都找不到，可信度最高；"
            "「局部缺失」表示节目目录还在、只是该文件对应的 strm 没了，可能只删了其中几集，建议先人工确认。"
        )
        if not self._allow_clean:
            foot += " 当前未开启「允许一键清理」，页面上的清理按钮不会真正删除记录。"
        content.append(
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": foot},
            }
        )

        return [
            {
                "component": "VRow",
                "content": [
                    {"component": "VCol", "props": {"cols": 12}, "content": content}
                ],
            }
        ]

    # ------------------------------------------------------------------
    # 扫描核心
    # ------------------------------------------------------------------
    def __scan_task(self, clean: bool = False, notify: bool = True):
        try:
            self.__scan(clean=clean, notify=notify)
        except Exception as err:
            logger.error(f"幽灵整理记录：后台扫描失败：{err}")

    def __scan(self, clean: bool = False, notify: bool = False) -> Dict[str, Any]:
        """扫描整理记录，返回统计信息。"""
        if not self._lock.acquire(blocking=False):
            logger.info("幽灵整理记录：已有体检任务在执行，本次跳过")
            return self._stats or {}

        started = time.time()
        try:
            self._scanning = True
            # 每次扫描都重置目录缓存，确保读到最新的 strm 目录结构
            self._dir_cache = {}

            # 没有配置 strm 根目录时不给出任何结论，避免把整库误判为幽灵
            if not self._strm_paths:
                stats = {
                    "engine": ENGINE_VERSION,
                    "total": 0,
                    "scanned": 0,
                    "ghost": 0,
                    "whole": 0,
                    "part": 0,
                    "unknown": 0,
                    "truncated": False,
                    "suspicious": False,
                    "duration": 0,
                    "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "cleaned": 0,
                    "last_action": "尚未配置 strm 媒体库根目录，无法判断整理记录是否有效",
                    "strm_paths": [],
                }
                self._ghosts = []
                self._stats = stats
                self.__save()
                logger.warning("幽灵整理记录：未配置 strm 媒体库根目录，本次扫描未执行")
                return stats

            rows, total = self.__load_records()

            ghosts: List[Dict[str, Any]] = []
            unknown = 0
            scanned = 0
            truncated = False

            for row in rows:
                if time.time() - started > SCAN_DEADLINE:
                    truncated = True
                    logger.warning("幽灵整理记录：体检超时，已提前结束，结果可能不完整")
                    break
                scanned += 1
                try:
                    state, item = self.__check(row)
                except Exception as err:
                    unknown += 1
                    logger.debug(f"幽灵整理记录：检查整理记录 {row.get('id')} 出错：{err}")
                    continue
                if state == "ghost" and item:
                    ghosts.append(item)
                elif state == "unknown":
                    unknown += 1

            whole = len([g for g in ghosts if g["level"] == LEVEL_WHOLE])
            part = len([g for g in ghosts if g["level"] == LEVEL_PART])
            suspicious = scanned >= SUSPICIOUS_MIN_SAMPLE and len(ghosts) >= scanned * SUSPICIOUS_RATIO

            stats: Dict[str, Any] = {
                "engine": ENGINE_VERSION,
                "total": total,
                "scanned": scanned,
                "ghost": len(ghosts),
                "whole": whole,
                "part": part,
                "unknown": unknown,
                "truncated": truncated,
                "suspicious": suspicious,
                "duration": round(time.time() - started, 1),
                "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "cleaned": 0,
                "last_action": "",
                "strm_paths": list(self._strm_paths),
            }

            if clean and ghosts:
                if suspicious:
                    stats["last_action"] = "异常记录占比过高，疑似媒体库挂载路径发生变化，已中止清理"
                    logger.warning("幽灵整理记录：异常记录占比过高，已中止清理，请先核对目录映射")
                else:
                    cleaned_ids, failed = self.__clean(ghosts)
                    ghosts = [g for g in ghosts if g["id"] not in cleaned_ids]
                    stats["cleaned"] = len(cleaned_ids)
                    stats["ghost"] = len(ghosts)
                    stats["whole"] = len([g for g in ghosts if g["level"] == LEVEL_WHOLE])
                    stats["part"] = len([g for g in ghosts if g["level"] == LEVEL_PART])
                    stats["last_action"] = f"已清理 {len(cleaned_ids)} 条幽灵整理记录"
                    if failed:
                        stats["last_action"] += f"，{failed} 条清理失败（详见日志）"

            self._ghosts = ghosts[:CACHE_LIMIT]
            self._stats = stats
            self.__save()

            if notify:
                self.__notify_result(stats, self._ghosts)

            logger.info(
                f"幽灵整理记录：体检完成（依据本地 strm），检查 {scanned}/{total} 条，"
                f"发现幽灵记录 {stats['ghost']} 条（整部缺失 {stats['whole']}、局部缺失 {stats['part']}）"
            )
        finally:
            self._scanning = False
            self._lock.release()

        return self._stats or {}

    def __load_records(self) -> Tuple[List[Dict[str, Any]], int]:
        """从数据库读取整理记录，返回（记录字典列表, 符合条件的总数）。"""
        db = SessionFactory()
        try:
            query = db.query(TransferHistory).filter(TransferHistory.status == True)  # noqa: E712
            if self._min_age_days > 0:
                cutoff = (datetime.now() - timedelta(days=self._min_age_days)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                query = query.filter(TransferHistory.date < cutoff)
            total = query.count()
            records = query.limit(self._max_records).all()
            rows = [self.__to_dict(record) for record in records]
        finally:
            db.close()
        return rows, total

    def __check(self, row: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
        """检查单条整理记录，返回（ok/ghost/unknown, 幽灵记录详情）。"""
        raw_dest = row.get("dest") or ""
        # 路径原样保留（Linux 下反斜杠是合法文件名字符），仅展示时统一分隔符
        dests = [
            item.strip()
            for item in str(raw_dest).replace("\r", "\n").split("\n")
            if item.strip()
        ]
        if not dests:
            return "unknown", None

        # 主判定：记录的目标路径在本地 strm 目录里还能不能找到对应文件
        states = [self.__probe_strm(path) for path in dests]
        if all(state == STATE_UNKNOWN for state in states):
            return STATE_UNKNOWN, None
        dest_exists = STATE_OK in states

        # 深度检查：按记录内的文件清单逐个核对 strm（可选）
        file_paths: List[str] = []
        files_left = 0
        files_missing = 0
        if self._check_files:
            file_paths = self.__extract_paths(row.get("files"))
            for path in file_paths:
                state = self.__probe_strm(path)
                if state == STATE_OK:
                    files_left += 1
                elif state != STATE_UNKNOWN:
                    files_missing += 1

        if dest_exists:
            # 目标路径的 strm 还在：正常记录
            if not self._check_files or not file_paths or files_left > 0:
                return STATE_OK, None
            level = LEVEL_PART
            reason = f"目标路径的 strm 仍在，但记录中的 {files_missing} 个文件已全部没有对应 strm"
        else:
            if self._check_files and file_paths and files_left > 0:
                return STATE_OK, None
            # 所有目标路径都指向「整部缺失」时才升级为整部缺失
            if states and all(state == LEVEL_WHOLE for state in states):
                level = LEVEL_WHOLE
                reason = "strm 媒体库中找不到该节目目录（整部媒体已缺失）"
            else:
                level = LEVEL_PART
                reason = "节目目录仍在 strm 媒体库中，但该文件对应的 strm 已不存在"

        return "ghost", {
            "id": row.get("id"),
            "title": row.get("title") or "",
            "year": row.get("year") or "",
            "type": row.get("type") or "",
            "category": row.get("category") or "",
            "seasons": row.get("seasons") or "",
            "episodes": row.get("episodes") or "",
            "date": row.get("date") or "",
            "mode": row.get("mode") or "",
            "src": row.get("src") or "",
            "dest": self.__display_path(dests[0]),
            "match": "strm",
            "level": level,
            "level_text": LEVEL_TEXT[level],
            "reason": reason,
            "files_total": len(file_paths),
            "files_missing": files_missing,
            "download_hash": row.get("download_hash") or "",
        }

    def __clean(self, ghosts: List[Dict[str, Any]]) -> Tuple[List[Any], int]:
        """删除幽灵整理记录，返回（已删除的 id 列表, 失败条数）。"""
        targets = [g for g in ghosts if g.get("id") is not None]
        if self._only_whole:
            targets = [g for g in targets if g.get("level") == LEVEL_WHOLE]
        if not targets:
            return [], 0

        ids = [g["id"] for g in targets]
        deleted: List[Any] = []
        failed = 0

        db = SessionFactory()
        try:
            for record_id in ids:
                try:
                    count = (
                        db.query(TransferHistory)
                        .filter(TransferHistory.id == record_id)
                        .delete(synchronize_session=False)
                    )
                    db.commit()
                    if count:
                        deleted.append(record_id)
                    else:
                        failed += 1
                except Exception as err:
                    failed += 1
                    db.rollback()
                    logger.error(f"幽灵整理记录：删除整理记录 {record_id} 失败：{err}")
        finally:
            db.close()

        if deleted:
            logger.info(f"幽灵整理记录：已清理 {len(deleted)} 条整理记录")
        return deleted, failed

    # ------------------------------------------------------------------
    # strm 比对（只读本地文件系统，绝不访问云端）
    # ------------------------------------------------------------------
    def __probe_strm(self, path: str) -> str:
        """
        判断一条记录路径在本地 strm 库中是否还有对应文件。

        采用「后缀匹配」：把记录路径逐级剥掉前导目录，拼到每个 strm 根目录下查找
        同名 .strm，命中即认为媒体还在。这样可兼容「记录里是 115 路径、本地是 strm
        目录」这类前缀不一致的情况，无需用户精确配置路径映射。

        :return ok（找到对应 strm）/ whole（连节目目录都找不到，整部缺失）/
                part（节目目录还在但该文件的 strm 没了）/ unknown（无法判断）
        """
        if not path:
            return STATE_UNKNOWN
        return self.__probe_normalized(self.__normalize(path))

    def __probe_normalized(self, normalized: str) -> str:
        segments = [seg for seg in str(normalized).split("/") if seg]
        if not segments:
            return STATE_UNKNOWN

        filename = segments[-1]
        dirs = segments[:-1]
        names = self.__strm_names(filename)
        if not names:
            return STATE_UNKNOWN

        dir_found = False
        for root in self._strm_paths:
            # 先试最完整的相对路径，再逐级剥离前导目录
            for drop in range(0, min(len(dirs), MAX_TAIL_DEPTH) + 1):
                tail = dirs[drop:]
                for name in names:
                    if self.__lookup(root, tail, name) is not None:
                        return STATE_OK
                # 该层级的父目录下是否存在同名节目目录
                if tail and self.__lookup(root, tail[:-1], tail[-1]) is True:
                    dir_found = True

        return LEVEL_PART if dir_found else LEVEL_WHOLE

    def __lookup(self, root: str, tail: List[str], name: str) -> Optional[bool]:
        """
        在 strm 根目录下按相对目录段查找某个条目（大小写不敏感）。
        :return True 目录 / False 文件 / None 不存在
        """
        entries = self.__list_dir(self.__join(root, tail))
        if entries is None:
            return None
        return entries.get(str(name).lower())

    def __list_dir(self, directory: str) -> Optional[Dict[str, bool]]:
        """
        列出目录内容（小写名称 -> 是否子目录），并缓存结果。
        目录不存在、不可读或不是目录时返回 None 并缓存，避免重复扫描。
        """
        if directory in self._dir_cache:
            return self._dir_cache[directory]

        entries: Optional[Dict[str, bool]] = None
        try:
            if os.path.isdir(directory):
                result: Dict[str, bool] = {}
                with os.scandir(directory) as iterator:
                    for item in iterator:
                        try:
                            result[item.name.lower()] = item.is_dir()
                        except OSError:
                            continue
                entries = result
        except OSError as err:
            logger.debug(f"幽灵整理记录：读取目录 {directory} 失败：{err}")
            entries = None

        if len(self._dir_cache) < DIR_CACHE_LIMIT:
            self._dir_cache[directory] = entries
        return entries

    def __normalize(self, path: str) -> str:
        """统一分隔符，并应用用户配置的路径前缀映射。"""
        value = str(path).strip().replace("\\", "/")
        for old, new in self._path_map:
            if value.startswith(old):
                value = f"{new}{value[len(old):]}"
                break
        return value

    @staticmethod
    def __join(root: str, tail: List[str]) -> str:
        """把 strm 根目录与相对目录段拼成绝对路径。"""
        parts = [seg for seg in (tail or []) if seg]
        if not parts:
            return root
        return "/".join([str(root).rstrip("/")] + parts)

    @staticmethod
    def __strm_names(filename: str) -> List[str]:
        """给出文件在 strm 库中的可能名称（原名、换成 .strm、补 .strm）。"""
        name = str(filename or "").strip()
        if not name:
            return []

        candidates = [name]
        stem = name[: -len(STRM_SUFFIX)] if name.lower().endswith(STRM_SUFFIX) else name
        # 去掉原扩展名后补上 .strm（.mkv/.mp4 → .strm）
        if "." in stem:
            stem = stem.rsplit(".", 1)[0]
        if stem:
            candidates.append(f"{stem}{STRM_SUFFIX}")
        candidates.append(f"{name}{STRM_SUFFIX}")

        result: List[str] = []
        for candidate in candidates:
            if candidate and candidate not in result:
                result.append(candidate)
        return result

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def __to_dict(record: Any) -> Dict[str, Any]:
        """把整理记录转成普通字典，避免会话关闭后访问属性出错。"""
        try:
            return {
                column.name: getattr(record, column.name, None)
                for column in record.__table__.columns
            }
        except Exception:
            names = (
                "id", "src", "dest", "mode", "type", "category", "title", "year",
                "tmdbid", "seasons", "episodes", "download_hash", "status", "date",
                "files", "dest_storage", "src_storage",
            )
            return {name: getattr(record, name, None) for name in names}

    @staticmethod
    def __extract_paths(raw: Any) -> List[str]:
        """从整理记录的 files 字段里提取文件路径，兼容不同版本的存储结构。"""
        if not raw:
            return []
        data = raw
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return []
            try:
                data = json.loads(text)
            except Exception:
                data = [line.strip() for line in text.splitlines() if line.strip()]
        if isinstance(data, dict):
            data = list(data.values())
        if not isinstance(data, (list, tuple)):
            return []

        result: List[str] = []
        for item in data:
            if isinstance(item, str):
                value = item
            elif isinstance(item, dict):
                value = ""
                for key in ("new_path", "target", "dest", "path", "file"):
                    candidate = item.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        value = candidate
                        break
            else:
                value = str(getattr(item, "path", "") or "")
            value = str(value).replace("\\", "/").strip()
            # 只接受看起来像路径的值，避免把标题之类的内容当成路径
            if value and ("/" in value or ":" in value) and value not in result:
                result.append(value)
        return result

    @staticmethod
    def __parse_lines(raw: Any) -> List[str]:
        """解析多行/逗号/分号分隔的路径列表，去重并保持顺序。"""
        if not raw:
            return []
        text = (
            str(raw)
            .replace("\r", "\n")
            .replace("，", ",")
            .replace("；", "\n")
            .replace(";", "\n")
        )
        values: List[str] = []
        for chunk in text.split("\n"):
            for item in chunk.split(","):
                value = item.strip().strip('"').strip("'")
                if value and value not in values:
                    values.append(value)
        return values

    @staticmethod
    def __parse_path_map(raw: Any) -> List[Tuple[str, str]]:
        """解析「记录中的前缀=本地实际前缀」形式的路径映射规则，长前缀优先。"""
        rules: List[Tuple[str, str]] = []
        for line in GhostTransferCleaner.__parse_lines(raw):
            if "=" not in line:
                continue
            old, _, new = line.partition("=")
            old = old.strip().rstrip("/")
            new = new.strip().rstrip("/")
            if old and new:
                rules.append((old, new))
        rules.sort(key=lambda item: len(item[0]), reverse=True)
        return rules

    @staticmethod
    def __display_path(path: Any) -> str:
        """统一展示用的路径分隔符。"""
        return str(path or "").replace("\\", "/")

    @staticmethod
    def __to_int(value: Any, default: int) -> int:
        try:
            if value is None or value == "":
                return default
            return int(float(value))
        except (TypeError, ValueError):
            return default

    def __notify_result(self, stats: Dict[str, Any], ghosts: List[Dict[str, Any]]):
        try:
            if stats.get("last_action"):
                head = stats["last_action"]
            else:
                head = (
                    f"已比对 {stats.get('scanned', 0)} 条整理记录（依据本地 strm 文件），"
                    f"发现幽灵记录 {stats.get('ghost', 0)} 条"
                    f"（整部缺失 {stats.get('whole', 0)}、局部缺失 {stats.get('part', 0)}）"
                )
            lines = [head]
            if stats.get("ghost"):
                for ghost in ghosts[:5]:
                    season_episode = (
                        f"{ghost.get('seasons', '') or ''}{ghost.get('episodes', '') or ''}"
                    ).strip()
                    label = " ".join(
                        part for part in [ghost.get("title", ""), ghost.get("year", ""), season_episode] if part
                    )
                    lines.append(f"· {label or '未知'} [{ghost.get('level_text', '')}]")
                if stats["ghost"] > 5:
                    lines.append(f"…等共 {stats['ghost']} 条，详见插件数据页面")
            else:
                lines.append("未发现幽灵整理记录，自动整理逻辑正常。")
            if stats.get("suspicious"):
                lines.append("⚠️ 超八成记录都找不到对应 strm，疑似 strm 根目录配置有误，已中止清理，请先核对配置。")
            if stats.get("truncated"):
                lines.append("⚠️ 本次扫描超时提前结束，结果可能不完整。")
            self.systemmessage.put("\n".join(lines), title="幽灵整理记录")
        except Exception as err:
            logger.error(f"幽灵整理记录：发送通知失败：{err}")

    def __load(self):
        try:
            stored = self.get_data("status") or {}
        except Exception as err:
            logger.error(f"幽灵整理记录：读取历史扫描结果失败：{err}")
            stored = {}
        if isinstance(stored, dict):
            ghosts = stored.get("ghosts")
            stats = stored.get("stats")
            if isinstance(stats, dict) and stats.get("engine") == ENGINE_VERSION:
                self._ghosts = ghosts if isinstance(ghosts, list) else []
                self._stats = stats
            else:
                # 旧版本（基于存储层查询）的结论已失效，直接丢弃，避免误导
                self._ghosts = []
                self._stats = {}
                logger.info("幽灵整理记录：历史扫描结果来自旧判定引擎，已丢弃")

    def __save(self):
        try:
            self.save_data("status", {"ghosts": self._ghosts, "stats": self._stats})
        except Exception as err:
            logger.error(f"幽灵整理记录：保存扫描结果失败：{err}")
