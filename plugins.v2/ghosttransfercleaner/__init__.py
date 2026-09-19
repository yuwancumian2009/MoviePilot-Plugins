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

诊断报告
--------
只给汇总数字（「发现 N 条幽灵记录」）无法回答「为什么全都对不上」。
数据页面提供「生成诊断报告」：抽一小批整理记录逐条展开，把
「记录里的路径」与「strm 库里的实际目录」摆在一起对照，并给出
strm 根目录体检结果、记录侧路径画像、交叉命中率与整改建议。
报告只读本地文件系统，同样不访问云端。

报告采用「后台生成 + 通知交付」：

- MoviePilot 前端的事件处理是 `try { 调接口 } catch { console.error }`，
  接口一旦异常，页面只会静默关掉进度框、不给任何提示，用户看到的就是
  「点了没反应」。而本报告要遍历 strm 目录树，耗时可达几十秒，
  同步接口很容易被判定成无响应。
- 因此接口只负责「启动」，立刻返回；报告在后台线程里生成，
  完成后**无论成功失败都通过通知渠道推送**，失败时连错误原因一起发出，
  这样即使页面不刷新，用户在手机上也一定看得到结果。
- 通知正文只保留「结论 / 逐条对照（前几条）/ 交叉命中 / 建议」，
  完整报告写入插件目录的 `diagnose_report.txt`，并整篇打进 MP 日志。
- 另外提供 `/ghostdiag` 远程命令作为入口，不依赖页面按钮。

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
import re
import threading
import time
from collections import Counter
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

# 诊断报告里的比对结论文案（比扫描分级多出「正常」「无法判断」两种）
PROBE_TEXT = {
    STATE_OK: "正常 —— 该路径对应的 strm 仍在库中",
    LEVEL_PART: "局部缺失 —— 节目目录还在，但该文件对应的 strm 不在",
    LEVEL_WHOLE: "整部缺失 —— strm 库里连该节目目录都找不到",
    STATE_UNKNOWN: "无法判断 —— 记录里没有可用的目标路径",
}

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

# ---- 诊断报告参数 ----
# 报告中逐条对照的样例条数
DIAG_SAMPLE = 12
# 每个根目录在报告里展示的顶层条目数
DIAG_ROOT_TOP = 20
# 反查目录时展示单个目录内的条目数
DIAG_PEEK = 8
# 每条记录在每个根目录下最多记录的尝试层级数
DIAG_MAX_ATTEMPTS = 6
# 建立「目录名索引」时遍历的最大深度与最大节点数
DIAG_INDEX_DEPTH = 3
DIAG_INDEX_LIMIT = 4000
# 统计 .strm 时遍历的最大节点数与最长耗时（秒）
DIAG_WALK_LIMIT = 40000
DIAG_WALK_SECONDS = 8

# ---- 通知推送参数 ----
# 通知正文的长度上限：各渠道限制不同（企业微信最短，Telegram 4096），
# 这里取一个能装下「结论 + 逐条对照 + 建议」的值；更短的渠道由 MP 自行截断，
# 所以章节顺序按重要性排（结论最前），截断也只丢末尾的次要统计。
NOTIFY_LIMIT = 2800
# 通知里保留几条「逐条对照」——这是定位问题最关键的证据
NOTIFY_ITEMS = 3
# 完整报告落盘的文件名（写在插件目录下）
REPORT_FILENAME = "diagnose_report.txt"


