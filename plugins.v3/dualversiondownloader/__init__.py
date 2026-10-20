import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.schemas.types import EventType, MediaType, MessageType


class DualVersionDownloader(_PluginBase):
    """
    双版本补全。

    监听下载添加事件：当电影以 4K 版本开始下载时，按同一媒体身份再搜索一个
    1080p 版本并下载到与原任务相同的目录；原任务为 1080p 时不做任何处理。
    """

    # 插件名称
    plugin_name = "双版本补全"
    # 插件描述
    plugin_desc = "电影订阅下载 4K 版本时，自动补一个 1080p 版本到同一目录；下载 1080p 时不做处理"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/movie.png"
    # 插件版本
    plugin_version = "1.0.3"
    # 插件作者
    plugin_author = "yuwancumian"
    # 作者主页
    author_url = "https://github.com/yuwancumian2009/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "DualVersionDownloader_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 4K / 1080p 判定
    _RE_4K = re.compile(r"(?i)(\b4k\b|2160[pi]|x2160|\buhd\b)")
    _RE_1080 = re.compile(r"(?i)(1080p|x1080)")

    def __init__(self) -> None:
        super().__init__()
        self._enabled: bool = False
        self._notify_enabled: bool = True
        self._dry_run: bool = False
        self._only_subscribe: bool = True
        self._target_pix: str = "1080p"
        self._max_size_gb: float = 0.0
        self._min_seeders: int = 0
        self._prefer: str = "seeders"
        self._sites: str = ""
        self._cooldown_days: int = 7
        self._exclude_words: str = "CAM,TS,枪版,TC"
        self._config: Dict[str, Any] = {}
        self._history: Dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: Optional[dict] = None) -> None:
        if isinstance(config, dict) and isinstance(config.get("config"), dict):
            config = config["config"]
        self._config = config or {}

        self._enabled = bool(self._config.get("enabled", False))
        self._notify_enabled = bool(self._config.get("notify", True))
        self._dry_run = bool(self._config.get("dry_run", False))
        self._only_subscribe = bool(self._config.get("only_subscribe", True))
        self._target_pix = str(self._config.get("target_pix") or "1080p").strip()
        self._prefer = str(self._config.get("prefer") or "seeders").strip()
        self._sites = str(self._config.get("sites") or "").strip()
        self._exclude_words = str(self._config.get("exclude_words") or "").strip()

        try:
            self._max_size_gb = float(self._config.get("max_size_gb") or 0)
        except (TypeError, ValueError):
            self._max_size_gb = 0.0
        try:
            self._min_seeders = int(self._config.get("min_seeders") or 0)
        except (TypeError, ValueError):
            self._min_seeders = 0
        try:
            self._cooldown_days = int(self._config.get("cooldown_days") or 0)
        except (TypeError, ValueError):
            self._cooldown_days = 0

        history = self.get_data("history")
        self._history = history if isinstance(history, dict) else {}
        hashes = self.get_data("hashes")
        self._hashes = hashes if isinstance(hashes, dict) else {}
        self._prune_history()

        logger.info(
            f"双版本补全：初始化完成，enabled={self._enabled}，target={self._target_pix}，"
            f"dry_run={self._dry_run}，only_subscribe={self._only_subscribe}，"
            f"max_size_gb={self._max_size_gb}，min_seeders={self._min_seeders}，"
            f"prefer={self._prefer}，cooldown_days={self._cooldown_days}"
        )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        """无需后台服务，保留空实现。"""
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    # ------------------------------------------------------------------
    # 配置与页面
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "notify", "label": "发送通知"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "dry_run", "label": "仅预演不下载"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "only_subscribe", "label": "仅订阅来源触发"},
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
                                            "model": "target_pix",
                                            "label": "补下的分辨率",
                                            "placeholder": "1080p",
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
                                            "model": "max_size_gb",
                                            "label": "候选最大体积(GB，0为不限)",
                                            "placeholder": "0",
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
                                            "model": "min_seeders",
                                            "label": "候选最少做种数(0为不限)",
                                            "placeholder": "0",
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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "prefer",
                                            "label": "候选排序偏好",
                                            "items": [
                                                {"title": "做种多优先", "value": "seeders"},
                                                {"title": "体积小优先", "value": "size"},
                                            ],
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
                                            "model": "cooldown_days",
                                            "label": "同一电影冷却天数(0为不去重)",
                                            "placeholder": "7",
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
                                            "model": "sites",
                                            "label": "限定站点ID(逗号分隔，留空为全部)",
                                            "placeholder": "",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_words",
                                            "label": "排除关键词(逗号分隔)",
                                            "placeholder": "CAM,TS,枪版,TC",
                                        },
                                    }
                                ],
                            }
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
                                            "text": (
                                                "仅在「电影」下载的是 4K 版本时触发；补下的资源会下载到与 4K 任务"
                                                "相同的目录，整理时按命名规则自动区分版本。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "dry_run": False,
            "only_subscribe": True,
            "target_pix": "1080p",
            "max_size_gb": 0,
            "min_seeders": 0,
            "prefer": "seeders",
            "cooldown_days": 7,
            "sites": "",
            "exclude_words": "CAM,TS,枪版,TC",
        }

    def get_page(self) -> List[dict]:
        records = sorted(
            (self._history or {}).items(), key=lambda kv: kv[1], reverse=True
        )[:20]
        lines = [f"{ts[:19].replace('T', ' ')}  {key}" for key, ts in records]
        return [
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
                                    "text": (
                                        f"状态：{'已启用' if self._enabled else '未启用'}　"
                                        f"补下分辨率：{self._target_pix}　"
                                        f"预演模式：{'开' if self._dry_run else '关'}　"
                                        f"已处理：{len(self._history or {})} 部"
                                    ),
                                },
                            }
                        ],
                    }
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
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {"component": "VCardTitle", "text": "最近补全记录"},
                                    {
                                        "component": "VCardText",
                                        "text": "\n".join(lines) if lines else "暂无记录",
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        ]

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    @eventmanager.register(EventType.DownloadAdded)
    def on_download_added(self, event: Event) -> None:
        """下载任务添加后判定：仅电影、仅 4K、且非本插件自己补下的任务。"""
        if not self._enabled:
            return
        try:
            data = event.event_data or {}
            context = data.get("context")
            if not context:
                return

            mediainfo = getattr(context, "media_info", None)
            meta = getattr(context, "meta_info", None)
            torrent = getattr(context, "torrent_info", None)
            if not mediainfo or not meta:
                return

            media_type = getattr(getattr(mediainfo, "type", None), "value", getattr(mediainfo, "type", None))
            if media_type != MediaType.MOVIE.value:
                return

            source = str(data.get("source") or "")
            if self._only_subscribe and not source.startswith("Subscribe|"):
                logger.debug(f"双版本补全：跳过非订阅来源的下载（source={source or '未知'}）")
                return

            text = " ".join(
                str(x) for x in (
                    getattr(torrent, "title", "") or "",
                    getattr(meta, "resource_pix", "") or "",
                    getattr(meta, "org_string", "") or "",
                ) if x
            )
            if not self._RE_4K.search(text):
                return
            if self._RE_1080.search(text):
                # 标题里同时出现 1080 与 4K（如合集/多版本包）时不动，避免误判
                logger.debug(f"双版本补全：标题同时命中 1080p，跳过：{text}")
                return

            media_key = self._media_key(mediainfo)
            if not media_key:
                return
            download_hash = str(data.get("hash") or "").lower()
            if self._in_cooldown(media_key):
                logger.debug(f"双版本补全：{media_key} 在冷却期内，跳过")
                return

            with self._lock:
                # 同一个种子只处理一次，避免事件重复投递；不同种子仍按冷却期判定
                if download_hash and download_hash in self._hashes:
                    logger.debug(f"双版本补全：种子 {download_hash[:12]} 已处理过，跳过")
                    return
                now = datetime.now().isoformat(timespec="seconds")
                if download_hash:
                    self._hashes[download_hash] = now
                self._history[media_key] = now
                self._save_state()

            threading.Thread(
                target=self._complete,
                args=(media_key, context, data),
                name="DualVersionComplete",
                daemon=True,
            ).start()
        except Exception as err:  # noqa: BLE001
            logger.error(f"双版本补全：处理下载事件出错：{err}", exc_info=True)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _complete(self, media_key: str, context: Any, data: Dict[str, Any]) -> None:
        """搜索并补下目标分辨率版本。"""
        mediainfo = context.media_info
        downloader = data.get("downloader")
        download_hash = data.get("hash")
        username = data.get("username")
        title = f"{getattr(mediainfo, 'title', '') or ''} ({getattr(mediainfo, 'year', '') or ''})"
        try:
            save_path = self._resolve_save_path(download_hash, downloader)
            candidates = self._search_candidates(mediainfo)
            if not candidates:
                logger.info(f"双版本补全：{title} 未找到可用的 {self._target_pix} 候选")
                self._notify(f"{title} 未找到可用的 {self._target_pix} 版本", ok=False)
                return

            best = candidates[0]
            best_title = getattr(getattr(best, "torrent_info", None), "title", "") or ""
            best_site = getattr(getattr(best, "torrent_info", None), "site_name", "") or ""
            size_gb = round((getattr(best.torrent_info, "size", 0) or 0) / 1024 ** 3, 2)

            if self._dry_run:
                logger.info(
                    f"双版本补全[预演]：{title} 将补下 {self._target_pix}：{best_title}"
                    f"（{best_site}，{size_gb}GB，保存目录 {save_path or '默认'}）"
                )
                self._notify(f"{title} 预演命中：{best_title}（{size_gb}GB），未实际下载", ok=True)
                return

            result = DownloadChain().download_single(
                context=best,
                save_path=save_path,
                downloader=downloader,
                username=username,
                source="Plugin|双版本补全",
            )
            if result:
                logger.info(
                    f"双版本补全：{title} 已补下 {self._target_pix}：{best_title}"
                    f"（{best_site}，{size_gb}GB，hash={result}）"
                )
                self._notify(f"{title} 已补下 {self._target_pix}：{best_title}（{size_gb}GB）", ok=True)
            else:
                logger.warn(f"双版本补全：{title} 补下失败（下载器未接受任务）：{best_title}")
                self._notify(f"{title} 补下失败：{best_title}", ok=False)
        except Exception as err:  # noqa: BLE001
            logger.error(f"双版本补全：{title} 补下过程出错：{err}", exc_info=True)
            self._notify(f"{title} 补下出错：{err}", ok=False)
        finally:
            with self._lock:
                self._history[media_key] = datetime.now().isoformat(timespec="seconds")
                self._save_state()

    def _search_candidates(self, mediainfo: Any) -> List[Any]:
        """按媒体身份搜索，过滤出目标分辨率的候选并排序。"""
        sites = None
        if self._sites:
            try:
                sites = [int(x) for x in re.split(r"[,\s]+", self._sites) if x.strip()]
            except ValueError:
                sites = None

        contexts = SearchChain().process(mediainfo=mediainfo, sites=sites) or []
        logger.info(f"双版本补全：搜索到 {len(contexts)} 个候选资源")
        return self._filter_candidates(contexts)

    def _filter_candidates(self, contexts: List[Any]) -> List[Any]:
        """从搜索结果里挑出目标分辨率的候选并排序（纯函数，便于自测）。"""
        excludes = [w.strip().lower() for w in re.split(r"[,\s]+", self._exclude_words) if w.strip()]
        picked: List[Any] = []
        for item in contexts:
            torrent = getattr(item, "torrent_info", None)
            meta = getattr(item, "meta_info", None)
            if not torrent:
                continue
            text = " ".join(
                str(x) for x in (
                    getattr(torrent, "title", "") or "",
                    getattr(meta, "resource_pix", "") or "",
                ) if x
            )
            if self._RE_4K.search(text) or not self._RE_1080.search(text):
                continue
            if any(word in text.lower() for word in excludes):
                continue
            size = getattr(torrent, "size", 0) or 0
            if self._max_size_gb > 0 and size > self._max_size_gb * 1024 ** 3:
                continue
            if self._min_seeders > 0 and (getattr(torrent, "seeders", 0) or 0) < self._min_seeders:
                continue
            picked.append(item)

        if self._prefer == "size":
            picked.sort(key=lambda c: (getattr(c.torrent_info, "size", 0) or 0,
                                       -(getattr(c.torrent_info, "seeders", 0) or 0)))
        else:
            picked.sort(key=lambda c: (-(getattr(c.torrent_info, "seeders", 0) or 0),
                                       getattr(c.torrent_info, "size", 0) or 0))
        return picked

    def _resolve_save_path(self, download_hash: Optional[str], downloader: Optional[str]) -> Optional[str]:
        """读取原 4K 任务的保存目录，保证两版落在同一文件夹。"""
        if not download_hash:
            return None
        try:
            torrents = DownloadChain().list_torrents(hashs=download_hash, downloader=downloader) or []
        except Exception as err:  # noqa: BLE001
            logger.warn(f"双版本补全：读取原任务保存路径失败：{err}")
            return None
        for item in torrents:
            path = getattr(item, "save_path", None) or getattr(item, "path", None)
            if path:
                return str(path)
        logger.info("双版本补全：未能读取原任务保存路径，将使用默认下载目录")
        return None

    def _media_key(self, mediainfo: Any) -> Optional[str]:
        source = getattr(mediainfo, "media_source", None)
        media_id = getattr(mediainfo, "media_id", None)
        if source and media_id:
            return f"{getattr(source, 'value', source)}:{media_id}"
        tmdb_id = getattr(mediainfo, "tmdb_id", None)
        if tmdb_id:
            return f"tmdb:{tmdb_id}"
        return None

    def _in_cooldown(self, media_key: str) -> bool:
        if self._cooldown_days <= 0:
            return False
        last = (self._history or {}).get(media_key)
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return False
        return datetime.now() - last_dt < timedelta(days=self._cooldown_days)

    def _prune_history(self) -> None:
        """冷却表与种子去重表都只保留最近 90 天、各自最多 500 条。"""
        limit = datetime.now() - timedelta(days=90)

        def _clean(raw: Any) -> Dict[str, str]:
            if not isinstance(raw, dict):
                return {}
            cleaned: Dict[str, str] = {}
            for key, value in raw.items():
                try:
                    if datetime.fromisoformat(str(value)) >= limit:
                        cleaned[str(key)] = str(value)
                except ValueError:
                    continue
            if len(cleaned) > 500:
                newest = sorted(cleaned.items(), key=lambda kv: kv[1], reverse=True)[:500]
                cleaned = dict(newest)
            return cleaned

        self._history = _clean(self._history)
        self._hashes = _clean(getattr(self, "_hashes", {}))

    def _save_state(self) -> None:
        """把冷却表与种子去重表落盘（调用方需自行持有锁）。"""
        self._prune_history()
        self.save_data("history", self._history)
        self.save_data("hashes", self._hashes)

    def _notify(self, text: str, ok: bool = True) -> None:
        if not self._notify_enabled:
            return
        try:
            self.post_message(
                mtype=MessageType.Plugin,
                title="双版本补全",
                text=("✅ " if ok else "⚠️ ") + text,
            )
        except Exception as err:  # noqa: BLE001
            logger.warn(f"双版本补全：发送通知失败：{err}")
