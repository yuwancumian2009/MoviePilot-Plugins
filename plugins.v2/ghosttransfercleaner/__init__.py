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

匹配方式分三层，逐层放宽：

1. **前缀剥离 + 目录模糊解析**：把整理记录的目标路径逐级剥掉前导目录，拼到每个
   已配置的 strm 根目录下查找同名 .strm。目录名先按原样匹配，对不上再按归一化
   名称匹配 —— 忽略大小写与分隔符、忽略 `[tmdbid-xxx]` 标记、忽略
   `Season 2` / `Season 02` 的写法差异。命中的是库里的真实路径。
2. **按节目目录全库定位**：第 1 层落空时，用节目目录名在整个 strm 库里找它的实际
   落点，再往下核对季目录与文件。媒体库初始化后目录被改名、换了分类目录的情况，
   只有这一层认得出来。
3. **按文件名全库查找**（宽松匹配，默认开启）：只认文件名，用于目录层级结构完全
   对不上的情况。

**注意**：整理记录里的 `files` 字段存的是**整理前的下载源文件清单**
（`TransferInfo.file_list`，形如 `/视频/qb/.../xxx.mkv`），它不是整理后的目标路径，
不能拿去比对 strm 库 —— 那样必然会 100% 对不上，把所有记录都误判成幽灵。
插件只用记录里的 `dest`（目标路径）做判定。

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

import csv
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.event import eventmanager, Event
from app.db import SessionFactory
from app.db.models.transferhistory import TransferHistory
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

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
# 记录所属库的目录没挂载到容器，文件是否存在无从判断
STATE_UNVERIFIED = "unverified"

# 诊断报告里的比对结论文案（比扫描分级多出「正常」「无法判断」两种）
PROBE_TEXT = {
    STATE_OK: "正常 —— 该路径对应的 strm 仍在库中",
    LEVEL_PART: "局部缺失 —— 节目目录还在，但该文件对应的 strm 不在",
    LEVEL_WHOLE: "整部缺失 —— strm 库里连该节目目录都找不到",
    STATE_UNKNOWN: "无法判断 —— 记录里没有可用的目标路径",
    STATE_UNVERIFIED: "未核验 —— 记录所属库的目录没挂载到容器，查不了",
}

# strm 扩展名
STRM_SUFFIX = ".strm"
# 判定引擎版本（1=旧的存储层查询，2=本地 strm 比对，3=目录模糊解析 + 全库索引，
# 4=按来源映射分库判定：命中映射只在该库内比对，库未挂载则判「未核验」）
ENGINE_VERSION = 5
# 单次扫描的最长耗时（秒）
SCAN_DEADLINE = 60
# 页面默认每页展示条数（可在插件配置里改；0 = 一页展示全部）
DEFAULT_PER_PAGE = 100
# 最多缓存的幽灵记录条数（结果页要能翻到全部，所以留得比较宽；
# 每条幽灵 ≈ 300 字节，2 万条 ≈ 6MB，插件单独持有可接受）
CACHE_LIMIT = 20000

# 合并视图：一个剧集最多在展开明细里列出多少条记录（超出的部分在提示里说明，
# 完整清单始终可以导出 CSV），避免单组 789 条那种超大剧集把页面撑爆。
GROUP_DETAIL_LIMIT = 300
# 判定为「异常占比过高」的最低样本量
SUSPICIOUS_MIN_SAMPLE = 20
# 判定为「异常占比过高」的比例
SUSPICIOUS_RATIO = 0.8
# 后缀匹配时最多回溯的目录层数（防止异常记录产生大量探测）
# 后缀匹配时最多回溯的目录层数（防止异常记录产生大量探测）
MAX_TAIL_DEPTH = 8
# 集号识别用到的正则：记录里的文件名与库里实际文件名往往只差发布组/音轨标记
# （例如 `...BD.HEVC.FLAC-Snow-Raws.mkv` vs `...BD.HEVC-Snow-Raws.strm`），
# 只比文件名会把这类记录全部误判成「局部缺失」，所以补一层「同季同集」比对。
EP_SXXEXX_RE = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,4})")
# `S01E01-E03` 里前一个集号已被 SXXEXX 吃掉，这里只匹配紧随其后的 `[-~]E?NN`
EP_TAIL_RE = re.compile(r"\s*[-~～]\s*([Ee])?(\d{1,4})(?!\d)")
EP_CN_RE = re.compile(r"第\s*(\d{1,4})\s*[话話集期回]")
EP_BARE_RE = re.compile(
    r"(?<![A-Za-z0-9])[Ee][Pp][\s._-]?(\d{1,3})(?!\d)"
    r"|(?<![A-Za-z0-9])[Ee][\s._-](\d{1,3})(?!\d)"
)
# 季号必须写清楚才算：「Season 01」「S2」可以，「第 1 季」必须带「季」字
# （`第 10 集` 是集号，绝不能当成季号）
SEASON_DIR_RE = re.compile(r"^(?:season|s)\s*0*(\d{1,2})$", re.I)
SEASON_DIR_CN_RE = re.compile(r"^第\s*0*(\d{1,2})\s*季$")
SEASON_WORD_RE = re.compile(r"(?:[Ss]eason)\s*0*(\d{1,2})|第\s*0*(\d{1,2})\s*季")
# 集号区间最多展开多少集（防止 `E01-E9999` 这类异常名称把内存撑爆）；
# 写成 `-E03` 的按 EP_RANGE_MAX，写成裸数字 `-03` 的更保守
EP_RANGE_MAX = 60
EP_RANGE_GAP_BARE = 10
# 进季子目录找集号时最多扫多少个季目录
EP_SUBDIR_LIMIT = 40
# 目录名索引里做「忽略年份」回退时用的年份
YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# 目录内容缓存的最大条目数
DIR_CACHE_LIMIT = 20000
# 全库索引（文件名/目录名）的遍历上限：节点数与最长耗时（秒）
INDEX_LIMIT = 200000
INDEX_SECONDS = 20

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
# 完整幽灵清单落盘的文件名（页面表格分页展示，全量同时落盘一份）
GHOST_CSV_FILENAME = "ghost_records.csv"
# 来源标签：未配置映射的记录 / 映射里没写来源名的记录
SOURCE_NONE = "未映射"
SOURCE_BLANK = "未标注"


# ---------------------------------------------------------------------------
# 页面表格渲染助手
#
# 为什么不直接用 Vuetify 的 VDataTable：MoviePilot 的插件页面渲染器（PageRender）
# 渲染每个节点时**总会给组件传一个 default 插槽**（内容 = 该节点的 text + content）。
# VDataTable 的数据靠 props(headers/items) 传入，但它的表格主体取自 default 插槽，
# 于是数据被这个空插槽覆盖 —— 实测渲染出来只剩空壳 <table></table>（0 表头 0 行）。
# 而且 props 写成短横线风格（items-per-page）在渲染函数里也不会被识别。
# 结论：插件页面里展示列表统一改用「原生 HTML 表格」，由下面的函数生成，
# 既避开该限制，又能精确控制列宽、省略号、悬停提示与配色。
# ---------------------------------------------------------------------------

# 手机端卡片视图按字段类型区分单元格：标题/状态/来源/元信息/路径
_CELL_CLASS = {
    "title": "gtc-c-title",
    "level_text": "gtc-c-sev",
    "source": "gtc-c-src",
    "type": "gtc-c-meta",
    "season_episode": "gtc-c-meta",
    "date": "gtc-c-meta",
    "dest": "gtc-c-path",
}


_TABLE_CSS = """<style>
.gtc-wrap{border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));border-radius:8px;overflow:auto;max-height:62vh;background:rgb(var(--v-theme-surface));}
table.gtc-tbl{border-collapse:separate;border-spacing:0;width:100%;table-layout:fixed;font-size:12.5px;}
table.gtc-tbl th{position:sticky;top:0;z-index:2;background:rgb(var(--v-theme-surface-variant));color:rgb(var(--v-theme-on-surface-variant));font-weight:600;text-align:left;padding:9px 11px;white-space:nowrap;border-bottom:1px solid rgba(var(--v-border-color),var(--v-border-opacity));}
table.gtc-tbl td{padding:8px 11px;transition:background .12s;border-bottom:1px solid rgba(var(--v-border-color),calc(var(--v-border-opacity) * .55));overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:rgb(var(--v-theme-on-surface));}
table.gtc-tbl tbody tr:nth-child(even) td{background:rgba(var(--v-theme-on-surface),.03);}
table.gtc-tbl tbody tr:hover td{background:rgba(var(--v-theme-primary),.09);}
/* 悬停展开：鼠标放到某一行时，被省略号截断的标题/路径在行内完整展开（纯 CSS，无需 JS） */
table.gtc-tbl tbody tr:hover td.gtc-c-title,table.gtc-tbl tbody tr:hover td.gtc-c-path{white-space:normal;word-break:break-all;overflow-wrap:anywhere;overflow:visible;text-overflow:clip;}
table.gtc-tbl tbody tr:hover td.gtc-c-path{color:rgb(var(--v-theme-on-surface));}
table.gtc-tbl td.gtc-c-path{cursor:zoom-in;}
/* 表格最小宽度：仅桌面端生效（弹窗太窄时出现横向滚动条，避免各列被压成一条）。
   放进 min-width 媒体查询，避免覆盖手机端卡片布局。 */
@media (min-width:601px){table.gtc-tbl{min-width:900px;}}
table.gtc-tbl tbody tr:last-child td{border-bottom:none;}
.gtc-mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'Courier New',monospace;font-size:11.5px;color:rgb(var(--v-theme-on-surface-variant));}
.gtc-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;line-height:1.6;white-space:nowrap;}
.gtc-cap{font-size:11.5px;color:rgba(var(--v-theme-on-surface),.72);padding:0 2px 6px;}
/* ---- 按剧集合并：组行（可展开）与明细行（纯 CSS 展开，无 JS） ---- */
.gtc-exp{display:inline-flex;align-items:center;cursor:pointer;user-select:none;vertical-align:middle;margin-right:5px;line-height:1;}
.gtc-exp input{position:absolute;opacity:0;width:0;height:0;}
.gtc-exp .gtc-chev{display:inline-block;font-size:10px;color:rgb(var(--v-theme-primary));transition:transform .15s;}
.gtc-exp input:checked~.gtc-chev{transform:rotate(90deg);}
.gtc-cnt{display:inline-block;margin-left:6px;padding:0 7px;border-radius:999px;font-size:11px;font-weight:600;background:rgba(var(--v-theme-primary),.15);color:rgb(var(--v-theme-primary));}
.gtc-chip+.gtc-chip{margin-left:4px;}
table.gtc-tbl tr.gtc-det{display:none;}
table.gtc-tbl tr.gtc-grp:has(.gtc-tg:checked)+tr.gtc-det{display:table-row;}
table.gtc-tbl tr.gtc-grp:has(.gtc-tg:checked) td{background:rgba(var(--v-theme-primary),.10);}
table.gtc-tbl tr.gtc-det td{padding:4px 11px 9px!important;white-space:normal!important;overflow:visible!important;text-overflow:clip!important;background:rgba(var(--v-theme-primary),.045)!important;}
.gtc-det .gtc-drow{display:flex;flex-wrap:wrap;align-items:center;gap:3px 8px;padding:5px 8px;border-radius:6px;font-size:12px;}
.gtc-det .gtc-drow:nth-child(odd){background:rgba(var(--v-theme-on-surface),.035);}
.gtc-det .gtc-dep{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'Courier New',monospace;font-weight:600;font-size:11.5px;min-width:62px;color:rgb(var(--v-theme-on-surface));}
.gtc-det .gtc-ddate{font-size:11.5px;color:rgba(var(--v-theme-on-surface),.72);}
.gtc-det .gtc-dpath{flex:1 1 100%;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'Courier New',monospace;font-size:11.5px;line-height:1.45;color:rgba(var(--v-theme-on-surface),.80);word-break:break-all;}
.gtc-det .gtc-dnote{font-size:11.5px;color:rgba(var(--v-theme-on-surface),.72);padding:7px 8px 1px;}
</style>"""
# 工具条（筛选 / 显示方式 / 每页 / 翻页）排版：左侧固定宽度标签列 + 右侧等高等宽按钮组。
# 桌面端与手机端都用同一套标记，靠 CSS 切换（手机端见 _MOBILE_CSS 内覆盖规则）。
_TOOLBAR_CSS = """<style>
/* 筛选/分页工具条：左侧固定宽度标签列 + 每组按钮等宽等高。
   等宽由后端按组内最长文字算好写在内联 style 上（这里绝不能写 min-width，否则会盖掉它） */
.gtc-fbox{padding:12px 14px!important;}
.gtc-tbrow{display:flex;flex-wrap:wrap;align-items:center;gap:10px 24px;}
.gtc-tbrow+.gtc-tbrow{margin-top:10px;padding-top:10px;border-top:1px solid rgba(var(--v-border-color),calc(var(--v-border-opacity)*.7));}
.gtc-sec{display:flex;align-items:center;gap:10px;min-width:0;}
.gtc-sec-lab{flex:0 0 auto;width:4.6em;text-align:right;font-size:12px;font-weight:600;letter-spacing:0;color:rgba(var(--v-theme-on-surface),.6);}
.gtc-seg{display:flex;flex-wrap:wrap;align-items:center;gap:6px;}
.gtc-fbox .v-btn{height:32px!important;padding:0 12px!important;font-size:12.5px!important;font-weight:600!important;letter-spacing:0!important;text-transform:none!important;border-radius:8px!important;box-shadow:none!important;}
.gtc-fbox .v-btn .v-btn__content{font-size:12.5px!important;letter-spacing:0!important;line-height:1;white-space:nowrap;}
/* 文字颜色锁在按钮本身（含 .v-btn__content），否则会被 Vuetify 的 on-* 反色盖掉，
   出现「白字配浅紫底」这种看不清的组合 */
.gtc-fbox .v-btn.gtc-off, .gtc-fbox .v-btn.gtc-off .v-btn__content{color:rgba(var(--v-theme-on-surface),.8)!important;}
.gtc-fbox .v-btn.gtc-on, .gtc-fbox .v-btn.gtc-on .v-btn__content{color:rgba(var(--v-theme-on-surface),.95)!important;}
.gtc-fbox .v-btn.gtc-on{background:rgba(var(--v-theme-primary),.24)!important;}
.gtc-fbox .v-btn.gtc-page, .gtc-fbox .v-btn.gtc-page .v-btn__content{color:rgb(var(--v-theme-primary))!important;}
.gtc-pinfo{font-size:12px;font-weight:600;letter-spacing:0;color:rgba(var(--v-theme-on-surface),.7);padding:0 6px;white-space:nowrap;}
.gtc-only-mob{display:none;}
</style>"""

