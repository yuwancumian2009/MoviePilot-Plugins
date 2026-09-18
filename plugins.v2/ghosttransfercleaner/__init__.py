"""
幽灵整理记录（GhostTransferCleaner）

场景说明
--------
MoviePilot 在整理入库成功后会写入一条「整理记录」（transferhistory）。
自动整理的判定逻辑是：**只要存在一条整理成功的历史记录，就跳过整理**
（v2 源码 app/chain/transfer.py: "已成功转移过，如需重新处理，请删除历史记录"）。

于是会出现这种情况：媒体文件被手工误删后，整理记录仍然留在库里。
此后再次下载同一资源，MP 会因为「已有整理记录」而直接跳过整理，无法自动入库。

本插件用于体检这类「幽灵整理记录」：整理记录还在，但它记录的目标路径
（以及记录中的媒体文件）在当前存储上已经不存在了。

判定分级
--------
- 整部缺失：目标路径不存在，且其上级目录也不存在 —— 说明整部媒体都没了，
  属于高可信幽灵记录，可安全清理。
- 局部缺失：目标路径不存在，但上级目录仍在 —— 可能只是删了其中几集，
  也可能是记录已过时，需要人工确认后再清理。

安全设计
--------
1. 只读取整理记录，不触碰任何媒体文件；清理仅删除数据库里的整理记录。
2. 默认只用文件系统/存储层事实做判断，不做任何猜测。
3. 一键清理需要先在插件配置中显式打开「允许一键清理」开关。
4. 若超过八成的记录都被判定为丢失，视为「媒体库挂载路径可能发生变化」的
   异常信号，自动禁止清理并给出提示。
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
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

# 单次扫描的最长耗时（秒），防止网络存储检查把请求挂死
SCAN_DEADLINE = 60
# 页面最多展示条数
PAGE_LIMIT = 200
# 最多缓存的幽灵记录条数
CACHE_LIMIT = 1000
# 判定为「异常占比过高」的最低样本量
SUSPICIOUS_MIN_SAMPLE = 20
# 判定为「异常占比过高」的比例
SUSPICIOUS_RATIO = 0.8

# 本地存储的别名（不同版本/配置下可能是空值、local 等）
LOCAL_STORAGE_ALIAS = {"", "local", "localstorage", "local_storage"}


class GhostTransferCleaner(_PluginBase):
    """幽灵整理记录：体检并清理「整理记录还在、媒体文件已丢失」的记录。"""

    # 插件名称
    plugin_name = "幽灵整理记录"
    # 插件描述
    plugin_desc = "体检「整理记录还在、媒体文件已丢失」的幽灵记录，支持一键清理，恢复正常自动整理入库。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/clean.png"
    # 插件版本
    plugin_version = "1.0.0"
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
    _storage_chain: Any = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None):
        """根据插件配置初始化运行状态。"""
        # 重置配置
        self._enabled = False
        self._notify = True
        self._check_files = False
        self._only_whole = True
        self._allow_clean = False
        self._auto_clean = False
        self._min_age_days = 0
        self._max_records = 5000
        self._cron = ""

        scan_now = False
        clean_now = False
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
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
                                            "label": "深度检查（按记录内的文件清单判断）",
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
                                            "text": "本插件只读取整理记录并做路径存在性判断，不会删除任何媒体文件。"
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
                        "text": "⚠️ 超过八成整理记录都被判定为丢失，这更像媒体库挂载路径发生了变化"
                                "（例如容器内路径被改动），而不是文件真的被删。已自动禁止清理，"
                                "请先核对 MoviePilot 的目录映射再操作。",
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
            "「整部缺失」表示目标路径及其上级目录都已不存在，可信度最高；"
            "「局部缺失」表示上级目录还在，可能只是删了其中几集，建议先人工确认。"
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
                f"幽灵整理记录：体检完成，检查 {scanned}/{total} 条，"
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
        storage = str(row.get("dest_storage") or row.get("src_storage") or "").strip()
        # 路径原样保留用于存在性判断（Linux 下反斜杠是合法文件名字符），仅展示时统一分隔符
        dests = [
            item.strip()
            for item in str(raw_dest).replace("\r", "\n").split("\n")
            if item.strip()
        ]
        if not dests:
            return "unknown", None

        states = [self.__exists(path, storage) for path in dests]
        if all(state is None for state in states):
            return "unknown", None
        dest_exists = any(state is True for state in states)

        # 记录中的媒体文件（可选深度检查）
        file_paths: List[str] = []
        files_left = 0
        files_missing = 0
        if self._check_files:
            file_paths = self.__extract_paths(row.get("files"))
            for path in file_paths:
                state = self.__exists(path, storage)
                if state is True:
                    files_left += 1
                elif state is False:
                    files_missing += 1

        if dest_exists:
            # 目标路径还在：只有开启深度检查、且记录中的媒体文件全部缺失时才判定为幽灵
            if not self._check_files or not file_paths or files_left > 0:
                return "ok", None
            level = LEVEL_PART
            reason = f"目标路径仍在，但记录中的 {files_missing} 个媒体文件已全部丢失"
        else:
            if self._check_files and file_paths and files_left > 0:
                return "ok", None
            parents = {self.__exists(str(Path(path).parent), storage) for path in dests}
            if parents == {False} or (False in parents and True not in parents and None not in parents):
                level = LEVEL_WHOLE
                reason = "目标路径及其上级目录均已不存在（整部媒体已丢失）"
            else:
                level = LEVEL_PART
                reason = "目标路径已不存在（上级目录仍在）"

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
            "storage": storage or "local",
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
    # 存在性判断
    # ------------------------------------------------------------------
    def __exists(self, path: str, storage: str) -> Optional[bool]:
        """
        判断路径是否存在。
        :return True 存在 / False 不存在 / None 无法判断
        """
        if not path:
            return None
        path = str(path).strip()
        if not path:
            return None

        # 本地存储（或未声明存储类型）直接用文件系统判断
        if storage.strip().lower() in LOCAL_STORAGE_ALIAS:
            try:
                return Path(path).exists()
            except Exception as err:
                logger.debug(f"幽灵整理记录：检查本地路径 {path} 失败：{err}")
                return None

        # 其它存储走存储链
        chain = self.__get_storage_chain()
        if chain is None:
            # 存储链不可用时退回本地判断，避免整条记录被误判
            try:
                if Path(path).exists():
                    return True
            except Exception:
                pass
            return None
        try:
            return bool(chain.get_file_item(storage=storage, path=Path(path)))
        except Exception as err:
            logger.debug(f"幽灵整理记录：通过存储链检查 {storage}:{path} 失败：{err}")
            return None

    def __get_storage_chain(self):
        if self._storage_chain is not None:
            # False 表示当前版本不可用
            return self._storage_chain or None
        try:
            from app.chain.storage import StorageChain

            self._storage_chain = StorageChain()
            logger.debug("幽灵整理记录：已启用存储链用于远端存储检查")
        except Exception as err:
            logger.debug(f"幽灵整理记录：当前版本无可用存储链，改为本地路径判断：{err}")
            self._storage_chain = False
        return self._storage_chain or None

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
                    f"已检查整理记录 {stats.get('scanned', 0)} 条，"
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
                lines.append("⚠️ 异常记录占比过高，疑似媒体库路径变更，已中止清理，请先核对目录映射。")
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
            self._ghosts = ghosts if isinstance(ghosts, list) else []
            self._stats = stats if isinstance(stats, dict) else {}

    def __save(self):
        try:
            self.save_data("status", {"ghosts": self._ghosts, "stats": self._stats})
        except Exception as err:
            logger.error(f"幽灵整理记录：保存扫描结果失败：{err}")
