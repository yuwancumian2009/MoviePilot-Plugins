# 基础库
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
import re
from typing import Any, Dict, List, Optional, Union
import pytz

# 第三方库
from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 项目库 (假设这些是项目标准导入)
from app.plugins import _PluginBase # 假设 _PluginBase 是这样导入的
from app.log import logger # 假设 logger 是这样导入的
from app.core.config import settings # 假设 settings 是这样导入的
# from app.db.systemconfig_oper import SystemConfigOper # 不需要，因为不列出下载器
# from app.helper.downloader import DownloaderHelper # 不需要，不涉及下载器
# from app.schemas.types import SystemConfigKey # 不需要


# Watchdog Event Handler
class FileChangeHandler(FileSystemEventHandler):
    """Handles file system events."""
    def __init__(self, plugin_instance: '_BatchFileRename'):
        self.plugin = plugin_instance
        super().__init__()

    def on_created(self, event):
        """Called when a file or directory is created."""
        if not event.is_directory:
            logger.info(f"[{self.plugin.plugin_name}] New file detected: {event.src_path}")
            self.plugin.process_file(event.src_path)

@dataclass
class BatchFileRenameConfig:
    """Configuration specific to BatchFileRename plugin."""
    enabled: bool = False
    monitor_path: str = ""
    rename_rule: str = "" # Format: regex_pattern|replacement_string
    run_once: bool = False
    process_existing_on_run_once: bool = True