_MOBILE_CSS = """<style>
/* 手机端（<=600px）适配：统计卡两列、记录转卡片、路径完整换行、筛选区紧凑。
   桌面端样式完全不受影响（规则全部在媒体查询内）。 */
@media (max-width:600px){
.gtc-row{margin:0!important}
.gtc-col{padding:3px 4px!important}
.gtc-stat .v-card-subtitle{padding:0 0 2px!important;font-size:11px!important;font-weight:500;letter-spacing:0;opacity:.9}
.gtc-stat .v-card-title{padding:0!important;font-size:19px!important;line-height:1.25!important}
.gtc-stat .v-card-text{padding:3px 0 0!important;font-size:10.5px!important;line-height:1.35;opacity:.85}
.gtc-fbox{padding:8px!important}
.gtc-frow{gap:4px 6px!important;margin-bottom:6px!important}
.gtc-frow:last-child{margin-bottom:0!important}
.gtc-frow .v-divider{display:none!important}
.gtc-frow .v-btn{font-size:12px;padding:0 8px!important}
.gtc-wrap{border:0!important;background:transparent!important;border-radius:0!important;max-height:none!important;overflow:visible!important}
table.gtc-tbl{display:block;table-layout:auto;min-width:0}
table.gtc-tbl thead{display:none}
table.gtc-tbl tbody{display:block}
table.gtc-tbl tr{display:flex;flex-wrap:wrap;align-items:center;gap:2px 8px;padding:9px 10px;margin-bottom:8px;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));border-radius:10px;background:rgb(var(--v-theme-surface))}
table.gtc-tbl tbody tr:nth-child(even) td{background:transparent}
table.gtc-tbl tbody tr:hover td{background:transparent}
table.gtc-tbl td{display:block!important;width:auto!important;max-width:none!important;border:0!important;padding:0!important;white-space:normal!important;overflow:visible!important;text-overflow:clip!important;line-height:1.45;word-break:break-word}
table.gtc-tbl td.gtc-c-title{order:1;flex:1 1 auto;min-width:0;font-size:14.5px;font-weight:600;overflow-wrap:anywhere}
table.gtc-tbl td.gtc-c-sev{order:2;flex:0 0 auto;margin-left:auto}
table.gtc-tbl td.gtc-c-src{order:3;flex:0 0 auto}
table.gtc-tbl td.gtc-c-meta{order:3;flex:0 0 auto;font-size:11.5px;color:rgba(var(--v-theme-on-surface),.72)}
table.gtc-tbl td.gtc-c-meta+td.gtc-c-meta::before{content:'\00b7';padding-right:6px;color:rgba(var(--v-theme-on-surface),.3)}
table.gtc-tbl td.gtc-c-path{order:4;flex:1 1 100%;margin-top:5px;padding:6px 8px!important;border-radius:6px;background:rgba(var(--v-theme-on-surface),.05);word-break:break-all}
.gtc-rep{font-size:11px!important;line-height:1.45!important}
/* 合并视图：展开时把明细接在组卡片下方，视觉上连成一张卡 */
table.gtc-tbl tr.gtc-grp:has(.gtc-tg:checked){margin-bottom:0;border-radius:10px 10px 0 0;}
table.gtc-tbl tr.gtc-grp:has(.gtc-tg:checked)+tr.gtc-det{display:block;margin:0 0 8px;padding:2px 10px 9px;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));border-top:0;border-radius:0 0 10px 10px;background:rgb(var(--v-theme-surface));}
table.gtc-tbl tr.gtc-det td{padding:0!important;border:0!important;background:transparent!important;}
table.gtc-tbl tr.gtc-det .gtc-drow{padding:6px 3px;}
table.gtc-tbl tr.gtc-det .gtc-dep{min-width:0;font-size:11.5px;}
table.gtc-tbl tr.gtc-det .gtc-dpath{font-size:10.5px;}
table.gtc-tbl .gtc-cnt{margin-left:5px;padding:0 6px;}
/* ---- 筛选/分页工具条：手机端 = 小标题在上 + 按钮按组等分栅格（列数由后端算好 --gtc-cols） ---- */
.gtc-fbox{padding:10px 12px!important}
.gtc-tbrow{display:block;gap:0}
.gtc-tbrow+.gtc-tbrow{margin-top:10px;padding-top:10px}
.gtc-sec{display:block;gap:0}
.gtc-sec+.gtc-sec{margin-top:9px}
.gtc-sec-lab{display:block;width:auto;text-align:left;margin:0 0 5px;font-size:11.5px;color:rgba(var(--v-theme-on-surface),.6)}
.gtc-seg{display:grid;grid-template-columns:repeat(var(--gtc-cols,4),1fr);gap:6px}
.gtc-fbox .v-btn{width:100%!important;min-width:0!important;height:32px!important;padding:0 4px!important;font-size:12px!important}
.gtc-fbox .v-btn .v-btn__content{font-size:12px!important;overflow:hidden;text-overflow:ellipsis}
.gtc-only-desk{display:none!important}
.gtc-only-mob{display:block!important}
.gtc-pinfo{padding:7px 0 0;text-align:center;white-space:normal}
}
</style>"""



# 标签配色：底色保持浅色调，文字用深一档的同色相（实测对比度 >= 4.3:1，浅色主题下清晰可读）
_CHIP_TONES = {
    "red": "background:rgba(244,67,54,.16);color:#b71c1c;",
    "amber": "background:rgba(255,152,0,.18);color:#b04a00;",
    "blue": "background:rgba(33,150,243,.16);color:#0d47a1;",
    "green": "background:rgba(76,175,80,.18);color:#1b5e20;",
    "purple": "background:rgba(156,39,176,.16);color:#4a148c;",
    "grey": "background:rgba(var(--v-theme-on-surface),.10);color:rgba(var(--v-theme-on-surface),.78);",
}


def _esc(value: Any) -> str:
    """HTML 转义（页面按 html 片段渲染，文件名/路径里的 & < > 必须转义）。"""
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _cell_html(key: str, text: str) -> str:
    """按列决定单元格内容：状态类字段渲染成彩色小标签，其余原样输出。"""
    if key == "level_text":
        tone = "red" if "整部" in text else ("amber" if "局部" in text else "grey")
        body = text.replace("🔴", "").replace("🟡", "").strip() or text
        return f'<span class="gtc-chip" style="{_CHIP_TONES[tone]}">{_esc(body)}</span>'
    if key == "source":
        tones = {"115": "blue", "本地": "green", "夸克": "purple"}
        names = [part.strip() for part in text.replace("/", "\u00b7").split("\u00b7") if part.strip()]
        if not names:
            names = [text]
        return "".join(
            '<span class="gtc-chip" style="'
            + _CHIP_TONES.get(tones.get(name, "grey"), _CHIP_TONES["grey"]) + '">'
            + _esc(name) + "</span>"
            for name in names
        )
    return _esc(text)


def _season_num(text: str) -> int:
    """从 S01 / S1 / 第1季 里取数字，用于排序与连续性判断。"""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return int(digits) if digits else 0


def _season_label(seasons: List[str]) -> str:
    """季号压缩：S01/S02/S03 -> S01-S03，不连续则逗号列出；空则返回空串。"""
    items = sorted({str(s).strip() for s in seasons if str(s).strip()}, key=_season_num)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    nums = [_season_num(s) for s in items]
    if nums == list(range(nums[0], nums[0] + len(nums))):
        return items[0] + "\u2013" + items[-1]
    return ",".join(items)


def _common_dir(paths: List[str]) -> str:
    """一组目标路径的公共目录前缀（按路径段比较，末尾带 /）。

    单条记录时返回完整路径本身；毫无公共目录时返回空串。
    """
    cleaned = [str(item or "").replace("\\", "/").strip("/") for item in paths if item]
    cleaned = [item for item in cleaned if item]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return str(paths[0])
    split = [item.split("/") for item in cleaned]
    shared: List[str] = []
    for idx in range(min(len(item) for item in split)):
        if len({item[idx] for item in split}) == 1:
            shared.append(split[0][idx])
        else:
            break
    if not shared:
        return ""
    return "/" + "/".join(shared) + "/"


