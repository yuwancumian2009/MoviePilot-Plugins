import json
import os
import subprocess
import sys
import ast
from typing import Dict, Optional, Tuple, List


def _extract_version_from_file(file_path: str, var_name: str) -> Tuple[Optional[str], Optional[str]]:
    if not os.path.exists(file_path):
        return None, f"文件未找到: {file_path}"
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=file_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == var_name:
                        if isinstance(node.value, ast.Constant):
                            return str(node.value.value), None
                        elif isinstance(node.value, (ast.Str, ast.Num)):
                            return str(node.value.s), None
        return None, f"在 {file_path} 中未找到 '{var_name}' 变量"
    except Exception as e:
        return None, f"解析文件 {file_path} 时出错: {e}"


def get_version_from_source(plugin_dir: str) -> Tuple[Optional[str], Optional[str]]:
    version_py_path = os.path.join(plugin_dir, 'version.py')
    if os.path.exists(version_py_path):
        version, err = _extract_version_from_file(version_py_path, 'VERSION')
        if version or (err and "文件未找到" not in err):
            log(f"[Info] 尝试从 {version_py_path} 获取版本...")
            return version, err

    init_py_path = os.path.join(plugin_dir, '__init__.py')
    log(f"[Info] 尝试从 {init_py_path} 获取版本...")
    return _extract_version_from_file(init_py_path, 'plugin_version')


def build_plugin_metadata(plugin_id, version, source_dir, package_data, is_prerelease=False) -> Dict:
    plugin_info = package_data.get(plugin_id, {})
    lowercase_id = plugin_id.lower()
    history_key = "prerelease_history" if is_prerelease else "history"
    notes = plugin_info.get(history_key, {}).get(f"v{version}", "")
    return {
        "id": plugin_id,
        "version": version,
        "name": plugin_info.get("name", ""),
        "notes": notes,
        "tag_name": f"{plugin_id}_v{version}",
        "archive_base": f"{lowercase_id}_v{version}",
        "backend_worker_path": f"{source_dir}",
        "backend_path": f"{lowercase_id}",
    }


def handle_workflow_dispatch() -> List[Dict]:
    plugins_to_release = []
    plugin_id = os.environ.get("INPUT_PLUGIN_ID", "").strip()
    source_dir = os.environ.get("INPUT_SOURCE_DIRECTORY", "").strip()
    is_prerelease = os.environ.get("INPUT_PRERELEASE", "false").lower() == "true"

    try:
        if not plugin_id or not source_dir:
            raise ValueError("[必须提供插件 ID 和源码目录。请检查输入参数是否正确设置。")
        suffix = source_dir.replace("plugins", "")
        package_file_name = f"package{suffix}.json"
        release_mode_text = "预发布" if is_prerelease else "正式版"
        log(f"[Info] 手动模式 ({release_mode_text})：正在处理来自 {package_file_name} 的插件 {plugin_id}")

        if not os.path.exists(package_file_name):
            raise FileNotFoundError(f"文件 {package_file_name} 未找到。请检查路径是否正确。")

        with open(package_file_name, 'r', encoding='utf-8') as f:
            package_data = json.load(f)
        plugin_info = package_data.get(plugin_id)

        if not plugin_info:
            raise ValueError(f"插件 {plugin_id} 在 package(.*).json 文件中未找到。请检查插件 ID 是否正确。")

        plugin_code_dir = f"{source_dir}/{plugin_id.lower()}"
        py_version, err = get_version_from_source(plugin_code_dir)

        if err:
            raise ValueError(err)

        log(f"[Info] 从源码 (version.py 或 __init__.py) 中获取的版本号为: {py_version}")

        if is_prerelease:
            prerelease_versions = plugin_info.get("prerelease_vers", [])
            if py_version in prerelease_versions:
                log(f"[Info] ✅ 版本校验通过: {py_version} 存在于 prerelease_vers 列表中。")
                plugins_to_release.append(
                    build_plugin_metadata(plugin_id, py_version, source_dir, package_data, is_prerelease=True))
            else:
                raise ValueError(
                    f"预发布版本号不匹配: {py_version} 不在 package.json 的 prerelease_vers 列表中 ({prerelease_versions})。"
                )
        else:
            json_version = plugin_info.get("version")
            if not plugin_info.get("release", False):
                raise ValueError(f"插件 '{plugin_id}' 未被标记为可发布 (release: true)。")
            if py_version == json_version:
                log(f"[Info] ✅ 版本校验通过: 源码文件与 package.json 中的版本一致 ({py_version})。")
                plugins_to_release.append(
                    build_plugin_metadata(plugin_id, py_version, source_dir, package_data, is_prerelease=False))
            else:
                raise ValueError(
                    f"正式版版本号不匹配: 源码中的版本 {py_version} 与 package.json 中的版本 {json_version} 不一致。"
                )

    except Exception as e:
        log(f"[Fatal] 处理手动触发时出错: {e}")

    return plugins_to_release