class GhostTransferCleaner(_PluginBase):
    """幽灵整理记录：体检并清理「整理记录还在、媒体文件已丢失」的记录。"""

    # 插件名称
    plugin_name = "幽灵整理记录"
    # 插件描述
    plugin_desc = "体检「整理记录还在、媒体文件已丢失」的幽灵记录，支持一键清理，恢复正常自动整理入库。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/clean.png"
    # 插件版本
    plugin_version = "1.3.0"
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
    _notify_report = True
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
    _report: str = ""
    _report_time: str = ""
    _report_path: str = ""
    _scanning = False
    _diagnosing = False
    # 完整报告的存放目录覆盖项（留空则自动推断，主要供离线测试使用）
    _report_dir_override = ""
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
        self._notify_report = True
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
        self._report = ""
        self._report_time = ""
        self._report_path = ""
        self._diagnosing = False

        scan_now = False
        clean_now = False
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._notify_report = bool(config.get("notify_report", True))
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
            },
            {
                "cmd": "/ghostdiag",
                "event": EventType.PluginAction,
                "desc": "生成诊断报告并推送到通知渠道",
                "category": "插件",
                "data": {"action": "ghosttransfercleaner_diag"},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        """响应 /ghost 与 /ghostdiag 远程命令。"""
        try:
            data = (event.event_data if event else None) or {}
            action = data.get("action")
            if action == "ghosttransfercleaner_scan":
                self.__scan(clean=False, notify=True)
            elif action == "ghosttransfercleaner_diag":
                # 命令入口不依赖前端页面，即使页面上的按钮出问题也能拿到报告
                self.__start_diagnose(source="远程命令 /ghostdiag")
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
            {
                "path": f"/{pid}/diagnose",
                "endpoint": self.api_diagnose,
                "methods": ["GET", "POST"],
                "summary": "生成诊断报告",
                "description": "抽查整理记录并与本地 strm 目录逐条对照，"
                               "输出可用于定位「为什么全都对不上」的具体报告；"
                               "后台生成，完成后推送到通知渠道",
            },
            {
                "path": f"/{pid}/notify_report",
                "endpoint": self.api_notify_report,
                "methods": ["GET", "POST"],
                "summary": "把诊断报告发送到通知渠道",
                "description": "将最近一次生成的诊断报告摘要重新推送到 MP 通知渠道",
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

    def api_diagnose(self) -> Dict[str, Any]:
        """
        触发生成诊断报告（后台执行，完成后推送到通知渠道）。

        之所以不在接口里同步生成：报告要遍历 strm 目录树，耗时可达几十秒，
        而前端在请求异常时只会静默关闭进度框、不给任何提示，
        表现为「点了没反应」。改成后台生成后，接口秒回，
        结果一律通过通知渠道交付，成功失败都看得见。
        """
        started, message = self.__start_diagnose(source="插件数据页面")
        return {"success": True, "message": message, "data": {"running": started}}

    def api_notify_report(self) -> Dict[str, Any]:
        """把最近一次生成的诊断报告摘要重新推送到通知渠道。"""
        if not self._report:
            return {
                "success": False,
                "message": "还没有生成过诊断报告，请先点「生成诊断报告」",
                "data": {},
            }
        self.__send_report(self._report, note="手动重发")
        return {
            "success": True,
            "message": "诊断报告已推送到通知渠道",
            "data": {"report_time": self._report_time, "path": self._report_path},
        }

    def __start_diagnose(self, source: str = "") -> Tuple[bool, str]:
        """
        启动一次后台诊断（不阻塞请求，与页面刷新无关）。

        :return (是否成功启动, 给用户看的结果说明)
        """
        with self._lock:
            if self._diagnosing:
                return False, "诊断报告正在生成中，完成后会推送到通知渠道，请稍候…"
            self._diagnosing = True
        logger.info(f"幽灵整理记录：开始生成诊断报告（来源：{source or '未知'}）")
        threading.Thread(target=self.__diagnose_task, daemon=True).start()
        return True, (
            "诊断已开始，正在后台生成（要遍历 strm 目录，可能需要几十秒）。"
            "完成后会自动推送到通知渠道；稍后刷新本页面即可看到完整报告。"
        )

    def __diagnose_task(self):
        """后台生成诊断报告：落盘、写日志，并按配置推送到通知渠道。"""
        try:
            try:
                report = self.__diagnose()
            except Exception as err:
                logger.error(f"幽灵整理记录：生成诊断报告失败：{err}")
                # 前端在接口异常时不给任何提示，失败也必须走通知，
                # 否则用户端看到的仍然是「点了没反应」
                self.__notify_text(
                    "幽灵整理记录 · 诊断报告",
                    f"生成诊断报告失败：{err}\n"
                    "请把上面这句报错发给我；若与 strm 根目录有关，"
                    "请先在插件配置里核对容器内的路径是否正确。",
                )
                return

            try:
                self._report_path = self.__write_report_file(report)
            except Exception as err:
                logger.error(f"幽灵整理记录：写出诊断报告文件失败：{err}")
                self._report_path = ""
            # 把报告文件路径一并落盘，重启后「重发到通知」仍能给出正确位置
            self.__save()

            if self._notify_report:
                self.__send_report(report)
            else:
                logger.info("幽灵整理记录：诊断报告已生成（未开启推送，可在页面查看）")
        finally:
            # 必须等「落盘 + 推送」全部做完才复位：若在生成报告后立刻复位，
            # 外部（页面刷新、定时任务、测试）会误以为任务已结束，
            # 而实际上通知还没发出去。
            self._diagnosing = False

    def __send_report(self, report: str, note: str = ""):
        """把诊断报告裁剪成通知能承受的长度发出，并附上完整报告的获取方式。"""
        if self._report_path:
            tail = f"完整报告：{self._report_path}"
        else:
            tail = "完整报告见 MP 日志（搜「幽灵整理记录｜诊断报告」）"
        head = f"生成于 {self._report_time or '-'}"
        if note:
            head += f"（{note}）"
        text = f"{head}\n\n{self.__digest_report(report)}\n\n（{tail}）"
        self.__notify_text("幽灵整理记录 · 诊断报告", text)

    def __digest_report(self, report: str) -> str:
        """
        把诊断报告裁剪到通知渠道能承受的长度。

        保留「结论 / 逐条对照（前几条）/ 建议 / 交叉命中」这四节 ——
        它们才是定位问题真正需要的信息，其余明细留给完整报告。

        顺序即优先级：结论（是什么问题）与逐条对照（证据）放最前，
        交叉命中放最后，这样短渠道截断时丢的是最不关键的那一段。
        """
        blocks: List[Tuple[str, str]] = []
        # 报告末尾的「报告完 · 耗时…」与结束线不属于任何一节，先摘掉，
        # 否则会黏在最后一节（建议）的正文后面
        whole = str(report).split("\n报告完 ·")[0]
        # 节标题固定是「行首」的【；正文里也会出现【】——例如结论里的
        # 「这【不是「媒体被删」的特征】」——所以必须锚定行首，
        # 用裸的 split("【") 会把结论拦腰截断。
        for chunk in re.split(r"(?m)^【", whole)[1:]:
            title, _, body = chunk.partition("】")
            blocks.append((title.strip(), body.strip("\n")))

        def pick(keyword: str) -> Tuple[str, str]:
            for title, body in blocks:
                if keyword in title:
                    return title, body
            return "", ""

        parts: List[str] = []
        for keyword in ("结论", "逐条对照", "建议", "交叉命中"):
            title, body = pick(keyword)
            if not title:
                continue
            if keyword == "逐条对照":
                body = self.__trim_items(body)
            parts.append(f"【{title}】\n{body}")

        text = "\n\n".join(parts) if parts else str(report)
        if len(text) > NOTIFY_LIMIT:
            # 按行截断，避免把某一行切成半句
            clipped = text[:NOTIFY_LIMIT]
            cut = clipped.rfind("\n")
            if cut > NOTIFY_LIMIT // 2:
                clipped = clipped[:cut]
            text = clipped.rstrip() + "\n…（通知已截断，完整内容见下方说明）"
        return text

    @staticmethod
    def __trim_items(body: str, count: int = NOTIFY_ITEMS) -> str:
        """逐条对照只保留前几条 —— 它们在通知里最有价值，篇幅也最省。"""
        matches = list(re.finditer(r"(?m)^  \[(\d+)\] ", body))
        if len(matches) <= count:
            return body
        end = matches[count].start()
        return body[:end].rstrip() + f"\n  …（其余 {len(matches) - count} 条见完整报告）"

    def __report_dir(self) -> str:
        """
        完整报告的存放目录。

        优先使用 MoviePilot 提供的数据目录（若该版本提供），
        其次退化为插件自身目录 —— 两种位置用户都能直接找到文件。
        """
        if self._report_dir_override:
            return self._report_dir_override
        getter = getattr(self, "get_data_path", None)
        if callable(getter):
            try:
                path = str(getter() or "").strip()
                if path:
                    return path
            except Exception as err:
                logger.debug(f"幽灵整理记录：获取插件数据目录失败，改用插件目录：{err}")
        return os.path.dirname(os.path.abspath(__file__))

    def __write_report_file(self, report: str) -> str:
        """把完整报告写到磁盘，便于用户直接取阅（失败不影响其它流程）。"""
        directory = self.__report_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, REPORT_FILENAME)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(report)
        return path

    def __notify_text(self, title: str, text: str):
        """通过 MP 通知渠道发送一条纯文本消息。"""
        try:
            self.systemmessage.put(str(text), title=title)
        except Exception as err:
            logger.error(f"幽灵整理记录：发送通知失败：{err}")

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
                                            "model": "notify_report",
                                            "label": "诊断报告生成后发送到通知渠道",
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
            "notify_report": True,
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
                        "props": {"cols": 12, "class": "d-flex flex-wrap ga-2 mb-2"},
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
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "info",
                                    "variant": "tonal",
                                    "size": "small",
                                    "prepend-icon": "mdi-clipboard-text-search",
                                },
                                "text": "生成诊断报告",
                                "events": {
                                    "click": {"api": f"/plugin/{pid}/diagnose", "method": "POST"}
                                },
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "secondary",
                                    "variant": "tonal",
                                    "size": "small",
                                    "prepend-icon": "mdi-bell-send",
                                },
                                "text": "把报告发到通知",
                                "events": {
                                    "click": {
                                        "api": f"/plugin/{pid}/notify_report",
                                        "method": "POST",
                                    }
                                },
                            },
                        ],
                    }
                ],
            }
        )

        if self._report:
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": f"诊断报告（生成于 {self._report_time or '-'}）："
                                "把整理记录里的路径与 strm 库里的实际目录逐条摆在一起对照，"
                                "用于判断到底是「文件真被删」还是「目录配置/结构对不上」。"
                                "报告只读本地文件系统，不访问云端；文本可长按选择复制。"
                                + (f"完整报告已写入：{self._report_path}" if self._report_path else ""),
                    },
                }
            )
            content.append(
                {
                    "component": "VCard",
                    "props": {
                        "variant": "outlined",
                        "class": "pa-3",
                        "style": "white-space: pre-wrap; word-break: break-all;"
                                 " font-family: ui-monospace, Consolas, 'Courier New', monospace;"
                                 " font-size: 12px; line-height: 1.5;"
                                 " max-height: 50vh; overflow: auto;",
                    },
                    "text": self._report,
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
    # 诊断报告（只读本地文件系统，同样不访问云端）
    # ------------------------------------------------------------------
    def __diagnose(self) -> str:
        """
        生成诊断报告。

        汇总数字回答不了「为什么这么多记录都找不到对应 strm」，所以报告的做法是
        抽一小批整理记录逐条展开，把「记录里的路径」与「strm 库里的实际目录」
        摆在一起对照，再给出根目录体检、路径画像、交叉命中率与整改建议。
        """
        started = time.time()
        root_infos = [self.__inspect_root(root) for root in self._strm_paths]

        rows: List[Dict[str, Any]] = []
        total = 0
        load_error = ""
        if self._strm_paths:
            try:
                rows, total = self.__load_records()
            except Exception as err:  # 数据库不可用时也要能出报告
                load_error = str(err)
                logger.error(f"幽灵整理记录：诊断报告读取整理记录失败：{err}")

        sample = self.__sample(rows, DIAG_SAMPLE)

        # ---- 记录侧画像 ----
        top_counter: Counter = Counter()
        ext_counter: Counter = Counter()
        for row in rows:
            normalized = self.__normalize(self.__first_dest(row))
            if not normalized:
                continue
            top_counter[self.__top_segment(normalized)] += 1
            ext_counter[self.__ext_of(normalized)] += 1

        # ---- 逐条对照 ----
        traces = [self.__trace(self.__first_dest(row), root_infos) for row in sample]

        # ---- 交叉命中统计 ----
        strm_index: Dict[str, List[str]] = {}
        for root_info in root_infos:
            for key, places in root_info["strm_index"].items():
                bucket = strm_index.setdefault(key, [])
                for place in places:
                    if len(bucket) < 3:
                        bucket.append(f"{root_info['path']}/{place}")
        dir_index: Dict[str, List[str]] = {}
        for root_info in root_infos:
            for key, places in root_info["index"].items():
                bucket = dir_index.setdefault(key, [])
                for place in places:
                    if len(bucket) < 3:
                        bucket.append(f"{root_info['path']}/{place}")

        name_hit = 0
        dir_hit = 0
        for trace in traces:
            if not trace["segments"]:
                continue
            if any(name.lower() in strm_index for name in trace["names"]):
                name_hit += 1
            parents = trace["segments"][:-1]
            if parents and parents[-1].lower() in dir_index:
                dir_hit += 1

        root_tops = []
        for root_info in root_infos:
            if root_info["isdir"]:
                root_tops.extend(
                    name for name, is_dir in root_info["top"] if is_dir
                )
        root_top_keys = {name.lower() for name in root_tops}
        record_top_keys = {name.lower() for name in top_counter}
        overlap = sorted(root_top_keys & record_top_keys)

        # ---- 组装报告 ----
        lines: List[str] = []
        add = lines.append

        def section(title: str):
            add("")
            add(f"【{title}】")

        def bullet(text: str, indent: int = 1):
            add("  " * indent + "· " + text)

        add("=" * 72)
        add("幽灵整理记录 · 诊断报告")
        add(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        add(
            f"插件版本：{self.plugin_version}    判定引擎：v{ENGINE_VERSION}"
            "（只比对 NAS 本地 strm 文件，不访问云端）"
        )
        add("=" * 72)

        # 一、结论
        section("一、结论（先看这里）")
        if not self._strm_paths:
            bullet("根因明确：尚未配置「strm 媒体库根目录」，插件不给出任何判定。")
            bullet("请先到插件配置里填写容器内的 strm 根目录（例如 /media/strm）再扫描。")
        elif not any(info["isdir"] for info in root_infos):
            bullet("根因明确：配置的 strm 根目录在容器内不存在或不是目录。")
            for info in root_infos:
                bullet(f"不可访问：{info['path']}", indent=2)
            bullet("请核对容器挂载（docker-compose 的 volumes / bind 路径），"
                   "确认该路径在 MoviePilot 容器里真的存在。")
        elif not rows:
            bullet("没有读到符合条件的整理记录，无法进一步分析。"
                   "请检查「只检查 N 天前」与「单次最多检查记录数」的设置。")
        elif len(traces) and name_hit == 0 and dir_hit == 0:
            bullet(
                f"抽查 {len(traces)} 条记录中，文件名的同名 strm 命中 {name_hit} 条、"
                f"末级目录名命中 {dir_hit} 条 —— 两边几乎毫无交集。"
            )
            bullet("这【不是「媒体被删」的特征】，更像是：strm 根目录填到了别的目录，"
                   "或整理记录指向的媒体库与这个 strm 库根本不是同一套。")
            bullet("在核对清楚之前，请不要点「清理幽灵记录」。")
        elif len(traces) and name_hit == 0:
            bullet(
                f"抽查 {len(traces)} 条中，末级目录名命中 {dir_hit} 条、"
                f"但没有任何一条的文件名能在 strm 库里找到同名 strm（命中 {name_hit} 条）。"
            )
            bullet("目录结构大致能对上，所以先别急着下结论：可能是这些 strm 真的不在了，"
                   "也可能是 strm 的命名规则与整理记录不同"
                   "（例如多了清晰度/来源后缀、季集写法不一样）。")
            bullet("请到「五、逐条对照」逐条看「该目录内含」列出的真实文件名，与记录里的文件名对照，"
                   "差异会直接暴露出来；确认清楚之前不要点「清理幽灵记录」。")
        else:
            bullet(
                f"抽查 {len(traces)} 条中，文件名命中 {name_hit} 条、末级目录名命中 {dir_hit} 条，"
                "路径结构与本地 strm 库基本能对上。"
            )
            bullet("此时「幽灵记录」才比较可能是真的被删了。仍建议先按「五、逐条对照」人工确认几条再清理。")

        # 二、配置快照
        section("二、配置快照")
        bullet(f"插件启用：{'是' if self._enabled else '否'}")
        bullet(f"strm 媒体库根目录：{len(self._strm_paths)} 个")
        for index, root in enumerate(self._strm_paths, 1):
            bullet(f"[{index}] {root}", indent=2)
        if not self._strm_paths:
            bullet("（空 —— 这是当前唯一且最可能的根因）", indent=2)
        bullet(
            f"路径前缀映射：{len(self._path_map)} 条"
            + ("（未配置）" if not self._path_map else "")
        )
        for old, new in self._path_map:
            bullet(f"{old}  =>  {new}", indent=2)
        bullet(f"单次最多检查记录数：{self._max_records}")
        bullet(f"只检查 N 天前的记录：{self._min_age_days}（0 表示全部）")
        bullet(f"深度检查（逐个文件核对 strm）：{'开' if self._check_files else '关'}")
        bullet(f"只清理「整部缺失」：{'是' if self._only_whole else '否'}")
        bullet(f"允许一键清理：{'是' if self._allow_clean else '否'}")
        bullet(f"定时扫描 cron：{self._cron or '（未设置）'}")
        if self._stats:
            bullet(
                f"最近一次扫描：{self._stats.get('scan_time', '-')}｜"
                f"检查 {self._stats.get('scanned', 0)} 条｜"
                f"幽灵 {self._stats.get('ghost', 0)} 条"
                f"（整部 {self._stats.get('whole', 0)}、局部 {self._stats.get('part', 0)}）"
            )
            if self._stats.get("suspicious"):
                bullet("最近一次扫描触发了「占比过高」保护，已自动禁止清理。")

        # 三、strm 媒体库体检
        section("三、strm 媒体库体检")
        if not root_infos:
            bullet("未配置根目录，跳过。")
        for index, info in enumerate(root_infos, 1):
            add(f"  [{index}] {info['path']}")
            if not info["isdir"]:
                add("      · 是否目录：否 —— 容器内不存在或不可读！")
                continue
            dirs = [name for name, is_dir in info["top"] if is_dir]
            files = [name for name, is_dir in info["top"] if not is_dir]
            add("      · 是否目录：是")
            add(
                f"      · 顶层条目 {len(info['top'])} 个"
                f"（目录 {len(dirs)} / 文件 {len(files)}，最多展示 {DIAG_ROOT_TOP} 个）："
            )
            for name, is_dir in info["top"][:DIAG_ROOT_TOP]:
                add(f"          [{'目录' if is_dir else '文件'}] {name}")
            if len(info["top"]) > DIAG_ROOT_TOP:
                add(f"          …（其余 {len(info['top']) - DIAG_ROOT_TOP} 个省略）")
            add(
                f"      · 目录名索引：{info['index_nodes']} 个目录"
                f"（深度 ≤ {DIAG_INDEX_DEPTH}）"
                + ("，已达上限提前结束" if info["index_truncated"] else "")
            )
            add(
                f"      · .strm 文件计数：{info['strm_count']} 个"
                + ("（遍历超限提前结束，实际更多）" if info["walk_truncated"] else "")
            )

        # 四、整理记录侧画像
        section("四、整理记录侧画像")
        if load_error:
            bullet(f"读取整理记录失败：{load_error}")
        else:
            bullet(f"符合条件的整理记录共 {total} 条，本次读取 {len(rows)} 条，抽查 {len(traces)} 条")
            bullet("目标路径（dest）顶层目录分布（最多 10 项）：")
            if top_counter:
                for name, count in top_counter.most_common(10):
                    add(f"          {count:>6} 次     {name}")
            else:
                add("          （无可用路径）")
            bullet("目标文件名扩展名分布（最多 10 项）：")
            if ext_counter:
                for name, count in ext_counter.most_common(10):
                    add(f"          {count:>6} 次     {name}")
            else:
                add("          （无可用路径）")

        # 五、逐条对照
        section(f"五、逐条对照（抽查 {len(traces)} 条）")
        if not traces:
            bullet("没有可对照的记录。")
        for index, trace in enumerate(traces, 1):
            row = sample[index - 1]
            label = " ".join(
                part for part in [
                    str(row.get("title") or ""),
                    str(row.get("year") or ""),
                    f"{row.get('seasons') or ''}{row.get('episodes') or ''}".strip(),
                ] if part
            )
            add("")
            add(f"  [{index}] #{row.get('id')} {label or '（无标题）'}")
            add(f"      整理时间：{row.get('date') or '-'}    类型：{row.get('type') or '-'}")
            add(f"      本路径比对：{PROBE_TEXT.get(trace['level'], '未知状态')}")
            add(f"      记录目标路径（原样）：{trace['raw'] or '（空）'}")
            if trace["normalized"] != trace["raw"]:
                add(f"      归一化（应用前缀映射后）：{trace['normalized']}")
            if not trace["segments"]:
                add("      该记录没有可用的目标路径，已跳过比对（也不会据此判为幽灵）")
                continue
            add(f"      文件名候选：{'、'.join(trace['names']) or '（无）'}")
            for unit in trace["roots"]:
                add(f"      在 {unit['root']} 下逐级剥离前导目录尝试：")
                if not unit["levels"]:
                    add("          （无可尝试的层级）")
                for level in unit["levels"]:
                    # 根目录本身必然存在，三、体检区已列出内容，逐条重复没有信息量
                    if not level["rel"] and not level["hit"]:
                        continue
                    rel = level["rel"] or "（根目录）"
                    if level["hit"] and level["hit_is_dir"]:
                        add(f"          ● {rel}/{level['hit']}    命中一个同名「目录」"
                            "（记录指向的是目录本身，不是文件）")
                    elif level["hit"]:
                        add(f"          ✓ {rel}/{level['hit']}    ← 命中对应 strm")
                    elif level["dir_ok"]:
                        peek = "、".join(level["peek"]) or "（空目录）"
                        add(f"          ✗ {rel}/…    目录存在，但没找到对应文件；"
                            f"该目录实际内含：{peek}")
                    else:
                        add(f"          ✗ {rel}/…    该层级目录不存在")
                if unit["repeat"]:
                    add(f"          …（更深层级已省略 {unit['repeat']} 次尝试）")
            hits = self.__dir_hits(trace["segments"][:-1], dir_index)
            if hits:
                add("      记录里的目录名在 strm 库中的落点（最能说明问题）：")
                for hit in hits:
                    if hit["places"]:
                        add(f"          「{hit['name']}」→ 命中 {len(hit['places'])} 处："
                            + "、".join(hit["places"]))
                    else:
                        add(f"          「{hit['name']}」→ strm 库中不存在同名目录")

        # 六、交叉命中统计
        section("六、交叉命中统计")
        bullet(
            f"抽查 {len(traces)} 条中：文件名同名 strm 命中 {name_hit} 条、"
            f"末级目录名命中 {dir_hit} 条"
        )
        bullet("记录里的顶层目录（最多 10 项）：" + ("、".join(
            name for name, _ in top_counter.most_common(10)) or "（无）"))
        bullet("strm 库的顶层目录（最多 10 项）：" + ("、".join(root_tops[:10]) or "（无）"))
        bullet(
            "两边顶层目录的交集：" + ("、".join(overlap) if overlap else "无 —— 完全不重合")
        )

        # 七、建议
        section("七、建议")
        suggestions: List[str] = []
        if not self._strm_paths:
            suggestions.append(
                "1. 到插件配置填写「strm 媒体库根目录」（容器内路径，例如 /media/strm），保存后再扫描。"
            )
        elif not any(info["isdir"] for info in root_infos):
            suggestions.append(
                "1. 上面标注「是否目录：否」的路径在容器内不可见。"
                "请检查 MoviePilot 容器的挂载配置，把 strm 所在目录映射进容器。"
            )
        else:
            if len(traces) and name_hit == 0 and dir_hit == 0:
                suggestions.append(
                    "文件名与目录名都命中不了，先不要清理。请把「三、strm 媒体库体检」里列出的顶层目录，"
                    "与你印象中的媒体库结构核对一遍，确认这个根目录就是 strm 库本身"
                    "（常见的错法：填成了上级目录、或填成了下载目录）。"
                )
                suggestions.append(
                    "若记录的 dest 前几级是云端/下载器路径（如 /115、/我的资源），"
                    "而本地 strm 库的顶层是「电影、电视剧」这一类，请用「路径前缀映射」"
                    "把记录前缀改写到本地前缀，例如填一行：  /115=/media/strm"
                )
                suggestions.append(
                    "若确认两套库确实对不上（例如 strm 是另一台机器生成的），"
                    "说明本插件在当前配置下无法判断，此时不要使用清理功能。"
                )
            elif len(traces) and name_hit == 0:
                suggestions.append(
                    "目录能对上、文件名对不上。请把「五、逐条对照」里的「记录目标路径」与"
                    "「该目录实际内含」两份文件名并排看，差异通常落在：清晰度/来源后缀、"
                    "季集写法（S01E01 / 第01集 / EP01）、是否带年份，或多出一层点号。"
                )
                suggestions.append(
                    "若确认只是命名规则不同，说明本插件的「后缀匹配」不适配你的命名，"
                    "此时不要使用清理功能（会误删仍然有效的记录）。"
                )
            else:
                suggestions.append(
                    "抽查结果说明路径结构基本能对上，此时「幽灵记录」的可信度较高；"
                    "但仍建议先按「五、逐条对照」人工确认几条，再开启清理。"
                )

        # 与根因无关的通用建议
        if self._strm_paths and any(info["isdir"] for info in root_infos):
            suggestions.append(
                "需要缩小范围时，可把「单次最多检查记录数」调小（例如 200）后重扫，"
                "先看小样本结论，确认无误再放大范围。"
            )
            if not self._only_whole:
                suggestions.append(
                    "当前「只清理整部缺失」是关闭的，清理会同时删掉「局部缺失」的记录，"
                    "建议先打开该开关，只清理可信度最高的那部分。"
                )

        if not suggestions:
            add("  （暂无）")
        for number, item in enumerate(suggestions, 1):
            add(f"  {number}. {item}")

        add("")
        add(
            f"报告完 · 生成耗时 {round(time.time() - started, 1)} 秒 · "
            f"本报告只读取本地文件系统与整理记录，未访问任何云端接口"
        )
        add("=" * 72)

        report = "\n".join(lines)

        # 落盘 + 写日志，便于在 MP 日志里取全文（作为一条记录，不刷屏）
        self._report = report
        self._report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.__save()
        try:
            logger.info(f"幽灵整理记录｜诊断报告｜{self._report_time}\n{report}")
        except Exception as err:
            logger.error(f"幽灵整理记录：输出诊断报告到日志失败：{err}")
        return report

    def __inspect_root(self, root: str) -> Dict[str, Any]:
        """体检一个 strm 根目录：是否存在、顶层结构、目录名索引与 .strm 计数。"""
        info: Dict[str, Any] = {
            "path": root,
            "isdir": False,
            "top": [],
            "index": {},
            "index_nodes": 0,
            "index_truncated": False,
            "strm_index": {},
            "strm_count": 0,
            "walk_truncated": False,
        }
        top = self.__list_named(root)
        if top is None:
            return info
        info["isdir"] = True
        info["top"] = top

        index, nodes, truncated = self.__index_dirs(root)
        info["index"] = index
        info["index_nodes"] = nodes
        info["index_truncated"] = truncated

        count, strm_index, walk_truncated = self.__walk_strm(root)
        info["strm_count"] = count
        info["strm_index"] = strm_index
        info["walk_truncated"] = walk_truncated
        return info

    def __index_dirs(self, root: str) -> Tuple[Dict[str, List[str]], int, bool]:
        """
        建立「目录名（小写） → 相对路径列表」索引。

        用于反查「整理记录里的某一级目录名」在 strm 库里究竟落在哪里，
        这是判断「目录结构是否对得上」最直接的证据。

        :return (索引, 已遍历目录数, 是否因超限提前结束)
        """
        index: Dict[str, List[str]] = {}
        nodes = 0
        queue: List[Tuple[str, int]] = [("", 0)]
        while queue:
            relative, depth = queue.pop(0)
            entries = self.__list_named(
                self.__join(root, [seg for seg in relative.split("/") if seg])
            )
            if entries is None:
                continue
            for name, is_dir in entries:
                if not is_dir:
                    continue
                nodes += 1
                if nodes > DIAG_INDEX_LIMIT:
                    return index, nodes, True
                child = f"{relative}/{name}" if relative else name
                bucket = index.setdefault(name.lower(), [])
                if len(bucket) < 3:
                    bucket.append(child)
                if depth + 1 < DIAG_INDEX_DEPTH:
                    queue.append((child, depth + 1))
        return index, nodes, False

    def __walk_strm(self, root: str) -> Tuple[int, Dict[str, List[str]], bool]:
        """
        遍历 strm 根目录，统计 .strm 数量并建立「文件名（小写） → 相对路径」索引。

        索引让我们能回答「这条记录对应的 strm 到底在不在库里、在哪儿」，
        比只看目标目录是否命中有用得多。

        :return (.strm 数量, 文件名索引, 是否因超限提前结束)
        """
        count = 0
        index: Dict[str, List[str]] = {}
        nodes = 0
        deadline = time.time() + DIAG_WALK_SECONDS
        stack: List[str] = [""]
        while stack:
            relative = stack.pop()
            entries = self.__list_named(
                self.__join(root, [seg for seg in relative.split("/") if seg])
            )
            if entries is None:
                continue
            for name, is_dir in entries:
                nodes += 1
                if nodes > DIAG_WALK_LIMIT or time.time() > deadline:
                    return count, index, True
                child = f"{relative}/{name}" if relative else name
                if is_dir:
                    stack.append(child)
                    continue
                if name.lower().endswith(STRM_SUFFIX):
                    count += 1
                    bucket = index.setdefault(name.lower(), [])
                    if len(bucket) < 3:
                        bucket.append(child)
        return count, index, False

    def __trace(self, path: str, root_infos: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        记录一条整理记录路径在 strm 库中的查找过程，供诊断报告展示。

        判定结果直接取 ``__probe_strm``，这里只负责把「试过哪些相对路径、
        每个层级目录是否存在、目录里实际有什么」如实记下来，二者永远一致。
        """
        normalized = self.__normalize(path)
        segments = [seg for seg in normalized.split("/") if seg]
        trace: Dict[str, Any] = {
            "raw": str(path or ""),
            "normalized": normalized,
            "segments": segments,
            "names": [],
            "roots": [],
            "level": STATE_UNKNOWN,
        }
        if not segments:
            return trace

        names = self.__strm_names(segments[-1])
        dirs = segments[:-1]
        trace["names"] = names
        trace["level"] = self.__probe_strm(path)

        for root_info in root_infos:
            root = root_info["path"]
            levels: List[Dict[str, Any]] = []
            repeat = 0
            for drop in range(0, min(len(dirs), MAX_TAIL_DEPTH) + 1):
                if len(levels) >= DIAG_MAX_ATTEMPTS:
                    repeat = min(len(dirs), MAX_TAIL_DEPTH) + 1 - len(levels)
                    break
                tail = dirs[drop:]
                entries = self.__list_named(self.__join(root, tail))
                item: Dict[str, Any] = {
                    "rel": "/".join(tail),
                    "dir_ok": entries is not None,
                    "hit": "",
                    "hit_is_dir": False,
                    "peek": [],
                }
                if entries is not None:
                    lookup = {name.lower(): (name, is_dir) for name, is_dir in entries}
                    for candidate in names:
                        found = lookup.get(candidate.lower())
                        if found is not None:
                            item["hit"] = found[0]
                            item["hit_is_dir"] = bool(found[1])
                            break
                    if not item["hit"]:
                        item["peek"] = [name for name, _ in entries[:DIAG_PEEK]]
                levels.append(item)
                if item["hit"]:
                    break
            trace["roots"].append({"root": root, "levels": levels, "repeat": repeat})
        return trace

    def __dir_hits(self, dirs: List[str], dir_index: Dict[str, List[str]]) -> List[Dict[str, Any]]:
        """反查记录里最后几级目录名在 strm 库中的落点（最深一级优先）。"""
        result: List[Dict[str, Any]] = []
        for segment in reversed([seg for seg in dirs if seg][-4:]):
            result.append(
                {"name": segment, "places": list(dir_index.get(segment.lower(), [])[:3])}
            )
        return result

    def __list_named(self, directory: str) -> Optional[List[Tuple[str, bool]]]:
        """
        列出目录内容并保留原始大小写，供诊断报告展示；不写入扫描缓存。
        目录不存在、不可读或不是目录时返回 None。
        """
        try:
            if not os.path.isdir(directory):
                return None
            result: List[Tuple[str, bool]] = []
            with os.scandir(directory) as iterator:
                for item in iterator:
                    try:
                        result.append((item.name, item.is_dir()))
                    except OSError:
                        continue
            result.sort(key=lambda pair: (not pair[1], pair[0].lower()))
            return result
        except OSError as err:
            logger.debug(f"幽灵整理记录：诊断时读取目录 {directory} 失败：{err}")
            return None

    @staticmethod
    def __sample(rows: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
        """在记录集合上等距抽样，避免只看到最旧或最新的那几条。"""
        if count <= 0:
            return []
        if len(rows) <= count:
            return list(rows)
        step = len(rows) / float(count)
        return [rows[min(int(index * step), len(rows) - 1)] for index in range(count)]

    @staticmethod
    def __first_dest(row: Dict[str, Any]) -> str:
        """取整理记录目标路径中的第一条（dest 可能是多行）。"""
        raw = row.get("dest") or ""
        for line in str(raw).replace("\r", "\n").split("\n"):
            if line.strip():
                return line.strip()
        return ""

    @staticmethod
    def __top_segment(normalized: str) -> str:
        """取归一化路径的顶层目录名，用于统计路径画像。"""
        segments = [seg for seg in str(normalized).split("/") if seg]
        return segments[0] if segments else "(空)"

    @staticmethod
    def __ext_of(normalized: str) -> str:
        """取路径中文件名的扩展名（小写），无扩展名时返回「(无扩展名)」。"""
        segments = [seg for seg in str(normalized).split("/") if seg]
        if not segments:
            return "(空)"
        name = segments[-1]
        if "." not in name:
            return "(无扩展名)"
        return "." + name.rsplit(".", 1)[-1].lower()

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
            self.__notify_text("幽灵整理记录", "\n".join(lines))
        except Exception as err:
            logger.error(f"幽灵整理记录：发送通知失败：{err}")

    def __load(self):
        try:
            stored = self.get_data("status") or {}
        except Exception as err:
            logger.error(f"幽灵整理记录：读取历史扫描结果失败：{err}")
            stored = {}
        if isinstance(stored, dict):
            self._report = str(stored.get("report") or "")
            self._report_time = str(stored.get("report_time") or "")
            self._report_path = str(stored.get("report_path") or "")
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
            self.save_data(
                "status",
                {
                    "ghosts": self._ghosts,
                    "stats": self._stats,
                    "report": self._report,
                    "report_time": self._report_time,
                    "report_path": self._report_path,
                },
            )
        except Exception as err:
            logger.error(f"幽灵整理记录：保存扫描结果失败：{err}")