def _group_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把同一个剧集的记录合并成一组。

    分组键 = (类型, 标题, 年份)：同名同年的剧集（含跨季）合并成一条，
    电影/单集记录各自成组。组内按季号/集号排序，组的顺序保持「组内第一条记录
    在原列表中的位置」，因此合并不会打乱原有的大顺序。
    """
    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str, str]] = []
    for record in records:
        key = (
            str(record.get("type") or "").strip(),
            str(record.get("title") or "").strip(),
            str(record.get("year") or "").strip(),
        )
        if key not in groups:
            groups[key] = {"key": key, "records": []}
            order.append(key)
        groups[key]["records"].append(record)

    out: List[Dict[str, Any]] = []
    for key in order:
        items: List[Dict[str, Any]] = groups[key]["records"]
        items.sort(key=lambda item: (
            _season_num(item.get("seasons")),
            _season_num(item.get("episodes")),
            str(item.get("date") or ""),
        ))
        gtype, title, year = key
        levels = {LEVEL_WHOLE: 0, LEVEL_PART: 0}
        for item in items:
            level = item.get("level")
            if level in levels:
                levels[level] += 1
        sources: List[str] = []
        for item in items:
            name = str(item.get("source") or SOURCE_NONE).strip() or SOURCE_NONE
            if name not in sources:
                sources.append(name)
        dates = sorted(str(item.get("date") or "") for item in items if item.get("date"))
        out.append({
            "key": key,
            "records": items,
            "count": len(items),
            "title": " ".join(part for part in (title, year) if part) or "-",
            "type": gtype or "-",
            "season_label": _season_label([str(item.get("seasons") or "") for item in items]),
            "whole": levels[LEVEL_WHOLE],
            "part": levels[LEVEL_PART],
            "sources": sources,
            "date": dates[-1] if dates else "-",
            "dest": _common_dir([str(item.get("dest") or "") for item in items]),
            "single": len(items) == 1,
        })
    return out


def _group_detail_html(group: Dict[str, Any], limit: int = GROUP_DETAIL_LIMIT) -> str:
    """展开明细：逐条列出该剧集的缺失记录（季集 / 缺失程度 / 来源 / 时间 / 完整路径）。"""
    parts: List[str] = ['<div class="gtc-dbox">']
    shown = 0
    for record in group["records"]:
        if shown >= limit:
            break
        season = str(record.get("seasons") or "").strip()
        episode = str(record.get("episodes") or "").strip()
        label = (season + episode).strip() or "整部"
        is_whole = record.get("level") == LEVEL_WHOLE
        tone = "red" if is_whole else "amber"
        src = str(record.get("source") or SOURCE_NONE).strip() or SOURCE_NONE
        src_tone = {"115": "blue", "本地": "green", "夸克": "purple"}.get(src, "grey")
        parts.append(
            '<div class="gtc-drow">'
            + '<span class="gtc-dep">' + _esc(label) + '</span>'
            + '<span class="gtc-chip" style="' + _CHIP_TONES[tone] + '">'
            + ("整部缺失" if is_whole else "局部缺失") + '</span>'
            + '<span class="gtc-chip" style="' + _CHIP_TONES[src_tone] + '">' + _esc(src) + '</span>'
            + '<span class="gtc-ddate">' + _esc(record.get("date") or "-") + '</span>'
            + '<span class="gtc-dpath">' + _esc(record.get("dest") or "-") + '</span>'
            + '</div>'
        )
        shown += 1
    rest = group["count"] - shown
    if rest > 0:
        parts.append(
            '<div class="gtc-dnote">另外 ' + str(rest) + ' 条同类记录未在此展开'
            '（可在「生成诊断报告」后导出的 CSV 里查看全部）</div>'
        )
    parts.append("</div>")
    return "".join(parts)


def _table_node(
    columns: List[Tuple[str, str, str, bool]],
    rows: List[Dict[str, Any]],
    caption: str = "",
    with_css: bool = False,
) -> dict:
    """把行数据渲染成页面节点（原生 HTML 表格 + 样式）。

    columns: [(表头文字, 数据键, 列宽, 是否等宽字体), ...]
    """
    head = "".join(
        f'<th{f" style=\"width:{width}\"" if width else ""}>{_esc(title)}</th>'
        for title, _key, width, _mono in columns
    )
    body_parts: List[str] = []
    span = len(columns)
    for row in rows:
        overrides = row.get("__cells__") or {}
        cells: List[str] = []
        for _title, key, _width, mono in columns:
            raw = row.get(key, "")
            text = "" if raw is None else str(raw)
            classes = ["gtc-mono"] if mono else []
            extra = _CELL_CLASS.get(key)
            if extra:
                classes.append(extra)
            cls = f' class="{" ".join(classes)}"' if classes else ""
            inner = overrides.get(key) or _cell_html(key, text)
            cells.append(f'<td{cls} title="{_esc(text)}">{inner}</td>')
        row_cls = row.get("__cls__") or ""
        head_cls = f' class="{row_cls}"' if row_cls else ""
        body_parts.append(f"<tr{head_cls}>" + "".join(cells) + "</tr>")
        detail = row.get("__detail__")
        if detail:
            body_parts.append(
                f'<tr class="gtc-det"><td colspan="{span}">' + detail + "</td></tr>"
            )
    html = (
        (_TABLE_CSS if with_css else "")
        + (f'<div class="gtc-cap">{_esc(caption)}</div>' if caption else "")
        + '<div class="gtc-wrap"><table class="gtc-tbl"><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body_parts)
        + "</tbody></table></div>"
    )
    return {"component": "div", "props": {"class": "mb-3"}, "html": html}


class GhostTransferCleaner(_PluginBase):
    """幽灵整理记录：体检并清理「整理记录还在、媒体文件已丢失」的记录。"""

    # 插件名称
    plugin_name = "幽灵整理记录"
    # 插件描述
    plugin_desc = "体检「整理记录还在、媒体文件已丢失」的幽灵记录，支持一键清理，恢复正常自动整理入库。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/clean.png"
    # 插件版本
    plugin_version = "1.9.4"
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
    # 路径映射规则：(记录中的前缀, 本地实际前缀, 来源标签)
    _path_map: List[Tuple[str, str, str]] = []
    _loose_match = True
    _only_whole = True
    # 结果页分页与筛选状态（不持久化，重启后回到第一页）
    _per_page: int = DEFAULT_PER_PAGE
    # 结果页是否按剧集合并显示（True=合并，False=逐条；不持久化，重启回默认合并）
    _merge: bool = True
    _page: int = 1
    _filter_source: str = ""
    _filter_level: str = ""
    # 无法核验的记录（所属库目录没挂载）
    _unverified: List[Dict[str, Any]] = []
    _allow_clean = False
    _auto_clean = False
    _min_age_days = 0
    _max_records = 0
    _cron = ""

    # ---- 运行状态 ----
    _ghosts: List[Dict[str, Any]] = []
    _stats: Dict[str, Any] = {}
    _report: str = ""
    _report_time: str = ""
    _report_path: str = ""
    # 完整幽灵清单 CSV 的落盘路径（每次扫描刷新）
    _ghost_csv_path: str = ""
    _scanning = False
    _diagnosing = False
    # 完整报告的存放目录覆盖项（留空则自动推断，主要供离线测试使用）
    _report_dir_override = ""
    _lock = threading.Lock()
    # 目录条目缓存：{目录绝对路径: {小写名称: (实际名称, 是否目录)}}，None 表示不可访问
    _dir_cache: Dict[str, Optional[Dict[str, Tuple[str, bool]]]] = {}
    # 「归一化名称 -> 实际目录名」缓存，用于目录名宽松匹配
    _key_cache: Dict[str, Dict[str, str]] = {}
    # 全库索引：{归一化文件名: 路径} / {归一化目录名: [路径]}，每次扫描重建
    _index_files: Dict[str, str] = {}
    _index_dir_map: Dict[str, List[str]] = {}
    # 「忽略年份的归一化目录名 -> 路径」：剧集被重新刮削后年份可能变化
    # （例如记录里是「安全警长啦咘啦哆 (2023)」，库里是「... (2022) [tmdbid-...]」），
    # 精确 key 匹配不上时用这张表兜底
    _index_dir_map_yl: Dict[str, List[str]] = {}
    # 「库根目录 -> 该根下的目录名索引」：全库名索引只覆盖 strm 库，记录映射到的
    # 其它库（本地目录、网盘挂载）按需单独扫一次并缓存，避免整部误报
    _root_dir_map: Dict[str, Dict[str, List[str]]] = {}
    _index_truncated = False

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
        self._loose_match = True
        self._only_whole = True
        self._per_page = DEFAULT_PER_PAGE
        self._merge = True
        self._page = 1
        self._filter_source = ""
        self._filter_level = ""
        self._unverified = []
        self._allow_clean = False
        self._auto_clean = False
        self._min_age_days = 0
        self._max_records = 0
        self._cron = ""
        self._dir_cache = {}
        self._key_cache = {}
        self._index_files = {}
        self._index_dir_map = {}
        self._index_dir_map_yl = {}
        self._root_dir_map = {}
        self._index_truncated = False
        self._report = ""
        self._report_time = ""
        self._report_path = ""
        self._ghost_csv_path = ""
        self._diagnosing = False

        scan_now = False
        clean_now = False
        if config:
            self._enabled = bool(config.get("enabled"))
            self._notify = bool(config.get("notify", True))
            self._notify_report = bool(config.get("notify_report", True))
            self._strm_paths = self.__parse_lines(config.get("strm_paths"))
            self._path_map = self.__parse_path_map(config.get("path_map"))
            # 兼容旧配置键 check_files（它原本是「按记录内文件清单逐个核对」，
            # 语义不成立已废弃；这里沿用其开关值作为「宽松匹配」）
            if "loose_match" in config:
                self._loose_match = bool(config.get("loose_match"))
            elif "check_files" in config:
                self._loose_match = bool(config.get("check_files"))
            else:
                self._loose_match = True
            self._only_whole = bool(config.get("only_whole", True))
            self._allow_clean = bool(config.get("allow_clean", False))
            self._auto_clean = bool(config.get("auto_clean", False))
            self._min_age_days = self.__to_int(config.get("min_age_days"), 0)
            # 0 或留空 = 不限；旧版本默认 5000 会漏掉后面的记录
            self._max_records = max(0, self.__to_int(config.get("max_records"), 0))
            self._cron = str(config.get("cron") or "").strip()
            scan_now = bool(config.get("scan_now"))
            clean_now = bool(config.get("clean_now"))
            if scan_now or clean_now:
                # 复位一次性开关，避免插件重载后重复执行
                reset = dict(config)
                reset["scan_now"] = False
                reset["clean_now"] = False
                self.update_config(reset)

        # 结果页的分页/筛选状态：每页条数 0 表示一页显示全部，负数按 0 处理
        self._per_page = self.__to_int(
            (config or {}).get("per_page"), DEFAULT_PER_PAGE
        )
        if self._per_page < 0:
            self._per_page = 0
        self._page = 1
        self._filter_source = ""
        self._filter_level = ""

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
        """
        返回插件 API 路由列表。

        两个必须遵守的约定（踩过坑，别改回去）：

        1. `path` 里**不要**再写插件 ID。MP 注册时是
           `PLUGIN_PREFIX + path`，而 `PLUGIN_PREFIX` 已经是
           `/plugin/<插件ID>`；自己再拼一次会得到
           `/plugin/<ID>/<ID>/xxx`，前端按 `/plugin/<ID>/xxx` 请求只能 404。
        2. `auth` 必须是 `"bear"`。前端页面上的按钮走的是 axios 实例，
           只带 `Authorization: Bearer <token>`，**不带 apikey**；
           而默认的 `"apikey"` 认证只认 URL 里的 `apikey=` 或
           `X-API-KEY` 头，于是后台返回 401，前端又静默吞掉异常，
           表现就是「点了没反应」。
        """
        return [
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询扫描状态与幽灵记录",
                "description": "返回最近一次扫描的统计信息与幽灵整理记录清单",
            },
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "立即扫描幽灵整理记录",
                "description": "扫描整理记录，找出目标媒体文件已不存在的幽灵记录",
            },
            {
                "path": "/clean",
                "endpoint": self.api_clean,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "清理幽灵整理记录",
                "description": "删除扫描到的幽灵整理记录（需先在插件配置中开启「允许一键清理」）",
            },
            {
                "path": "/diagnose",
                "endpoint": self.api_diagnose,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "生成诊断报告",
                "description": "抽查整理记录并与本地 strm 目录逐条对照，"
                               "输出可用于定位「为什么全都对不上」的具体报告；"
                               "后台生成，完成后推送到通知渠道",
            },
            {
                "path": "/notify_report",
                "endpoint": self.api_notify_report,
                "methods": ["GET", "POST"],
                "auth": "bear",
                "summary": "把诊断报告发送到通知渠道",
                "description": "将最近一次生成的诊断报告摘要重新推送到 MP 通知渠道",
            },
            {
                "path": "/page",
                "endpoint": self.api_page,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "切换结果页页码/筛选",
                "description": "数据页面的翻页与来源/缺失程度筛选用；"
                               "改完状态后前端会自动重新拉取页面",
            },
        ]

    def api_status(self) -> Dict[str, Any]:
        """查询最近一次扫描状态、来源分布与当前页的幽灵记录。"""
        _filtered = self.__filtered_ghosts()
        _units = _group_records(_filtered) if self._merge else _filtered
        _page_items, _cur, _pages = self.__page_slice(_units)
        return {
            "success": True,
            "message": "ok",
            "data": {
                "version": self.plugin_version,
                "engine": ENGINE_VERSION,
                "scanning": self._scanning,
                "diagnosing": self._diagnosing,
                "stats": self._stats or {},
                # 只回当前页的数据（全量清单走 CSV 文件），避免接口响应过大
                "ghosts": _page_items,
                "ghosts_total": len(self._ghosts or []),
                "pages": _pages,
                "filtered_total": len(_filtered),
                "merge": self._merge,
                "groups_total": len(_units) if self._merge else 0,
                # 无法核验（所属库目录没挂载）的记录
                "unverified": self._unverified or [],
                # 当前分页与筛选状态
                "page": self._page,
                "per_page": self._per_page,
                "filter_source": self._filter_source,
                "filter_level": self._filter_level,
                # 来源分布（来自最近一次扫描）
                "sources": (self._stats or {}).get("sources", {}),
                "unverified_sources": (self._stats or {}).get("unverified_sources", {}),
                "report": self._report,
                "report_time": self._report_time,
                "report_path": self._report_path,
                # 全量清单的 CSV 落盘路径（外部脚本读这个文件即可）
                "ghost_csv": self._ghost_csv_path,
            },
        }

    def api_page(self, p: int = 1, source: str = "", level: str = "", per: int = -1,
                 merge: int = -1) -> Dict[str, Any]:
        """
        切换结果页的页码与筛选条件（数据页面上的翻页 / 筛选按钮调用）。

        参数都带默认值，FastAPI 会当作查询参数注入；前端 GET 时通过
        events 里的 `params` 传（这一点已在前端源码里确认过）。

        前端按钮请求成功后会自己重新拉取页面数据，所以这里只改状态、返回提示文字。
        """
        try:
            page_no = int(p)
        except (TypeError, ValueError):
            page_no = 1
        source = str(source or "").strip()
        level = str(level or "").strip()
        # 换了筛选条件就回到第一页，否则可能停在越界页码
        if source != self._filter_source or level != self._filter_level:
            page_no = 1
        self._filter_source = source
        self._filter_level = level
        # per 不传（-1）= 保持当前每页条数；per=0 = 一页显示全部
        if per is not None:
            try:
                value = int(per)
            except (TypeError, ValueError):
                value = -1
            if value >= 0 and value != self._per_page:
                self._per_page = value
                page_no = 1
        # merge 不传（-1）= 保持当前显示方式；1 = 按剧集合并，0 = 逐条显示
        if merge is not None:
            try:
                flag = int(merge)
            except (TypeError, ValueError):
                flag = -1
            if flag in (0, 1) and bool(flag) != bool(self._merge):
                self._merge = bool(flag)
                page_no = 1
        self._page = max(1, page_no)
        filtered = self.__filtered_ghosts()
        units = _group_records(filtered) if self._merge else filtered
        _, page_now, pages = self.__page_slice(units)
        return {
            "success": True,
            "message": f"第 {page_now}/{pages} 页",
            "data": {
                "page": page_now,
                "pages": pages,
                "per_page": self._per_page,
                "filter_source": self._filter_source,
                "filter_level": self._filter_level,
                "merge": self._merge,
                "groups": len(units) if self._merge else 0,
                "records": len(filtered),
            },
        }

    def __filtered_ghosts(self) -> List[Dict[str, Any]]:
        """按来源 / 缺失程度筛选已缓存的幽灵记录（空条件 = 全部）。"""
        ghosts = list(self._ghosts or [])
        if self._filter_source:
            ghosts = [
                item for item in ghosts
                if (item.get("source") or SOURCE_NONE) == self._filter_source
            ]
        if self._filter_level:
            ghosts = [item for item in ghosts if item.get("level") == self._filter_level]
        return ghosts

    def __page_slice(self, ghosts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
        """按当前页码切片，返回（本页数据, 当前页, 总页数）。per_page=0 表示不分页。"""
        total = len(ghosts)
        per = self._per_page
        if per <= 0:
            self._page = 1
            return ghosts, 1, 1
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, self._page), pages)
        self._page = page
        start = (page - 1) * per
        return ghosts[start:start + per], page, pages

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

    def __write_ghost_csv(self, ghosts: List[Dict[str, Any]]) -> str:
        """
        把本次扫描到的**全部**幽灵记录导出成 CSV（页面分页展示，全量落盘一份）。

        utf-8-sig 是为了 Excel/WPS 直接双击打开不乱码；
        带 BOM 的 UTF-8 在多数表格软件里能正确识别中文与 emoji。
        """
        directory = self.__report_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, GHOST_CSV_FILENAME)
        # 字段顺序按「人看清单时的关注度」排：来源、缺失程度靠前，路径放最后
        fields = [
            "id", "title", "year", "type", "category", "seasons", "episodes",
            "source", "level", "level_text", "date", "reason", "dest", "src",
            "download_hash",
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for ghost in ghosts:
                writer.writerow({key: ghost.get(key, "") for key in fields})
        return path

    def __notify_text(self, title: str, text: str):
        """
        通过 MP 通知链发送一条纯文本消息。

        必须用基类的 post_message（内部走 Chain -> Notification）：
        它才会推送到用户在「通知设置」里启用的渠道，并在消息列表里留档。
        基类的 self.systemmessage（MessageHelper）只是把消息塞进前端的 SSE
        内存队列，页面不开着就看不到、也不留任何记录 —— 用它等于「点了没反应」。
        """
        try:
            self.post_message(mtype=NotificationType.Plugin, title=title, text=str(text))
            return
        except Exception as err:
            logger.error(f"幽灵整理记录：通知链发送失败，退回页面内提示：{err}")
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
                                            "placeholder": "/strm",
                                            "rows": 2,
                                            "hint": "填存放 strm 文件的本地目录。配了下面的「路径映射」时，"
                                                    "记录会直接去它所属的来源库里找；这里的根目录作为"
                                                    "「没配映射的记录」的兜底比对范围。"
                                                    "插件只读本地 .strm 文件，不访问 115 或任何云端接口，"
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
                                            "label": "路径映射 + 来源区分（每行一条：记录里的前缀=本地实际目录|来源标签）",
                                            "placeholder": "/影视=/strm|115\n/夸克=/夸克|夸克\n/本地文件=/本地文件|本地",
                                            "rows": 4,
                                            "hint": "左边是整理记录里的路径前缀，右边是本机的实际目录，"
                                                    "竖线后面是来源标签（可省略）。"
                                                    "命中规则后，插件只在该来源的库目录里比对，"
                                                    "并按来源分类展示、统计，不会跨库误匹配；"
                                                    "右边目录若没有挂载到容器，该记录会标成「未核验」，"
                                                    "而不是误判成幽灵。",
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
                                            "model": "loose_match",
                                            "label": "宽松匹配（目录名对不上时按文件名在整库里找）",
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
                                            "label": "单次最多检查记录数（0 = 不限）",
                                            "type": "number",
                                            "hint": "0 或留空表示不限；设了上限时按整理时间从新到旧检查，"
                                                    "超出的老记录本次不会被检查到。",
                                            "persistent-hint": True,
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "per_page",
                                            "label": "结果页每页条数（0 = 一页显示全部）",
                                            "type": "number",
                                            "hint": "只影响数据页面的展示，扫描结果与 CSV 导出都是全量。",
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "compact",
                                            "text": "数据页面支持按来源（115 / 夸克 / 本地…）和缺失程度筛选，"
                                                    "也可逐页翻看全部幽灵记录；"
                                                    "每次扫描还会把全量清单导出成 CSV 存到插件数据目录。",
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
            "path_map": "/影视=/strm|115\n/夸克=/夸克|夸克\n/本地文件=/本地文件|本地",
            "per_page": DEFAULT_PER_PAGE,
            "loose_match": True,
            "only_whole": True,
            "allow_clean": False,
            "auto_clean": False,
            "min_age_days": 0,
            "max_records": 0,
            "cron": "",
            "scan_now": False,
            "clean_now": False,
        }

    # ------------------------------------------------------------------
    # 数据页面
    # ------------------------------------------------------------------
    def get_page(self) -> List[dict]:
        """返回插件数据页面：统计卡片 + 来源筛选 + 全量分页清单。"""
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
        ghosts_all = self._ghosts if isinstance(self._ghosts, list) else []
        unverified = self._unverified if isinstance(self._unverified, list) else []
        # 筛选结果与合并分组：统计卡提示、筛选区、分页都要用，统一在这里算一次
        filtered = self.__filtered_ghosts()
        groups_all = _group_records(filtered) if self._merge else []
        sources = stats.get("sources") if isinstance(stats.get("sources"), dict) else {}
        unverified_sources = (
            stats.get("unverified_sources")
            if isinstance(stats.get("unverified_sources"), dict) else {}
        )

        content: List[dict] = []
        # 移动端适配样式：无表格时也要生效（统计卡/筛选区）
        content.append({"component": "div", "html": _TOOLBAR_CSS})
        content.append({"component": "div", "html": _MOBILE_CSS})

        def alert(kind: str, text: str, density: str = "comfortable") -> dict:
            return {
                "component": "VAlert",
                "props": {
                    "type": kind,
                    "variant": "tonal",
                    "text": text,
                    "density": density,
                    "class": "mb-3",
                },
            }

        def action_btn(text: str, icon: str, color: str, path: str,
                       params: Optional[Dict[str, Any]] = None,
                       variant: str = "tonal", cls: str = "") -> dict:
            """构造「点了就调插件 API」的按钮（前端点完会自动重拉本页面）。

            icon 传空字符串 = 不显示图标；cls 用于给按钮加排版 class
            （工具条里靠 class 统一高度/最小宽度，纯 CSS 对齐）。
            """
            event: Dict[str, Any] = {"api": f"/plugin/{pid}{path}", "method": "GET"}
            if params:
                event["params"] = params
            props: Dict[str, Any] = {
                "color": color,
                "variant": variant,
                "size": "small",
            }
            if icon:
                props["prependIcon"] = icon
            if cls:
                props["class"] = cls
            return {
                "component": "VBtn",
                "props": props,
                "text": text,
                "events": {"click": event},
            }

        def stat_card(color: str, label: str, value: Any, hint: str = "") -> dict:
            """一张统计卡：小标题 + 大数字 + 补充说明。"""
            body: List[dict] = [
                {
                    "component": "VCardSubtitle",
                    "props": {"class": "text-caption text-medium-emphasis pb-0"},
                    "text": label,
                },
                {
                    "component": "VCardTitle",
                    "props": {"class": "text-h5 font-weight-bold py-1"},
                    "text": str(value),
                },
            ]
            if hint:
                body.append(
                    {
                        "component": "VCardText",
                        "props": {"class": "text-caption pt-0 pb-2"},
                        "text": hint,
                    }
                )
            return {
                "component": "VCard",
                "props": {"variant": "tonal", "color": color, "class": "h-100 gtc-stat"},
                "content": body,
            }

        def grid(cells: List[dict]) -> dict:
            return {"component": "VRow", "props": {"class": "mb-1"}, "content": cells}

        def cell(span: int, inner: List[dict], md: int = 0, sm: int = 0) -> dict:
            props: Dict[str, Any] = {"cols": span, "class": "py-1"}
            if md:
                props["md"] = md
            if sm:
                props["sm"] = sm
            return {"component": "VCol", "props": props, "content": inner}

        # ---- 顶部状态 ----------------------------------------------------
        if self._scanning:
            head = "正在扫描整理记录，请稍后刷新本页查看结果…"
            head_type = "info"
        elif not stats:
            head = "尚未扫描。点击下方「立即扫描」开始体检。"
            head_type = "info"
        else:
            head = (
                f"最近扫描 {stats.get('scan_time', '-')}｜"
                f"检查 {stats.get('scanned', 0)}/{stats.get('total', 0)} 条整理记录｜"
                f"耗时 {stats.get('duration', 0)} 秒｜"
                f"来源 {len(sources)} 种"
            )
            if stats.get("truncated"):
                head += "｜⚠️ 本次扫描超时提前结束，结果可能不完整"
            if stats.get("limited"):
                head += (
                    f"｜⚠️ 受「单次最多检查记录数」限制，仅检查了前 {stats.get('scanned', 0)} 条，"
                    "建议把该值设为 0（不限）后重扫"
                )
            if stats.get("index_truncated"):
                head += "｜⚠️ strm 全库索引不完整，兜底匹配可能漏判"
            if stats.get("unknown"):
                head += f"｜{stats.get('unknown')} 条记录无法判断（已跳过）"
            head_type = "warning" if stats.get("ghost") else "success"
        content.append(alert(head_type, head))

        if not self._strm_paths:
            content.append(
                alert(
                    "error",
                    "尚未配置「strm 媒体库根目录」，插件无法判断整理记录是否还有效。"
                    "请先到插件配置中填写存放 strm 文件的本地目录（例如 /strm）再扫描；"
                    "配置完成前，扫描不会得出任何结论。",
                )
            )

        if stats.get("last_action"):
            content.append(alert("info", f"上次操作：{stats['last_action']}", density="compact"))

        if stats.get("suspicious"):
            content.append(
                alert(
                    "error",
                    "⚠️ 超过八成整理记录都找不到对应文件，这更像目录映射/挂载配错了"
                    "（例如库根目录填错、媒体库结构变过），而不是文件真被删。"
                    "已自动禁止清理，请先核对「路径映射 + 来源区分」里的规则。",
                )
            )

        # ---- 统计卡片 ----------------------------------------------------
        if stats:
            ok_count = max(
                0,
                int(stats.get("scanned", 0))
                - int(stats.get("ghost", 0))
                - int(stats.get("unverified", 0))
                - int(stats.get("unknown", 0)),
            )
            content.append(
                grid(
                    [
                        cell(6, [stat_card("primary", "整理记录总数", stats.get("total", 0),
                                             f"本次检查 {stats.get('scanned', 0)} 条")], md=3, sm=6),
                        cell(6, [stat_card("success", "正常", ok_count,
                                             "在本地 strm 库里能找到对应文件")], md=3, sm=6),
                        cell(6, [stat_card("error", "幽灵记录", stats.get("ghost", 0),
                                             f"整部缺失 {stats.get('whole', 0)} · "
                                             f"局部缺失 {stats.get('part', 0)}"
                                             + (f" · 合并 {len(groups_all)} 个剧集"
                                                if self._merge and groups_all else ""))], md=3, sm=6),
                        cell(6, [stat_card("warning", "未核验", stats.get("unverified", 0),
                                             "所属库目录没挂载，查不了")], md=3, sm=6),
                    ]
                )
            )

        # 来源分布 chips
        chip_list: List[dict] = []
        for label, count in sources.items():
            chip_list.append(
                {
                    "component": "VChip",
                    "props": {"size": "small", "variant": "tonal", "color": "primary"},
                    "text": f"{label} {count}",
                }
            )
        for label, count in unverified_sources.items():
            chip_list.append(
                {
                    "component": "VChip",
                    "props": {"size": "small", "variant": "tonal", "color": "warning"},
                    "text": f"{label}（未核验）{count}",
                }
            )
        if chip_list:
            content.append(
                grid([cell(12, [{"component": "div",
                                 "props": {"class": "d-flex flex-wrap ga-2 align-center mb-2"},
                                 "content": [
                                     {"component": "span",
                                      "props": {"class": "text-caption text-medium-emphasis mr-2"},
                                      "text": "来源分布"},
                                 ] + chip_list}])])
            )

        # ---- 操作按钮 ----------------------------------------------------
        content.append(
            grid([
                cell(12, [{
                    "component": "div",
                    "props": {"class": "d-flex flex-wrap ga-2"},
                    "content": [
                        action_btn("立即扫描", "mdi-magnify", "primary", "/scan"),
                        action_btn("生成诊断报告", "mdi-clipboard-text-search", "info", "/diagnose"),
                        action_btn("把报告发到通知", "mdi-bell-send", "secondary", "/notify_report"),
                        action_btn("清理幽灵记录", "mdi-delete-sweep", "error", "/clean"),
                    ],
                }])
            ])
        )

        if self._diagnosing:
            content.append(
                alert(
                    "warning",
                    "诊断报告正在后台生成（要遍历 strm 目录，约几十秒）。"
                    "完成后自动推送到通知渠道；稍后刷新本页即可看到完整报告。",
                )
            )

        # ---- 筛选 + 分页（合并视图下分页单位是「剧集」，不是单条记录）-------
        items, page_now, pages = self.__page_slice(groups_all if self._merge else filtered)
        per_label = "全部" if self._per_page <= 0 else str(self._per_page)
        count_note = (
            f"{len(groups_all)} 个剧集 / {len(filtered)} 条记录"
            if self._merge else f"共 {len(filtered)} 条"
        )

        if ghosts_all:
            # 工具条：每组 = 固定宽度标签列 + 一组等宽按钮。
            # 等宽由后端按组内最长文字算好（内联 min-width）；手机端按组等分成栅格，
            # 任何屏宽下都是「标签列对齐 + 组内按钮齐平」，不会再参差或错位折行。
            def _txt_w(text: str) -> float:
                """估算文字宽度：全角 13px、半角 7.2px、空格 4px。"""
                width = 0.0
                for ch in str(text):
                    if ch == " ":
                        width += 4.0
                    elif ord(ch) > 0x2E80:
                        width += 13.0
                    else:
                        width += 7.2
                return width

            def seg(label: str, items: List[Tuple[str, dict]],
                    extra: Optional[dict] = None) -> dict:
                """items = [(按钮文字, 按钮节点)]：同组统一 min-width，手机端按需分列。"""
                widest = max((_txt_w(text) for text, _ in items), default=0.0)
                desktop_w = int(widest + 24 + 3)          # 左右内边距 + 余量
                mobile_cols = max(1, min(len(items), int(310.0 / (widest + 10)) or 1))
                nodes: List[dict] = []
                for _text, node in items:
                    node.setdefault("props", {})["style"] = f"min-width:{desktop_w}px;"
                    nodes.append(node)
                children: List[dict] = [
                    {"component": "span", "props": {"class": "gtc-sec-lab"}, "text": label},
                    {
                        "component": "div",
                        "props": {"class": "gtc-seg", "style": f"--gtc-cols:{mobile_cols};"},
                        "content": nodes,
                    },
                ]
                if extra:
                    children.append(extra)
                return {"component": "div", "props": {"class": "gtc-sec"}, "content": children}

            def fbtn(text: str, params: Dict[str, Any], active: bool,
                     color: str = "primary", cls: str = "") -> Tuple[str, dict]:
                """筛选类按钮：选中 = 实心彩色，未选中 = 浅灰底 + 深灰字（尺寸完全一致）。"""
                state = "gtc-on" if active else "gtc-off"
                return (text, action_btn(text, "", color if active else "grey", "/page",
                                         params, variant="flat" if active else "tonal",
                                         cls=f"{cls} {state}".strip()))

            # 来源筛选（切换来源时保持缺失程度筛选不变）
            src_items: List[Tuple[str, dict]] = [
                fbtn(f"全部 {len(ghosts_all)}",
                     {"p": 1, "source": "", "level": self._filter_level},
                     not self._filter_source, cls="gtc-sbtn"),
            ]
            for label, count in sources.items():
                src_items.append(
                    fbtn(f"{label} {count}",
                         {"p": 1, "source": label, "level": self._filter_level},
                         self._filter_source == label, cls="gtc-sbtn")
                )

            # 缺失程度筛选（切换时保持来源筛选不变）
            level_items: List[Tuple[str, dict]] = [
                fbtn("不限", {"p": 1, "source": self._filter_source, "level": ""},
                     self._filter_level == "", cls="gtc-lbtn"),
                fbtn(f"整部缺失 {stats.get('whole', 0)}",
                     {"p": 1, "source": self._filter_source, "level": LEVEL_WHOLE},
                     self._filter_level == LEVEL_WHOLE, "error", "gtc-lbtn"),
                fbtn(f"局部缺失 {stats.get('part', 0)}",
                     {"p": 1, "source": self._filter_source, "level": LEVEL_PART},
                     self._filter_level == LEVEL_PART, "warning", "gtc-lbtn"),
            ]

            # 每页条数
            per_items: List[Tuple[str, dict]] = [
                fbtn(text,
                     {"p": 1, "source": self._filter_source,
                      "level": self._filter_level, "per": value},
                     self._per_page == value, cls="gtc-pbtn")
                for value, text in ((50, "50"), (100, "100"), (200, "200"), (500, "500"), (0, "全部"))
            ]

            # 显示方式：合并（一个剧集一行）/ 逐条（原始记录）
            view_items: List[Tuple[str, dict]] = [
                fbtn(text,
                     {"p": 1, "source": self._filter_source,
                      "level": self._filter_level, "merge": value},
                     bool(self._merge) == bool(value), cls="gtc-vbtn")
                for value, text in ((1, "合并显示"), (0, "逐条显示"))
            ]

            # 翻页
            first_off = page_now <= 1
            last_off = page_now >= pages

            def nav(target: int, text: str, off: bool) -> Tuple[str, dict]:
                state = "gtc-off" if off else "gtc-page"
                return (text, action_btn(text, "", "grey" if off else "primary", "/page",
                                         {"p": target, "source": self._filter_source,
                                          "level": self._filter_level},
                                         variant="tonal", cls=f"gtc-nbtn {state}"))

            page_items: List[Tuple[str, dict]] = [
                nav(1, "首页", first_off),
                nav(max(1, page_now - 1), "上一页", first_off),
                nav(min(pages, page_now + 1), "下一页", last_off),
                nav(pages, "末页", last_off),
            ]

            filter_rows: List[dict] = [
                {
                    "component": "div",
                    "props": {"class": "gtc-tbrow"},
                    "content": [seg("来源", src_items), seg("缺失程度", level_items)],
                },
                {
                    "component": "div",
                    "props": {"class": "gtc-tbrow"},
                    "content": [
                        seg("每页", per_items),
                        seg("显示", view_items),
                        seg(
                            "翻页",
                            page_items,
                            extra={
                                "component": "div",
                                "props": {"class": "gtc-pinfo gtc-only-mob"},
                                "text": f"第 {page_now} / {pages} 页 · {count_note}",
                            },
                        ),
                        {
                            "component": "div",
                            "props": {"class": "gtc-pinfo gtc-only-desk"},
                            "text": f"第 {page_now} / {pages} 页",
                        },
                    ],
                },
            ]

            content.append(
                {
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "mb-3 gtc-fbox"},
                    "content": filter_rows,
                }
            )
        # ---- 幽灵记录表格（合并视图：一个剧集一行，可展开每集明细）--------
        rows: List[dict] = []
        if self._merge:
            for group in items:
                members = group["records"]
                if group["single"]:
                    # 组内只有一条（电影 / 单集）：渲染成和逐条显示完全一样的普通行
                    one = members[0]
                    is_whole = one.get("level") == LEVEL_WHOLE
                    rows.append({
                        "title": f"{one.get('title', '')} {one.get('year', '')}".strip() or "-",
                        "type": one.get("type") or "-",
                        "season_episode": (
                            f"{one.get('seasons', '') or ''}{one.get('episodes', '') or ''}".strip()
                            or "-"
                        ),
                        "level_text": f"{'🔴' if is_whole else '🟡'} "
                                      f"{'整部缺失' if is_whole else '局部缺失'}",
                        "source": one.get("source") or SOURCE_NONE,
                        "date": one.get("date") or "-",
                        "dest": one.get("dest") or "-",
                    })
                    continue
                title_html = (
                    '<label class="gtc-exp" title="展开 / 收起每集明细">'
                    '<input type="checkbox" class="gtc-tg">'
                    '<span class="gtc-chev">&#9654;</span></label>'
                    + _esc(group["title"])
                    + f'<span class="gtc-cnt">{group["count"]} 条</span>'
                )
                chips: List[str] = []
                plain: List[str] = []
                if group["whole"]:
                    chips.append(
                        f'<span class="gtc-chip" style="{_CHIP_TONES["red"]}">'
                        f'整部缺失 {group["whole"]}</span>'
                    )
                    plain.append(f"整部缺失 {group['whole']}")
                if group["part"]:
                    chips.append(
                        f'<span class="gtc-chip" style="{_CHIP_TONES["amber"]}">'
                        f'局部缺失 {group["part"]}</span>'
                    )
                    plain.append(f"局部缺失 {group['part']}")
                rows.append({
                    "title": f"{group['title']}（{group['count']} 条）",
                    "type": group["type"],
                    "season_episode": group["season_label"] or "-",
                    "level_text": " / ".join(plain) or "-",
                    "source": "·".join(group["sources"]) or SOURCE_NONE,
                    "date": group["date"],
                    "dest": group["dest"] or (members[0].get("dest") or "-"),
                    "__cls__": "gtc-grp",
                    "__cells__": {"title": title_html, "level_text": "".join(chips)},
                    "__detail__": _group_detail_html(group),
                })
        else:
            for ghost in items:
                title = f"{ghost.get('title', '')} {ghost.get('year', '')}".strip()
                season_episode = (
                    f"{ghost.get('seasons', '') or ''}{ghost.get('episodes', '') or ''}".strip()
                )
                is_whole = ghost.get("level") == LEVEL_WHOLE
                rows.append(
                    {
                        "title": title or "-",
                        "type": ghost.get("type") or "-",
                        "season_episode": season_episode or "-",
                        "level_text": f"{'🔴' if is_whole else '🟡'} "
                                      f"{'整部缺失' if is_whole else '局部缺失'}",
                        "source": ghost.get("source") or SOURCE_NONE,
                        "date": ghost.get("date") or "-",
                        "dest": ghost.get("dest") or "-",
                    }
                )

        if rows:
            if self._merge:
                caption = (
                    f"已按剧集合并：本页 {len(rows)} 个剧集 / 当前筛选 {len(filtered)} 条记录"
                    f" · 点「▶」展开每集明细（含完整路径）；桌面端鼠标悬停整行可展开目标路径"
                )
            else:
                caption = "当前筛选下本页 " + str(len(rows)) + " 条 · 手机端路径完整换行显示，桌面端鼠标悬停该行可展开完整路径"
            content.append(
                _table_node(
                    [
                        ("标题", "title", "17%", False),
                        ("类型", "type", "6%", False),
                        ("季集", "season_episode", "7%", False),
                        ("缺失程度", "level_text", "10%", False),
                        ("来源", "source", "8%", False),
                        ("整理时间", "date", "12%", False),
                        ("记录里的目标路径", "dest", "", True),
                    ],
                    rows,
                    caption=caption,
                    with_css=True,
                )
            )

        elif ghosts_all:
            content.append(alert("info", "当前筛选条件下没有幽灵记录。"))
        else:
            content.append(alert("info", "还没有扫描结果，点「立即扫描」开始体检。"))

        # ---- 未核验清单 --------------------------------------------------
        if unverified:
            content.append(
                alert(
                    "warning",
                    f"另有 {len(unverified)} 条记录属于「未挂载的库」，无法核验文件是否存在"
                    "（不计入幽灵，也不参与清理）："
                    + "、".join(f"{k} {v} 条" for k, v in unverified_sources.items())
                    + "。若希望一起核验，需要把该库目录挂载进 MoviePilot 容器后重扫。",
                )
            )
            un_rows: List[dict] = []
            for one in unverified[:50]:
                title = f"{one.get('title', '')} {one.get('year', '')}".strip()
                season_episode = (
                    f"{one.get('seasons', '') or ''}{one.get('episodes', '') or ''}".strip()
                )
                un_rows.append(
                    {
                        "title": title or "-",
                        "season_episode": season_episode or "-",
                        "source": one.get("source") or SOURCE_BLANK,
                        "date": one.get("date") or "-",
                        "dest": one.get("dest") or "-",
                    }
                )
            content.append(
                _table_node(
                    [
                        ("标题", "title", "22%", False),
                        ("季集", "season_episode", "10%", False),
                        ("来源", "source", "10%", False),
                        ("整理时间", "date", "15%", False),
                        ("记录里的目标路径", "dest", "", True),
                    ],
                    un_rows,
                )
            )

        # ---- 诊断报告 ----------------------------------------------------
        if self._report:
            content.append(
                alert(
                    "info",
                    f"诊断报告（生成于 {self._report_time or '-'}）：把整理记录里的路径与 strm 库里的"
                    "实际目录逐条摆在一起对照，用于判断到底是「文件真被删」还是「目录配置/结构对不上」。"
                    "报告只读本地文件系统，不访问云端。"
                    + (f"完整报告已写入：{self._report_path}" if self._report_path else ""),
                )
            )
            content.append(
                {
                    "component": "VCard",
                    "props": {
                        "variant": "outlined",
                        "class": "pa-3 mb-3 gtc-rep",
                        "style": "white-space: pre-wrap; word-break: break-all;"
                                 " font-family: ui-monospace, Consolas, 'Courier New', monospace;"
                                 " font-size: 12px; line-height: 1.5;"
                                 " max-height: 50vh; overflow: auto;",
                    },
                    "text": self._report,
                }
            )

        # ---- 底部说明 ----------------------------------------------------
        foot = (
            f"共 {len(ghosts_all)} 条幽灵记录，可翻页查看，也可按来源/缺失程度筛选。"
            "判定依据是本地 strm 文件：「整部缺失」表示库里连该节目目录都找不到，可信度最高；"
            "「局部缺失」表示节目目录还在、只是该文件对应的 strm 没了，建议先人工确认。"
            "目录被改名、换过分类的情况已由「目录模糊解析 + 全库索引」自动兼容。"
        )
        if self._ghost_csv_path:
            foot += (
                f" 本次扫描的全量清单（{len(ghosts_all)} 条）已导出到：{self._ghost_csv_path}"
                "，可直接下载或用表格软件打开核对。"
            )
        if not self._allow_clean:
            foot += " 当前未开启「允许一键清理」，页面上的清理按钮不会真正删除记录。"
        content.append(alert("info", foot))

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
            self._key_cache = {}
            self._index_files = {}
            self._index_dir_map = {}
            self._index_dir_map_yl = {}
            self._root_dir_map = {}
            self._index_truncated = False

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
                    "unverified": 0,
                    "sources": {},
                    "unverified_sources": {},
                    "truncated": False,
                    "limited": False,
                    "suspicious": False,
                    "loose_match": bool(self._loose_match),
                    "index_truncated": False,
                    "duration": 0,
                    "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "cleaned": 0,
                    "last_action": "尚未配置 strm 媒体库根目录，无法判断整理记录是否有效",
                    "strm_paths": [],
                }
                self._ghosts = []
                self._unverified = []
                self._stats = stats
                self.__save()
                logger.warning("幽灵整理记录：未配置 strm 媒体库根目录，本次扫描未执行")
                return stats

            rows, total = self.__load_records()

            # 先建 strm 全库索引：用于「记录里的路径对不上」时的兜底定位
            self.__build_index()

            ghosts: List[Dict[str, Any]] = []
            unverified: List[Dict[str, Any]] = []
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
                elif state == STATE_UNVERIFIED and item:
                    unverified.append(item)
                elif state == "unknown":
                    unknown += 1

            whole = len([g for g in ghosts if g["level"] == LEVEL_WHOLE])
            part = len([g for g in ghosts if g["level"] == LEVEL_PART])
            suspicious = scanned >= SUSPICIOUS_MIN_SAMPLE and len(ghosts) >= scanned * SUSPICIOUS_RATIO

            def count_by_source(items: List[Dict[str, Any]]) -> Dict[str, int]:
                """按来源标签统计条数，来源多的排前面。"""
                counter: Dict[str, int] = {}
                for one in items:
                    label = one.get("source") or SOURCE_NONE
                    counter[label] = counter.get(label, 0) + 1
                return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))

            stats: Dict[str, Any] = {
                "engine": ENGINE_VERSION,
                "total": total,
                "scanned": scanned,
                "ghost": len(ghosts),
                "whole": whole,
                "part": part,
                "unknown": unknown,
                "unverified": len(unverified),
                "sources": count_by_source(ghosts),
                "unverified_sources": count_by_source(unverified),
                "truncated": truncated,
                # 受「单次最多检查记录数」限制，后面的记录没检查到
                "limited": bool(not truncated and total > scanned),
                "suspicious": suspicious,
                "loose_match": bool(self._loose_match),
                "index_truncated": bool(self._index_truncated),
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
            self._unverified = unverified[:CACHE_LIMIT]
            self._stats = stats
            # 每次扫描后回到第一页，避免分页停在越界页码
            self._page = 1
            # 把**完整**清单落盘：页面表格分页展示，用户要核对「到底是哪些记录」
            # 必须能一次拿到全量
            try:
                self._ghost_csv_path = self.__write_ghost_csv(ghosts)
            except Exception as err:
                logger.error(f"幽灵整理记录：导出完整幽灵清单失败：{err}")
                self._ghost_csv_path = ""
            self.__save()

            if notify:
                self.__notify_result(stats, self._ghosts)

            if unverified:
                logger.info(
                    f"幽灵整理记录：{len(unverified)} 条记录因所属库目录未挂载而无法核验"
                    f"（{stats['unverified_sources']}）"
                )
            logger.info(
                f"幽灵整理记录：体检完成（依据本地 strm），检查 {scanned}/{total} 条，"
                f"发现幽灵记录 {stats['ghost']} 条（整部缺失 {stats['whole']}、局部缺失 {stats['part']}）"
                f"，来源分布 {stats['sources']}"
                + ("，结果不完整：受 max_records 限制" if stats.get("limited") else "")
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
            # 按 id 倒序：一旦限额，优先检查最近的记录，且每次结果稳定可复现
            query = query.order_by(TransferHistory.id.desc())
            if self._max_records > 0:
                query = query.limit(self._max_records)
            records = query.all()
            rows = [self.__to_dict(record) for record in records]
        finally:
            db.close()
        return rows, total

    def __check(self, row: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        检查单条整理记录，返回（ok / ghost / unverified / unknown, 详情）。

        「来源」由配置里的路径映射表（`记录前缀=本地目录|来源标签`）决定：
        命中规则后只在该来源对应的库目录里比对，不会拿 115 的记录去夸克库里
        找同名文件；规则对应的库目录若不在容器里（例如夸克库没挂进来），返回
        unverified ——「查不了」和「确实没了」必须分开，否则会误报成幽灵。
        没配映射时回退到原来的「全库后缀匹配」，行为与旧版一致。
        """
        raw_dest = row.get("dest") or ""
        # 路径原样保留（Linux 下反斜杠是合法文件名字符），仅展示时统一分隔符
        dests = [
            item.strip()
            for item in str(raw_dest).replace("\r", "\n").split("\n")
            if item.strip()
        ]
        if not dests:
            return "unknown", None

        source, lib_root = self.__source_of(dests[0])

        # 映射命中、但该库目录并不存在于容器内：无法核验，不作为幽灵
        if lib_root and not os.path.isdir(lib_root):
            return STATE_UNVERIFIED, {
                "id": row.get("id"),
                "title": row.get("title") or "",
                "year": row.get("year") or "",
                "type": row.get("type") or "",
                "category": row.get("category") or "",
                "seasons": row.get("seasons") or "",
                "episodes": row.get("episodes") or "",
                "date": row.get("date") or "",
                "src": row.get("src") or "",
                "dest": self.__display_path(dests[0]),
                "source": source or SOURCE_BLANK,
                "reason": f"记录属于「{source or SOURCE_BLANK}」库（{lib_root}），"
                          f"但该目录没有挂载到 MoviePilot 容器，无法核验文件是否存在",
            }

        # 主判定：记录的目标路径在本地 strm 目录里还能不能找到对应文件。
        # 注意：整理记录里的 files 字段存的是「整理前的下载源文件清单」
        # （TransferInfo.file_list，形如 /视频/qb/.../xxx.mkv），与 strm 库无关，
        # 拿它去比对 strm 必然 100% 对不上。早期版本用它做「深度检查」，
        # 结果把所有记录都误判成幽灵，这个开关已改为「宽松匹配」。
        roots = [lib_root] if lib_root else None
        states = [self.__probe_strm(path, roots) for path in dests]
        if all(state == STATE_UNKNOWN for state in states):
            return STATE_UNKNOWN, None
        if STATE_OK in states:
            return STATE_OK, None

        if all(state == LEVEL_WHOLE for state in states):
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
            "source": source or SOURCE_NONE,
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
    def __probe_strm(self, path: str, roots: Optional[List[str]] = None) -> str:
        """
        判断一条记录路径在本地 strm 库中是否还有对应文件。

        采用「后缀匹配」：把记录路径逐级剥掉前导目录，拼到每个 strm 根目录下查找
        同名 .strm，命中即认为媒体还在。这样可兼容「记录里是 115 路径、本地是 strm
        目录」这类前缀不一致的情况，无需用户精确配置路径映射。

        :return ok（找到对应 strm）/ whole（连节目目录都找不到，整部缺失）/
                part（节目目录还在但该文件的 strm 没了）/ unknown（无法判断）/
                unverified（该记录所属库的目录没挂载到容器，无从判断）
        """
        if not path:
            return STATE_UNKNOWN
        # 先按路径映射判断来源：命中规则时只在该来源的库里找；
        # 库目录不在容器里则直接返回「未核验」，避免误报成幽灵。
        _source, lib_root = self.__source_of(path)
        if lib_root and not os.path.isdir(lib_root):
            return STATE_UNVERIFIED
        return self.__probe_normalized(
            self.__normalize(path), [lib_root] if lib_root else roots
        )

    # ------------------------------------------------------------------
    # 集号识别：记录里的文件名与库里的实际文件名经常只差发布组标记
    # （`...BD.HEVC.FLAC-Snow-Raws.mkv` vs `...BD.HEVC-Snow-Raws.strm`），
    # 逐字比对会把这类记录全部误判成「局部缺失」，所以补一层「同季同集」比对。
    # ------------------------------------------------------------------
    @staticmethod
    def __season_no(name: str) -> int:
        """从 `Season 01` / `S01` / `第 1 季` / `S01E10` 里取季号，取不到返回 0。"""
        text = str(name or "").strip()
        if not text:
            return 0
        match = SEASON_DIR_RE.match(text) or SEASON_DIR_CN_RE.match(text)
        if match:
            return int(match.group(1))
        match = EP_SXXEXX_RE.search(text)
        if match:
            return int(match.group(1))
        match = SEASON_WORD_RE.search(text)
        if match:
            return int(match.group(1) or match.group(2))
        return 0

    @staticmethod
    def __episode_tokens(name: str, season_hint: int = 0) -> Set[Tuple[int, int]]:
        """
        给出文件名覆盖的 {(季号, 集号)} 集合。

        支持 `S01E10`、`S01E01-E03`、`第 10 集 / 第10话`、`EP10` 等写法；
        文件名里没有季号时用 season_hint（所在季目录的季号）补，仍补不上记 0。
        """
        text = str(name or "")
        if not text:
            return set()
        tokens: Set[Tuple[int, int]] = set()
        matched = False
        for match in EP_SXXEXX_RE.finditer(text):
            matched = True
            season = int(match.group(1))
            tokens.add((season, int(match.group(2))))
            # `S01E01-E03` / `S01E01-03` 这类区间写法：连带把区间内的集号都算上
            span = EP_TAIL_RE.match(text[match.end(): match.end() + 12])
            if span:
                last = int(span.group(2))
                gap = last - int(match.group(2))
                limit = EP_RANGE_MAX if span.group(1) else EP_RANGE_GAP_BARE
                if 0 < gap <= limit:
                    first = int(match.group(2))
                    tokens.update((season, episode) for episode in range(first, last + 1))
        for match in EP_CN_RE.finditer(text):
            matched = True
            tokens.add((season_hint or 0, int(match.group(1))))
        if not matched:
            # 兜底：`EP10` / `E.10` 这类没写季号的写法（只在没有别的集号写法时才用）
            for match in EP_BARE_RE.finditer(text):
                number = match.group(1) or match.group(2)
                if number:
                    tokens.add((season_hint or 0, int(number)))
        return {(season or season_hint or 0, episode) for season, episode in tokens}

    @staticmethod
    def __ep_tokens_hit(actual: Set[Tuple[int, int]], wanted: Set[Tuple[int, int]],
                        season_known: bool) -> bool:
        """集号比对：季号都明确时要求「同季同集」，任一侧季号未知时只比集号。"""
        if actual & wanted:
            return True
        if season_known and all(season for season, _episode in wanted):
            return False
        return bool(
            {episode for _season, episode in actual}
            & {episode for _season, episode in wanted}
        )

    def __has_episode(self, directory: str, wanted: Set[Tuple[int, int]]) -> bool:
        """
        目录（或其下的季目录）里是否存在覆盖 wanted 中某一集的文件。

        用在「同名文件找不到、但同季同集的其他文件还在」的场景：媒体并没有丢，
        只是整理记录里的文件名与库里实际文件名不一致（换了发布组、后续重命名、
        重新刮削后补 `第 N 集` 之类）。这种情况下不能算幽灵记录。
        """
        if not wanted:
            return False
        entries = self.__list_entries(directory)
        if not entries:
            return False
        hint = self.__season_no(os.path.basename(directory))
        wanted_seasons = {season for season, _episode in wanted if season}
        children: List[str] = []
        for actual, is_dir in entries.values():
            if is_dir:
                if len(children) < EP_SUBDIR_LIMIT:
                    children.append(actual)
                continue
            if self.__ep_tokens_hit(self.__episode_tokens(actual, hint), wanted, bool(hint)):
                return True
        # 再进一层：记录指向的是节目目录本身时，集号在季子目录里
        for child in children:
            child_season = self.__season_no(child)
            if child_season and wanted_seasons and child_season not in wanted_seasons:
                continue
            sub_entries = self.__list_entries(f"{str(directory).rstrip('/')}/{child}")
            if not sub_entries:
                continue
            sub_hint = child_season or hint
            for actual, is_dir in sub_entries.values():
                if is_dir:
                    continue
                if self.__ep_tokens_hit(self.__episode_tokens(actual, sub_hint), wanted, bool(sub_hint)):
                    return True
        return False

    def __probe_normalized(self, normalized: str, roots: Optional[List[str]] = None) -> str:
        """
        判断一个归一化路径在本地 strm 库中是否还有对应文件。

        分三层，逐层放宽：

        1. 「前缀剥离 + 目录模糊解析」：从最完整的相对路径开始逐级剥掉前导目录，
           在 strm 根目录下按相对路径查找同名 .strm。目录名先按原样匹配，匹配不上
           再按归一化名称匹配（忽略大小写/分隔符、忽略 [tmdbid-xxx] 标记、
           忽略 Season 2 与 Season 02 的写法差异）。
        2. 「按节目目录全库定位」：第 1 层落空时，用节目目录名在整个 strm 库里
           找它的实际落点，再往下核对季目录与文件。媒体库初始化后目录被改名、
           换了分类目录时，只有这一层认得出来。
        3. 「按文件名全库查找」（宽松匹配，可关闭）：只认文件名，最宽松，
           用于目录层级结构完全对不上的情况。

        :return ok（找到对应 strm）/ whole（连节目目录都找不到，整部缺失）/
                part（节目目录还在但该文件的 strm 没了）/ unknown（无法判断）
        """
        search_roots = [r for r in (roots if roots is not None else self._strm_paths) if r]
        segments = [seg for seg in str(normalized).split("/") if seg]
        if not segments:
            return STATE_UNKNOWN

        filename = segments[-1]
        dirs = segments[:-1]
        names = self.__strm_names(filename)
        if not names:
            return STATE_UNKNOWN

        dir_found = False

        # 记录侧的季号（取最内层季目录）与集号；记录里没有集号（电影、字幕等）时
        # 下面两层集号比对自动不生效，行为与旧版一致
        season_hint = 0
        for segment in reversed(dirs):
            season_hint = self.__season_no(segment)
            if season_hint:
                break
        wanted_episodes = self.__episode_tokens(filename, season_hint)

        # 第 1 层：剥离前导目录后按相对路径查找（限定在该记录所属的库里）
        for root in search_roots:
            for drop in range(0, min(len(dirs), MAX_TAIL_DEPTH) + 1):
                tail = dirs[drop:]
                if not tail:
                    # tail 为空 = 已经剥到 strm 根目录本身。根目录当然存在，
                    # 不能据此认定「节目目录还在」，否则整部缺失会被误报成局部缺失。
                    continue
                parent = self.__resolve_dir(root, tail)
                if parent is None:
                    continue
                if self.__has_file(parent, names):
                    return STATE_OK
                # 同名文件不在，但同季同集的其他发布名还在：媒体没丢，
                # 只是记录里的文件名与库里实际文件名不一致，不算幽灵记录
                if wanted_episodes and self.__has_episode(parent, wanted_episodes):
                    return STATE_OK
                # 目录链（含节目目录）对上了，只是这个文件不在：属于「局部缺失」
                dir_found = True

        # 第 2 层：按节目目录名在全库索引里定位
        candidates: List[str] = list(self.__show_dir_candidates(dirs, search_roots))
        # 记录映射到的其它库（本地目录、网盘挂载）没进全库索引，这里按需补扫
        for root in (search_roots or []):
            for parent in self.__root_show_candidates(root, dirs):
                if parent not in candidates:
                    candidates.append(parent)
        for parent in candidates:
            if self.__has_file(parent, names):
                return STATE_OK
            if wanted_episodes and self.__has_episode(parent, wanted_episodes):
                return STATE_OK
            dir_found = True

        # 第 3 层：宽松匹配，只认文件名
        if self._loose_match and self.__name_index_hit(filename, search_roots):
            return STATE_OK

        return LEVEL_PART if dir_found else LEVEL_WHOLE

    def __has_file(self, directory: str, names: List[str]) -> bool:
        """目录下是否存在候选文件名对应的普通文件。"""
        entries = self.__list_entries(directory)
        if not entries:
            return False
        for name in names:
            found = entries.get(str(name).lower())
            if found is not None and not found[1]:
                return True
        return False

    def __resolve_dir(self, root: str, tail: List[str]) -> Optional[str]:
        """
        在 strm 根目录下逐级解析相对目录段，返回真实存在的绝对路径。

        段名先按原样匹配，匹配不上再用归一化名称在子目录里宽松匹配 ——
        这一步专门用来兼容「记录里的目录名与库里的目录名不完全一致」，
        例如 中国综艺/综艺、Season 2/Season 02、有无 [tmdbid-xxx] 后缀。

        :return 解析成功返回实际路径（沿用库里的真实大小写），任一段对不上返回 None
        """
        current = str(root).rstrip("/")
        for segment in tail or []:
            segment = str(segment or "").strip()
            if not segment:
                continue
            entries = self.__list_entries(current)
            if not entries:
                return None
            found = entries.get(segment.lower())
            if found is None or not found[1]:
                # 宽松匹配：按归一化名称在子目录里找
                actual = self.__key_map(current).get(self.__key(segment))
                found = entries.get(actual.lower()) if actual else None
            if found is None or not found[1]:
                return None
            current = f"{current}/{found[0]}"
        return current

    def __show_dir_candidates(self, dirs: List[str], roots: Optional[List[str]] = None) -> List[str]:
        """用节目目录名在全库索引里定位，返回所有可能的落点（含季目录）。

        roots 非空时只取这些库根目录下的落点。
        """
        show, season = self.__split_tail(dirs)
        if not show or not self._index_dir_map:
            return []
        result: List[str] = []
        candidates = list(self._index_dir_map.get(self.__key(show)) or [])
        if not candidates:
            # 精确名对不上时退一步用「忽略年份」的目录名定位：媒体库重新刮削后
            # 年份可能变化（记录里 2023、库里 2022），但仍是同一部剧
            yearless = YEAR_RE.sub("", self.__key(show))
            if yearless and len(yearless) >= 3:
                candidates = list(self._index_dir_map_yl.get(yearless) or [])
        for path in candidates:
            if roots and not any(path == root or path.startswith(f"{root}/") for root in roots):
                continue
            if path not in result:
                result.append(path)
            if season:
                child = self.__resolve_dir(path, season)
                if child and child not in result:
                    result.append(child)
        return result

    @classmethod
    def __dir_index_keys(cls, name: str) -> List[str]:
        """目录名参与索引的 key：归一化名 + 忽略年份的归一化名。"""
        key = cls.__key(name)
        if not key:
            return []
        keys = [key]
        yearless = YEAR_RE.sub("", key)
        if yearless and yearless != key and len(yearless) >= 3:
            keys.append(yearless)
        return keys

    def __root_show_candidates(self, root: str, dirs: List[str]) -> List[str]:
        """
        在指定库根目录下按名字找节目目录（含季目录）。

        全库名索引只覆盖配置里的 strm 库（`_strm_paths`）；记录映射到的其它库
        （本地目录、网盘挂载等）没进索引，这里按需扫一次该根目录并缓存 —— 否则
        「记录里的相对路径已经变了，但节目和文件都还在同一个库里」会被误判成
        整部缺失。
        """
        show, season = self.__split_tail(dirs)
        if not show or not root or not os.path.isdir(root):
            return []
        base = str(root).rstrip("/")
        for indexed in (self._strm_paths or []):
            indexed = str(indexed).rstrip("/")
            if base == indexed or base.startswith(f"{indexed}/"):
                return []      # 这个根已经在全库索引里了，交给 __show_dir_candidates
        table = self._root_dir_map.get(root)
        if table is None:
            table = {}
            deadline = time.time() + INDEX_SECONDS
            nodes = 0
            stack = [base]
            while stack and nodes <= INDEX_LIMIT and time.time() < deadline:
                current = stack.pop()
                entries = self.__list_entries(current)
                if not entries:
                    continue
                nodes += 1
                for actual, is_dir in entries.values():
                    if not is_dir:
                        continue
                    path = f"{current}/{actual}"
                    for key in self.__dir_index_keys(actual):
                        bucket = table.setdefault(key, [])
                        if len(bucket) < 4:
                            bucket.append(path)
                    stack.append(path)
            self._root_dir_map[root] = table
            logger.debug(f"幽灵整理记录：{root} 目录索引建立完成（{len(table)} 个 key）")
        result: List[str] = []
        for key in self.__dir_index_keys(show):
            for path in table.get(key) or []:
                if path not in result:
                    result.append(path)
                if season:
                    child = self.__resolve_dir(path, season)
                    if child and child not in result:
                        result.append(child)
        return result

    def __name_index_hit(self, filename: str, roots: Optional[List[str]] = None) -> bool:
        """全库文件名索引里是否存在同名 .strm（只比文件名，最宽松的一层）。

        roots 非空时只认这些库根目录下的命中。
        """
        if not self._index_files:
            return False
        stem = str(filename or "").strip()
        if stem.lower().endswith(STRM_SUFFIX):
            stem = stem[: -len(STRM_SUFFIX)]
        elif "." in stem:
            stem = stem.rsplit(".", 1)[0]
        key = self.__key(stem)
        if not key:
            return False
        paths = self._index_files.get(key) or []
        if not paths:
            return False
        if not roots:
            return True
        return any(
            any(path == root or path.startswith(f"{root}/") for root in roots)
            for path in paths
        )

    def __build_index(self):
        """
        遍历各 strm 根目录，建立「归一化文件名 -> 路径」与「归一化目录名 -> 路径」索引。

        strm 库不大（一个 strm 对应一个云端文件，文件数是万级），遍历很快；
        仍设了节点数与耗时上限，超限时索引不完整（结果会偏保守），并在页面上提示。
        """
        files: Dict[str, List[str]] = {}
        dirs: Dict[str, List[str]] = {}
        dirs_yearless: Dict[str, List[str]] = {}
        deadline = time.time() + INDEX_SECONDS
        nodes = 0
        truncated = False
        for root in self._strm_paths:
            if not os.path.isdir(root):
                continue
            for current, subdirs, filenames in os.walk(root):
                nodes += 1 + len(filenames)
                if nodes > INDEX_LIMIT or time.time() > deadline:
                    truncated = True
                    break
                for name in subdirs:
                    key = self.__key(name)
                    if not key:
                        continue
                    bucket = dirs.setdefault(key, [])
                    if len(bucket) < 4:
                        bucket.append(os.path.join(current, name))
                    # 忽略年份的 key：重新刮削后年份可能变（记录 2023 / 库里 2022），
                    # 精确名对不上时靠这张表兜底
                    yearless = YEAR_RE.sub("", key)
                    if yearless and yearless != key and len(yearless) >= 3:
                        bucket_yl = dirs_yearless.setdefault(yearless, [])
                        if len(bucket_yl) < 4:
                            bucket_yl.append(os.path.join(current, name))
                for name in filenames:
                    if not name.lower().endswith(STRM_SUFFIX):
                        continue
                    key = self.__key(name[: -len(STRM_SUFFIX)])
                    if not key:
                        continue
                    # 同一个文件名可能在多套库里各有一份（115/夸克），
                    # 全部记下（每个名字最多 4 条，控制内存占用），
                    # 比对时按「记录所属库」过滤根路径，避免跨库误判。
                    bucket = files.setdefault(key, [])
                    if len(bucket) < 4:
                        bucket.append(os.path.join(current, name))
            if truncated:
                break

        self._index_files = files
        self._index_dir_map = dirs
        self._index_dir_map_yl = dirs_yearless
        self._index_truncated = truncated
        logger.info(
            f"幽灵整理记录：strm 全库索引建立完成（文件名 {len(files)} 个、"
            f"目录 {len(dirs)} 个{ '，⚠️ 已截断' if truncated else '' }）"
        )

    @staticmethod
    def __key(name: str) -> str:
        """
        归一化目录名/文件名，用于宽松匹配。

        抹平这些差异：大小写、空格与各种分隔符与标点、[tmdbid-123] 之类的标记、
        Season 01 / S01 / season1 的写法、第 1 季 / 第一季。
        目的是让「记录里的名字」和「库里现在的名字」在语义相同时能得到同一个 key。
        """
        text = str(name or "").lower()
        # 去掉 [tmdbid-123] / {tmdb-123} / (tmdb=123) 这类标记
        text = re.sub(r"[\[\({]\s*tmdb(?:id)?\s*[-_=]?\s*\d+\s*[\]\)}]", "", text)
        # 去掉空格与常见分隔符/标点
        text = re.sub(r"[\s\-_.·,，。、'\"()\[\]{}]+", "", text)
        # Season 01 / S01 → s1
        text = re.sub(r"^(?:season|s)0*(\d+)$", r"s\1", text)
        return text

    def __key_map(self, directory: str) -> Dict[str, str]:
        """目录内「归一化名称 -> 实际目录名」映射，用于宽松匹配（带缓存）。"""
        if directory in self._key_cache:
            return self._key_cache[directory]
        mapping: Dict[str, str] = {}
        entries = self.__list_entries(directory)
        if entries:
            for actual, is_dir in entries.values():
                if not is_dir:
                    continue
                key = self.__key(actual)
                if key:
                    mapping.setdefault(key, actual)
        if len(self._key_cache) < DIR_CACHE_LIMIT:
            self._key_cache[directory] = mapping
        return mapping

    @staticmethod
    def __is_season_dir(name: str) -> bool:
        """判断目录名是否是季目录（Season 1 / S01 / 第一季 / 第 1 季）。"""
        key = GhostTransferCleaner.__key(name)
        return bool(re.match(r"^(?:s\d+|第\d+季|第[一二三四五六七八九十]+季)$", key))

    @staticmethod
    def __split_tail(dirs: List[str]) -> Tuple[str, List[str]]:
        """把目录段拆成（节目目录名, 结尾的季目录链）。"""
        segments = [seg for seg in (dirs or []) if str(seg).strip()]
        season: List[str] = []
        while segments and GhostTransferCleaner.__is_season_dir(segments[-1]):
            season.insert(0, segments.pop())
        return (segments[-1] if segments else ""), season

    def __list_entries(self, directory: str) -> Optional[Dict[str, Tuple[str, bool]]]:
        """
        列出目录内容：{小写名称: (实际名称, 是否目录)}，并缓存结果。
        目录不存在、不可读或不是目录时返回 None 并缓存，避免重复扫描。
        保留实际名称是为了让模糊匹配出来的路径能按磁盘上的真实大小写往下走。
        """
        if directory in self._dir_cache:
            return self._dir_cache[directory]

        entries: Optional[Dict[str, Tuple[str, bool]]] = None
        try:
            if os.path.isdir(directory):
                result: Dict[str, Tuple[str, bool]] = {}
                with os.scandir(directory) as iterator:
                    for item in iterator:
                        try:
                            result[item.name.lower()] = (item.name, item.is_dir())
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
        for old, new, _source in self._path_map:
            if value == old or value.startswith(f"{old}/"):
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
        for old, new, _source in self._path_map:
            label = f"｜来源：{_source}" if _source else "｜来源：未标注"
            bullet(f"{old}  =>  {new}{label}", indent=2)
        bullet(f"单次最多检查记录数：{self._max_records or '不限'}")
        bullet(f"只检查 N 天前的记录：{self._min_age_days}（0 表示全部）")
        bullet(f"宽松匹配（按文件名在全库兜底查找）：{'开' if self._loose_match else '关'}")
        if self._index_truncated:
            bullet("⚠️ 全库索引建立不完整（超出遍历上限），兜底匹配偏保守", indent=2)
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
            if self._stats.get("sources"):
                _src = self._stats.get("sources") or {}
                bullet(
                    "幽灵记录来源分布："
                    + "、".join(f"{k} {v} 条" for k, v in _src.items()),
                    indent=2,
                )
            if self._stats.get("unverified"):
                _uv = self._stats.get("unverified_sources") or {}
                bullet(
                    f"未核验 {self._stats.get('unverified')} 条（所属库目录未挂载）："
                    + "、".join(f"{k} {v} 条" for k, v in _uv.items()),
                    indent=2,
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
        # 与判定逻辑保持一致：把「目录名模糊解析」和「全库定位」的结果也记下来，
        # 否则报告里会显示「目录不存在」，而结论却是「正常」，看起来自相矛盾。
        trace["resolved"] = []
        for root_info in root_infos:
            root = root_info["path"]
            for drop in range(0, min(len(dirs), MAX_TAIL_DEPTH) + 1):
                tail = dirs[drop:]
                actual = self.__resolve_dir(root, tail)
                if actual and self.__has_file(actual, names):
                    trace["resolved"].append({"rel": "/".join(tail), "path": actual})
        trace["show_candidates"] = [
            {"path": item, "hit": self.__has_file(item, names)}
            for item in self.__show_dir_candidates(dirs)
        ]

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
    def __parse_path_map(raw: Any) -> List[Tuple[str, str, str]]:
        """
        解析路径映射规则，长前缀优先。

        每行格式：`记录中的前缀=本地实际前缀|来源标签`（来源标签可省略）。
        例：`/影视=/strm|115`、`/夸克=/夸克|夸克`。
        来源标签决定该记录归属哪套 strm 库，并在结果页展示、按来源统计；
        命中的本地前缀则限定「只在该库里找」，避免跨库误匹配。
        """
        rules: List[Tuple[str, str, str]] = []
        for line in GhostTransferCleaner.__parse_lines(raw):
            if "=" not in line:
                continue
            old, _, rest = line.partition("=")
            new, _, source = rest.partition("|")
            old = old.strip().rstrip("/")
            new = new.strip().rstrip("/")
            source = source.strip()
            if old and new:
                rules.append((old, new, source))
        rules.sort(key=lambda item: len(item[0]), reverse=True)
        return rules

    def __source_of(self, path: Any) -> Tuple[str, str]:
        """
        按映射表判断记录来自哪套库，返回（来源标签, 该库的本地根目录）。

        未命中任何规则时返回 ("", "")，由调用方按「未映射」处理并回退到
        全库后缀匹配（兼容没配映射的情况）。前缀按目录边界匹配，
        避免 /影视 误配 /影视剧 这类前缀包含关系。
        """
        value = str(path or "").strip().replace("\\", "/")
        if not value:
            return "", ""
        for old, new, source in self._path_map:
            if value == old or value.startswith(f"{old}/"):
                return source, new
        return "", ""

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
                if stats.get("sources"):
                    head += "｜来源：" + "、".join(
                        f"{k} {v} 条" for k, v in (stats.get("sources") or {}).items()
                    )
                if stats.get("unverified"):
                    head += (
                        f"｜另有 {stats.get('unverified')} 条未核验（库未挂载）："
                        + "、".join(
                            f"{k} {v} 条"
                            for k, v in (stats.get("unverified_sources") or {}).items()
                        )
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
                    source_tag = ghost.get("source") or SOURCE_NONE
                    lines.append(
                        f"· {label or '未知'} [{ghost.get('level_text', '')}｜{source_tag}]"
                    )
                if stats["ghost"] > 5:
                    lines.append(f"…等共 {stats['ghost']} 条，详见插件数据页面")
            else:
                lines.append("未发现幽灵整理记录，自动整理逻辑正常。")
            if stats.get("suspicious"):
                lines.append("⚠️ 超八成记录都找不到对应 strm，疑似 strm 根目录配置有误，已中止清理，请先核对配置。")
            if stats.get("truncated"):
                lines.append("⚠️ 本次扫描超时提前结束，结果可能不完整。")
            if stats.get("limited"):
                lines.append(
                    f"⚠️ 本次只检查了 {stats.get('scanned', 0)}/{stats.get('total', 0)} 条记录"
                    "（受「单次最多检查记录数」限制），建议在插件配置里把该值设为 0（不限）后重扫。"
                )
            if stats.get("index_truncated"):
                lines.append("⚠️ strm 全库索引建立不完整，兜底匹配可能漏判，结果偏保守。")
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
            self._ghost_csv_path = str(stored.get("ghost_csv_path") or "")
            ghosts = stored.get("ghosts")
            unverified = stored.get("unverified")
            stats = stored.get("stats")
            if isinstance(stats, dict) and stats.get("engine") == ENGINE_VERSION:
                self._ghosts = ghosts if isinstance(ghosts, list) else []
                self._unverified = unverified if isinstance(unverified, list) else []
                self._stats = stats
            else:
                # 旧版本（基于存储层查询）的结论已失效，直接丢弃，避免误导
                self._ghosts = []
                self._unverified = []
                self._stats = {}
                logger.info("幽灵整理记录：历史扫描结果来自旧判定引擎，已丢弃")

    def __save(self):
        try:
            self.save_data(
                "status",
                {
                    "ghosts": self._ghosts,
                    "unverified": self._unverified,
                    "stats": self._stats,
                    "report": self._report,
                    "report_time": self._report_time,
                    "report_path": self._report_path,
                    "ghost_csv_path": self._ghost_csv_path,
                },
            )
        except Exception as err:
            logger.error(f"幽灵整理记录：保存扫描结果失败：{err}")
