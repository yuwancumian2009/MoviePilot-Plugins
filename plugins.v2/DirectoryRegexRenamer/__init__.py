# 基础库
import datetime
import os
import re
import threading
import traceback
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver # For compatibility on some systems

# 项目库
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
# from app.schemas.types import SystemConfigKey # Not strictly needed for this plugin but good for reference
# from app.core.event import eventmanager, Event # Not needed for basic functionality

# Global lock for file operations if needed, though renaming individual files might be less prone to race conditions
# than softlinking. For simplicity, we'll start without a global lock for renaming,
# but keep it in mind if issues arise with rapid file creation.
# rename_lock = threading.Lock()

class RenameFileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类，用于文件重命名
    """

    def __init__(self, plugin_instance: Any, monitor_path: str, **kwargs):
        super().__init__(**kwargs)
        self._plugin = plugin_instance
        self._monitor_path = monitor_path
        logger.debug(f"RenameFileMonitorHandler initialized for path: {monitor_path}")

    def on_created(self, event: FileCreatedEvent):
        if not event.is_directory:
            logger.debug(f"File created: {event.src_path} in monitored_path: {self._monitor_path}")
            # Ensure the event path is within the specific monitored path this handler is for.
            # This is more of a safeguard, as watchdog should trigger per watch.
            if Path(event.src_path).parent.resolve() == Path(self._monitor_path).resolve() or \
               Path(event.src_path).resolve() == Path(self._monitor_path).resolve(): # if file created directly in mon path
                self._plugin.schedule_rename(event.src_path)


    def on_moved(self, event: FileMovedEvent):
        if not event.is_directory:
            logger.debug(f"File moved: from {event.src_path} to {event.dest_path} in {self._monitor_path}")
            # Process if the destination is within the monitored path
            if Path(event.dest_path).parent.resolve() == Path(self._monitor_path).resolve() or \
               Path(event.dest_path).resolve() == Path(self._monitor_path).resolve():
                 self._plugin.schedule_rename(event.dest_path)

class DirectoryRegexRenamer(_PluginBase):
    # 插件名称
    plugin_name = "目录文件正则重命名"
    # 插件描述
    plugin_desc = "监控指定目录中的新增文件，并根据自定义正则表达式规则进行重命名。"
    # 插件图标 (using a generic gear icon for now)
    plugin_icon = "https://github.com/yuwancumian2009/MoviePilot-Plugins/blob/main/icons/Filebrowser_A.png"
    # 插件版本
    plugin_version = "0.1.0"
    # 插件作者
    plugin_author = "yuwan" # Or your name
    # 作者主页
    author_url = "https://github.com/yuwancumian2009" # Or your repo
    # 插件配置项ID前缀
    plugin_config_prefix = "directoryregexrenamer_"
    # 加载顺序
    plugin_order = 40 # Arbitrary, adjust as needed
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler: Optional[BackgroundScheduler] = None
    _observers: List[Observer] = [] # Store multiple observers if multiple dirs

    # 配置属性
    _enabled: bool = False
    _onlyonce: bool = False
    _monitor_dirs_str: str = "" # Raw string from config
    _rename_rules_str: str = "" # Raw string from config
    _exclude_patterns_str: str = "" # Raw string for exclusion
    _monitor_mode: str = "fast" # "fast" or "compatibility"

    # Parsed configurations
    _monitor_paths: List[Path] = []
    _rename_rules: List[Tuple[re.Pattern, str]] = []
    _exclude_patterns: List[re.Pattern] = []

    _event_thread_lock = threading.Lock() # Lock for scheduling renames to avoid race conditions
    _rename_queue = []
    _rename_worker_thread = None
    _stop_worker_event = threading.Event()


    def init_plugin(self, config: dict = None):
        logger.info(f"Initializing {self.plugin_name} v{self.plugin_version}")
        self._load_config(config)

        # Stop any existing services before reinitializing
        self.stop_service()

        if self._enabled or self._onlyonce:
            self._parse_configs()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._onlyonce:
                logger.info(f"{self.plugin_name}: Queued for immediate run (once).")
                self._scheduler.add_job(
                    self._process_all_monitored_dirs,
                    'date',
                    run_date=datetime.datetime.now(pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                    name="目录正则重命名_单次运行"
                )
                self._onlyonce = False
                # Update config to turn off the 'onlyonce' flag for next load
                current_config = self.get_config() # Assuming _PluginBase has get_config
                if current_config:
                    current_config['onlyonce'] = False
                    self.update_config(config=current_config)
                else: # Fallback if get_config isn't available or robust
                    config_to_save = self._build_config_dict()
                    config_to_save['onlyonce'] = False
                    self.update_config(config=config_to_save)


            if self._enabled and self._monitor_paths:
                logger.info(f"{self.plugin_name}: Real-time monitoring enabled for {len(self._monitor_paths)} director(y/ies).")
                for path_obj in self._monitor_paths:
                    self._start_dir_monitor(path_obj)
                # Start rename worker thread
                self._stop_worker_event.clear()
                self._rename_worker_thread = threading.Thread(target=self._rename_processor, daemon=True)
                self._rename_worker_thread.start()


            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()
            else:
                logger.info(f"{self.plugin_name}: No jobs scheduled (other than potential 'run once').")

        else:
            logger.info(f"{self.plugin_name}: Plugin is not enabled and not set to run once.")

    def _load_config(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._monitor_dirs_str = config.get("monitor_dirs_str", "")
            self._rename_rules_str = config.get("rename_rules_str", "")
            self._exclude_patterns_str = config.get("exclude_patterns_str", "")
            self._monitor_mode = config.get("monitor_mode", "fast")
        logger.debug(f"Config loaded: enabled={self._enabled}, onlyonce={self._onlyonce}, mode={self._monitor_mode}")
        logger.debug(f"Monitor Dirs Raw: {self._monitor_dirs_str}")
        logger.debug(f"Rename Rules Raw: {self._rename_rules_str}")
        logger.debug(f"Exclude Patterns Raw: {self._exclude_patterns_str}")


    def _parse_configs(self):
        # Parse monitored directories
        self._monitor_paths = []
        if self._monitor_dirs_str:
            for line in self._monitor_dirs_str.strip().splitlines():
                path_str = line.strip()
                if path_str and os.path.isdir(path_str):
                    self._monitor_paths.append(Path(path_str).resolve())
                elif path_str:
                    logger.warning(f"Configured monitor path is not a valid directory or does not exist: {path_str}")
        logger.info(f"Parsed {len(self._monitor_paths)} valid monitor paths.")

        # Parse renaming rules
        self._rename_rules = []
        if self._rename_rules_str:
            for line in self._rename_rules_str.strip().splitlines():
                rule_parts = line.split('|', 1)
                if len(rule_parts) == 2:
                    pattern_str, replacement_str = rule_parts[0].strip(), rule_parts[1] # Keep replacement as is initially
                    if pattern_str:
                        try:
                            self._rename_rules.append((re.compile(pattern_str), replacement_str))
                        except re.error as e:
                            logger.error(f"Invalid regex pattern '{pattern_str}': {e}")
                    else:
                        logger.warning(f"Empty regex pattern in rule: {line}")
                elif line.strip(): # Non-empty line that doesn't match format
                    logger.warning(f"Invalid rename rule format (expected 'pattern|replacement'): {line}")
        logger.info(f"Parsed {len(self._rename_rules)} rename rules.")

        # Parse exclusion patterns
        self._exclude_patterns = []
        if self._exclude_patterns_str:
            for line in self._exclude_patterns_str.strip().splitlines():
                pattern_str = line.strip()
                if pattern_str:
                    try:
                        self._exclude_patterns.append(re.compile(pattern_str))
                    except re.error as e:
                        logger.error(f"Invalid exclusion regex pattern '{pattern_str}': {e}")
        logger.info(f"Parsed {len(self._exclude_patterns)} exclusion patterns.")

    def _start_dir_monitor(self, path_to_monitor: Path):
        if not path_to_monitor.is_dir():
            logger.error(f"Cannot monitor non-existent or non-directory path: {path_to_monitor}")
            return

        logger.info(f"Starting monitor for: {path_to_monitor} with mode: {self._monitor_mode}")
        event_handler = RenameFileMonitorHandler(plugin_instance=self, monitor_path=str(path_to_monitor))

        if self._monitor_mode == "compatibility":
            observer = PollingObserver(timeout=10)
        else: # fast mode (default)
            observer = Observer(timeout=10)
        
        try:
            observer.schedule(event_handler, str(path_to_monitor), recursive=False) # Monitor only top level of specified dir
            observer.daemon = True
            observer.start()
            self._observers.append(observer)
            logger.info(f"Successfully started monitoring {path_to_monitor}")
        except Exception as e:
            err_msg = str(e)
            if "inotify" in err_msg.lower() and ("reached" in err_msg.lower() or "instances" in err_msg.lower()):
                logger.warn(
                    f"Inotify limit reached for {path_to_monitor}. Monitoring might be unreliable. "
                    "Try increasing inotify limits on the host system. Error: {err_msg}"
                )
            else:
                logger.error(f"Failed to start monitor for {path_to_monitor}: {err_msg}", exc_info=True)
            # Optionally, send a system message if self.systemmessage is available
            # self.systemmessage.put(f"Failed to start file monitor for {path_to_monitor}: {err_msg}")


    def schedule_rename(self, file_path_str: str):
        with self._event_thread_lock:
            self._rename_queue.append(file_path_str)
            logger.debug(f"Added to rename queue: {file_path_str}. Queue size: {len(self._rename_queue)}")

    def _rename_processor(self):
        logger.info("Rename processor thread started.")
        while not self._stop_worker_event.is_set():
            file_to_process = None
            with self._event_thread_lock:
                if self._rename_queue:
                    file_to_process = self._rename_queue.pop(0)
            
            if file_to_process:
                logger.debug(f"Processing from queue: {file_to_process}")
                # Add a small delay to ensure file is fully written, especially for larger files or network shares
                time.sleep(1) # Adjust as necessary
                self._apply_rename_rules_to_file(Path(file_to_process))
            else:
                # Sleep briefly if queue is empty
                time.sleep(0.1)
        logger.info("Rename processor thread stopped.")


    def _apply_rename_rules_to_file(self, file_path: Path):
        if not file_path.exists() or not file_path.is_file():
            logger.debug(f"File no longer exists or is not a file, skipping rename: {file_path}")
            return

        original_name = file_path.name
        current_name = original_name
        file_path_str = str(file_path.resolve())

        # Check exclusions first
        for exclude_pattern in self._exclude_patterns:
            if exclude_pattern.search(file_path_str) or exclude_pattern.search(original_name):
                logger.info(f"File '{original_name}' at '{file_path.parent}' matches exclusion pattern '{exclude_pattern.pattern}', skipping.")
                return
        
        if not self._rename_rules:
            logger.debug(f"No rename rules defined. Skipping rename for {original_name}")
            return

        logger.debug(f"Applying rename rules to: {original_name} at {file_path.parent}")

        for pattern, replacement_str in self._rename_rules:
            try:
                # Handle backreferences in replacement string correctly
                # re.sub does this automatically
                current_name, num_subs = pattern.subn(replacement_str, current_name)
                if num_subs > 0:
                    logger.debug(f"Rule '{pattern.pattern}' -> '{replacement_str}' changed name to '{current_name}'")
            except re.error as e:
                logger.error(f"Regex error during substitution for pattern '{pattern.pattern}': {e}")
                # Potentially skip this rule or stop processing this file
                continue # Skip to next rule

        if current_name != original_name:
            # Sanitize new name (remove invalid characters for filenames, if any - OS dependent)
            # For simplicity, we assume regex replacement produces valid names.
            # More robust sanitization could be added here.
            # e.g. current_name = "".join(c for c in current_name if c not in r'<>:"/\|?*')

            if not current_name:
                logger.warning(f"New name for '{original_name}' became empty after applying rules. Skipping rename.")
                return

            new_file_path = file_path.parent / current_name
            if new_file_path.exists():
                logger.warning(f"Target file '{new_file_path}' already exists. Skipping rename for '{original_name}'.")
                return
            try:
                file_path.rename(new_file_path)
                logger.info(f"Successfully renamed '{original_name}' to '{current_name}' in '{file_path.parent}'")
            except OSError as e:
                logger.error(f"Failed to rename '{original_name}' to '{current_name}': {e}", exc_info=True)
        else:
            logger.debug(f"No rules changed the name for: {original_name}")

    def _process_all_monitored_dirs(self):
        logger.info("Starting one-time processing of all monitored directories.")
        if not self._monitor_paths:
            logger.info("No directories configured for monitoring.")
            return

        for dir_path in self._monitor_paths:
            if not dir_path.is_dir():
                logger.warning(f"Monitored path {dir_path} is not a directory. Skipping.")
                continue
            logger.info(f"Scanning directory for run-once: {dir_path}")
            try:
                for item in dir_path.iterdir(): # iterdir is non-recursive
                    if item.is_file():
                        self._apply_rename_rules_to_file(item)
                        time.sleep(0.05) # Small delay to be nice to the system
            except Exception as e:
                logger.error(f"Error scanning directory {dir_path}: {e}", exc_info=True)
        logger.info("One-time processing finished.")


    def stop_service(self):
        logger.info(f"Stopping {self.plugin_name} services...")
        if self._observers:
            for observer in self._observers:
                try:
                    if observer.is_alive():
                        observer.stop()
                        observer.join(timeout=5) # Wait for observer to stop
                    logger.debug(f"Observer for {observer.name if hasattr(observer, 'name') else 'unknown path'} stopped.")
                except Exception as e:
                    logger.error(f"Error stopping observer: {e}", exc_info=True)
            self._observers = []

        if self._scheduler and self._scheduler.running:
            try:
                self._scheduler.shutdown(wait=False)
                logger.debug("Scheduler shut down.")
            except Exception as e:
                logger.error(f"Error shutting down scheduler: {e}", exc_info=True)
        self._scheduler = None
        
        # Signal and stop rename worker thread
        self._stop_worker_event.set()
        if self._rename_worker_thread and self._rename_worker_thread.is_alive():
            self._rename_worker_thread.join(timeout=5)
        self._rename_worker_thread = None
        self._rename_queue = [] # Clear queue

        logger.info(f"{self.plugin_name} services stopped.")

    def get_state(self) -> bool:
        return self._enabled

    def _build_config_dict(self):
        """Helper to build a dictionary of current config values for saving."""
        return {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce, # This should be false if run_once just completed
            "monitor_dirs_str": self._monitor_dirs_str,
            "rename_rules_str": self._rename_rules_str,
            "exclude_patterns_str": self._exclude_patterns_str,
            "monitor_mode": self._monitor_mode,
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        form_structure = [
            {
                'component': 'VForm',
                'content': [
                    # Row 1: Switches
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'enabled', 'label': '启用插件'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSwitch',
                                    'props': {'model': 'onlyonce', 'label': '立即运行一次'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSelect',
                                    'props': {
                                        'model': 'monitor_mode',
                                        'label': '监控模式',
                                        'items': [
                                            {'title': '性能模式 (默认)', 'value': 'fast'},
                                            {'title': '兼容模式 (用于网络共享等)', 'value': 'compatibility'}
                                        ]
                                    }
                                }]
                            }
                        ]
                    },
                    # Row 2: Monitor Directories
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VTextarea',
                                'props': {
                                    'model': 'monitor_dirs_str',
                                    'label': '监控目录 (每行一个)',
                                    'placeholder': '/path/to/your/downloads\n/another/path/to/monitor',
                                    'rows': 3,
                                    'auto-grow': True,
                                    'clearable': True
                                }
                            }]
                        }]
                    },
                    # Row 3: Rename Rules
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VTextarea',
                                'props': {
                                    'model': 'rename_rules_str',
                                    'label': '重命名规则 (正则表达式, 每行一条: 查找模式|替换字符串)',
                                    'placeholder': '^\[.*?\] (.*) - (S[0-9]{2}E[0-9]{2}).*(\\..*)$|$1 - $2$3\n(.*)\\.sample(\\..*)$|$1$2',
                                    'rows': 5,
                                    'auto-grow': True,
                                    'clearable': True,
                                    'hint': '示例: (.*)_old(\\..*)|$1_new$2 (将文件名中的_old替换为_new)',
                                    'persistent-hint': True
                                }
                            }]
                        }]
                    },
                    # Row 4: Exclude Patterns
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VTextarea',
                                'props': {
                                    'model': 'exclude_patterns_str',
                                    'label': '排除模式 (正则表达式, 每行一条, 匹配完整路径或文件名)',
                                    'placeholder': '\\.part$\n^\\._.*',
                                    'rows': 3,
                                    'auto-grow': True,
                                    'clearable': True,
                                    'hint': '示例: ^hidden_folder/.* (排除hidden_folder下的所有内容)',
                                    'persistent-hint': True
                                }
                            }]
                        }]
                    },
                     # Row 5: Info Alert
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol',
                            'props': {'cols': 12},
                            'content': [{
                                'component': 'VAlert',
                                'props': {
                                    'type': 'info',
                                    'variant': 'tonal', # or 'outlined' or 'elevated' etc.
                                    'text': '说明:\n- 监控目录: 只监控指定目录的第一层新增文件，不递归子目录。\n- 立即运行一次: 会扫描所有已配置监控目录的第一层文件并应用规则。\n- 替换字符串中可使用 $1, $2 等反向引用匹配的分组。\n- 排除模式会检查文件的完整路径和单独的文件名。'
                                }
                            }]
                        }]
                    }
                ]
            }
        ]
        default_values = {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce, # Should be False for default display
            "monitor_dirs_str": self._monitor_dirs_str,
            "rename_rules_str": self._rename_rules_str,
            "exclude_patterns_str": self._exclude_patterns_str,
            "monitor_mode": self._monitor_mode
        }
        return form_structure, default_values

    # --- Other standard _PluginBase methods (get_api, get_command, get_service, get_page) ---
    # For this plugin, these are likely not needed or would be minimal.

    def get_api(self) -> List[Dict[str, Any]]:
        # No specific APIs exposed by this plugin for now
        return []

    def get_command(self) -> List[Dict[str, Any]]:
        # No specific commands exposed by this plugin for now
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        # No background services (like CRON jobs) beyond the watchdog monitor itself
        return []

    def get_page(self) -> List[dict]:
        # No custom page for this plugin
        return []