class BatchFileRename(_PluginBase):
    # 插件名称
    plugin_name = "文件批量重命名监控"
    # 插件描述
    plugin_desc = "监控指定目录的新增文件，并根据自定义正则表达式进行重命名。也支持对目录内现有文件执行一次重命名操作。"
    # 插件图标 (using one from the example's source for consistency)
    plugin_icon = "https://github.com/yuwancumian2009/MoviePilot-Plugins/blob/main/icons/Filebrowser_A.png"
    # 插件版本
    plugin_version = "0.1.0"
    # 插件作者
    plugin_author = "yuwan" # Or your name
    # 作者主页
    author_url = "https://github.com/yuwancumian2009" # Or your github
    # 插件配置项ID前缀
    plugin_config_prefix = "batchfilerename_" # Ensure this is unique
    # 加载顺序
    plugin_order = 40 # Choose an appropriate order
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler: Optional[BackgroundScheduler] = None
    _observer: Optional[Observer] = None
    _event_handler: Optional[FileChangeHandler] = None

    # 配置属性 (with defaults)
    _enabled: bool = False
    _monitor_path: str = ""
    _rename_rule: str = ""
    _run_once: bool = False # This is a trigger, UI sets it, init_plugin consumes it
    _process_existing_on_run_once: bool = True


    def init_plugin(self, config: Optional[Dict[str, Any]] = None):
        """Initializes the plugin with given configuration."""
        if config is None:
            config = {}

        # Load configuration from the provided dict
        self.load_config(config)

        # Stop any existing services (scheduler, observer) before reinitializing
        self.stop_service()

        config_to_save_if_run_once_triggered = config.copy() # Prepare for potential update

        # Handle 'run_once' functionality
        if self._run_once:
            logger.info(f"[{self.plugin_name}] 'Run Once' triggered.")
            # Reset the run_once flag in the configuration to be saved
            config_to_save_if_run_once_triggered['run_once'] = False
            self.update_config(config=config_to_save_if_run_once_triggered) # Persist the cleared flag

            # Schedule the one-time task
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(self.rename_files_in_directory, 'date',
                                    run_date=datetime.now(pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    args=[self._process_existing_on_run_once],
                                    name=f"{self.plugin_name}_RunOnce")
            logger.info(f"[{self.plugin_name}] Scheduled one-time job: {self.plugin_name}_RunOnce")
            self._scheduler.print_jobs()
            self._scheduler.start()
            # self._run_once remains True for this instance to indicate it was triggered,
            # but the saved config now has it as False.

        # Start file monitoring if enabled
        if self._enabled:
            if self._monitor_path and os.path.isdir(self._monitor_path):
                self.start_monitoring()
            elif self._monitor_path: # Path specified but not valid
                logger.error(f"[{self.plugin_name}] Monitor path '{self._monitor_path}' is not a valid directory. Monitoring disabled.")
                self._enabled = False # Disable if path is bad, to prevent errors
            else: # No path specified
                logger.info(f"[{self.plugin_name}] Monitoring path not configured. Monitoring disabled.")
                self._enabled = False

    def load_config(self, config: Dict[str, Any]):
        """Loads configuration into plugin attributes."""
        self._enabled = config.get('enabled', False)
        raw_monitor_path = config.get('monitor_path', "")
        if raw_monitor_path:
            self._monitor_path = os.path.normpath(os.path.expanduser(raw_monitor_path))
        else:
            self._monitor_path = ""
        self._rename_rule = config.get('rename_rule', "")
        self._run_once = config.get('run_once', False) # This loads the trigger state
        self._process_existing_on_run_once = config.get('process_existing_on_run_once', True)

        logger.debug(f"[{self.plugin_name}] Config loaded: enabled={self._enabled}, path='{self._monitor_path}', "
                    f"rule='{self._rename_rule}', run_once={self._run_once}, "
                    f"proc_exist_on_run_once={self._process_existing_on_run_once}")


    def get_form(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Returns the form definition for the plugin configuration UI."""
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用实时文件监控',
                                            'hint': '开启后，插件将监控指定目录中的新增文件并自动重命名。',
                                            'persistent-hint': True,
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'run_once',
                                            'label': '立即运行一次重命名',
                                            'hint': '对监控目录中的现有文件（如果下方开关开启）执行一次重命名操作。执行后此开关会自动关闭。',
                                            'persistent-hint': True,
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'monitor_path',
                                            'label': '监控目录路径',
                                            'placeholder': '/path/to/your/watch/folder',
                                            'hint': '请输入需要监控的文件夹的绝对路径。',
                                            'persistent-hint': True,
                                            'clearable': True,
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'rows': 3,
                                            'auto-grow': True,
                                            'model': 'rename_rule',
                                            'label': '重命名规则 (正则表达式)',
                                            'placeholder': '正则表达式|替换格式\n例如: (.*)_old(\\..*)|\\1_new\\2\n留空则不进行重命名。',
                                            'hint': '输入一行规则，格式为“正则表达式|替换格式”。使用\\1, \\2等或命名捕获组如\\g<name>引用。',
                                            'persistent-hint': True,
                                            'clearable': True,
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 9}, # Adjusted width
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'process_existing_on_run_once',
                                            'label': '“立即运行一次”时处理目录中所有现有文件',
                                            'hint': '如果关闭，“立即运行一次”将不处理已存在文件。通常应开启。',
                                            'persistent-hint': True,
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], { # Default values for the form fields when plugin is first added
            "enabled": False,
            "monitor_path": "",
            "rename_rule": "",
            "run_once": False,
            "process_existing_on_run_once": True,
        }

    def stop_service(self):
        """Stops all running services (scheduler and watchdog observer)."""
        # Stop APScheduler
        if self._scheduler:
            try:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
                logger.info(f"[{self.plugin_name}] Scheduler stopped.")
            except Exception as e:
                logger.error(f"[{self.plugin_name}] Error stopping scheduler: {e}", exc_info=True)
            finally:
                self._scheduler = None

        # Stop Watchdog Observer
        self.stop_monitoring()

    def start_monitoring(self):
        """Starts the file system monitor."""
        if not self._enabled:
            logger.info(f"[{self.plugin_name}] Monitoring is disabled. Cannot start.")
            return
        if not self._monitor_path:
            logger.warn(f"[{self.plugin_name}] Monitor path is not set. Cannot start monitoring.")
            return
        if not os.path.isdir(self._monitor_path):
            logger.error(f"[{self.plugin_name}] Monitor path '{self._monitor_path}' is not a valid directory. Cannot start monitoring.")
            return

        if self._observer and self._observer.is_alive():
            logger.info(f"[{self.plugin_name}] Monitor already running. Stopping first.")
            self.stop_monitoring() # Ensure any old observer is stopped

        self._event_handler = FileChangeHandler(self)
        self._observer = Observer()
        try:
            self._observer.schedule(self._event_handler, self._monitor_path, recursive=False) # Monitor only top-level
            self._observer.start()
            logger.info(f"[{self.plugin_name}] Started monitoring directory: {self._monitor_path}")
        except Exception as e:
            logger.error(f"[{self.plugin_name}] Failed to start monitoring on {self._monitor_path}: {e}", exc_info=True)
            self._observer = None # Ensure observer is None if start failed

    def stop_monitoring(self):
        """Stops the file system monitor."""
        if self._observer:
            try:
                if self._observer.is_alive():
                    self._observer.stop()
                    self._observer.join(timeout=5) # Wait for thread to terminate
                logger.info(f"[{self.plugin_name}] File monitoring stopped.")
            except Exception as e:
                logger.error(f"[{self.plugin_name}] Error stopping file monitor: {e}", exc_info=True)
            finally:
                self._observer = None
                self._event_handler = None

    def process_file(self, filepath: str):
        """
        Renames a single file based on the configured regex rule.
        """
        if not self._rename_rule:
            logger.debug(f"[{self.plugin_name}] No rename rule configured. Skipping file: {filepath}")
            return

        try:
            parts = self._rename_rule.split("|", 1)
            if len(parts) != 2:
                logger.error(f"[{self.plugin_name}] Invalid rename rule format: '{self._rename_rule}'. Expected 'regex_pattern|replacement_format'. Skipping: {filepath}")
                return

            regex_pattern, replacement_format = parts
            if not regex_pattern.strip(): # Check if regex pattern is empty
                logger.warn(f"[{self.plugin_name}] Regex pattern is empty in rule: '{self._rename_rule}'. Skipping: {filepath}")
                return

            directory = os.path.dirname(filepath)
            filename = os.path.basename(filepath)

            # Attempt to substitute using the regex.
            # re.sub will return the original string if no match is found.
            new_filename = re.sub(regex_pattern, replacement_format, filename)

            if new_filename == filename:
                logger.debug(f"[{self.plugin_name}] File '{filename}' did not match rule or replacement resulted in same name. Regex: '{regex_pattern}'")
                return

            if not new_filename.strip():
                logger.warning(f"[{self.plugin_name}] Regex replacement resulted in an empty filename for '{filename}' with rule '{self._rename_rule}'. Skipping rename.")
                return

            new_filepath = os.path.join(directory, new_filename)

            # Basic check for trying to rename to itself after path normalization (e.g. case changes on case-insensitive FS)
            if os.path.normcase(filepath) == os.path.normcase(new_filepath):
                logger.debug(f"[{self.plugin_name}] Proposed new path '{new_filepath}' is effectively the same as old path '{filepath}'. No rename needed.")
                return

            if os.path.exists(new_filepath):
                logger.warning(f"[{self.plugin_name}] Target file '{new_filepath}' already exists. Skipping rename of '{filepath}'.")
                return

            os.rename(filepath, new_filepath)
            logger.info(f"[{self.plugin_name}] Renamed: '{filepath}' -> '{new_filepath}'")

        except re.error as regex_err:
            logger.error(f"[{self.plugin_name}] Regex error processing file '{filepath}' with rule '{self._rename_rule}': {regex_err}", exc_info=True)
        except OSError as os_err:
            logger.error(f"[{self.plugin_name}] OS error renaming file '{filepath}': {os_err}", exc_info=True)
        except Exception as e:
            logger.error(f"[{self.plugin_name}] Unexpected error renaming file '{filepath}': {e}", exc_info=True)

    def rename_files_in_directory(self, process_existing: bool = True):
        """
        Processes files in the monitored directory according to the rename rule.
        Typically called by the 'Run Once' feature.
        """
        logger.info(f"[{self.plugin_name}] Starting 'rename_files_in_directory' (process_existing={process_existing}).")
        if not self._monitor_path or not os.path.isdir(self._monitor_path):
            logger.error(f"[{self.plugin_name}] Monitor path '{self._monitor_path}' is invalid. Cannot process files.")
            return

        if not self._rename_rule:
            logger.info(f"[{self.plugin_name}] No rename rule configured. Skipping directory processing.")
            return

        if not process_existing:
            logger.info(f"[{self.plugin_name}] 'process_existing_on_run_once' is disabled. No files will be processed from the directory scan.")
            return

        processed_count = 0
        try:
            for filename in os.listdir(self._monitor_path):
                filepath = os.path.join(self._monitor_path, filename)
                if os.path.isfile(filepath): # Ensure it's a file
                    logger.debug(f"[{self.plugin_name}] Processing existing file: {filepath}")
                    self.process_file(filepath)
                    processed_count +=1
        except Exception as e:
            logger.error(f"[{self.plugin_name}] Error listing or processing files in directory '{self._monitor_path}': {e}", exc_info=True)

        logger.info(f"[{self.plugin_name}] 'rename_files_in_directory' complete. Attempted to process {processed_count} items.")


    def get_state(self) -> Dict[str, Any]: # Returns a dictionary of states
        """Returns the current operational state of the plugin."""
        # The example returned self._onlyonce which is a trigger.
        # A more useful state for UI might be a dict.
        return {
            "monitoring_active": bool(self._observer and self._observer.is_alive()),
            "monitor_path": self._monitor_path if self._enabled else None,
            "scheduler_running_for_run_once": bool(self._scheduler and self._scheduler.running and self._scheduler.get_jobs()),
        }

    # Methods from _PluginBase that are not used or have default behavior
    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self):
        return None # No specific API endpoints for this plugin

    def get_command(self):
        return None # No specific commands

    def get_page(self):
        return None # No custom page, settings are via get_form