def handle_push() -> List[Dict]:
    plugins_to_release = []
    before_sha = os.environ.get("BEFORE_SHA")
    after_sha = os.environ.get("AFTER_SHA")

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", before_sha, after_sha],
            capture_output=True, text=True, check=True
        )
        changed_files = result.stdout.strip().split('\n')
        log(f"[Info] 检测到以下文件变更:\n{result.stdout.strip()}")

        package_files = [f for f in changed_files if f.startswith("package") and f.endswith(".json")]

        for package_file in package_files:
            log(f"[Info] 正在处理变更的 package 文件: {package_file}")

            try:
                old_content_raw = subprocess.check_output(["git", "show", f"{before_sha}:{package_file}"])
                old_package_data = json.loads(old_content_raw)
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                old_package_data = {}

            with open(package_file, 'r', encoding='utf-8') as f:
                new_package_data = json.load(f)

            suffix = package_file.replace("package", "").replace(".json", "")
            source_dir = f"plugins{suffix}"
            log(f"[Info] 推断出的源码目录: {source_dir}")

            all_plugin_ids = set(old_package_data.keys()) | set(new_package_data.keys())

            for plugin_id in all_plugin_ids:
                old_info = old_package_data.get(plugin_id, {})
                new_info = new_package_data.get(plugin_id, {})
                old_version = old_info.get("version")
                new_version = new_info.get("version")
                is_releasable = new_info.get("release", False)

                old_prerelease_vers = set(old_info.get("prerelease_vers", []))
                new_prerelease_vers = set(new_info.get("prerelease_vers", []))

                plugin_code_dir = f"{source_dir}/{plugin_id.lower()}"

                if old_version != new_version and new_version and is_releasable:
                    log(f"[Info] 检测到正式版发布意图: {plugin_id} (版本: {old_version} -> {new_version})")

                    py_version, err = get_version_from_source(plugin_code_dir)

                    if err:
                        log(f"[Fatal] {err}")
                        continue

                    if new_version == py_version:
                        log(f"[Info] ✅ 版本一致 ({new_version})。准备加入正式版发布矩阵。")
                        plugins_to_release.append(
                            build_plugin_metadata(plugin_id, new_version, source_dir, new_package_data, is_prerelease=False))
                    else:
                        log(f"[Fatal] 正式版版本号不匹配: {plugin_id}")
                        log(f"- package.json 中的版本: {new_version}")
                        log(f"- 源码文件中的版本:  {py_version}")
                        continue

                elif old_prerelease_vers != new_prerelease_vers:
                    added_vers = new_prerelease_vers - old_prerelease_vers
                    if not added_vers:
                        log(f"[Debug] ⏩ 跳过插件: {plugin_id} (仅删除了预发布版本，无新增)")
                        continue

                    prerelease_version_to_check = list(added_vers)[0]
                    if len(added_vers) > 1:
                        log(f"[Warn] 检测到多个新增的预发布版本 {added_vers}，将只处理第一个: {prerelease_version_to_check}")

                    log(f"[Info] 检测到预发布版发布意图: {plugin_id} (新增版本: {prerelease_version_to_check})")

                    py_version, err = get_version_from_source(plugin_code_dir)

                    if err:
                        log(f"[Fatal] {err}")
                        continue

                    if prerelease_version_to_check == py_version:
                        log(f"[Info] ✅ 版本一致 ({py_version})。准备加入预发布版发布矩阵。")
                        plugins_to_release.append(
                            build_plugin_metadata(plugin_id, py_version, source_dir, new_package_data, is_prerelease=True))
                    else:
                        log(f"[Fatal] 预发布版版本号不匹配: {plugin_id}")
                        log(f"- package.json 中新增的版本: {prerelease_version_to_check}")
                        log(f"- 源码文件中的版本:  {py_version}")
                        continue

                else:
                    log(f"[Debug] ⏩ 跳过插件: {plugin_id} (无版本变更)")

    except subprocess.CalledProcessError as e:
        log(f"[Fatal] 执行 git diff 时出错: {e}")
    except Exception as e:
        log(f"[Fatal] 处理推送事件时出错: {e}")

    return plugins_to_release


def log(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


if __name__ == "__main__":
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    final_plugins = []
    if event_name == "workflow_dispatch":
        final_plugins = handle_workflow_dispatch()
    elif event_name == "push":
        final_plugins = handle_push()
    else:
        log(f"[Error] 不支持的事件类型: {event_name}")
        sys.exit(1)

    print(json.dumps(final_plugins))
