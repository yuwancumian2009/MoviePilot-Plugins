"""
插件配置表单生成

MoviePilot 的插件配置界面有两种渲染模式：vue 由插件自带联邦前端产物，
vuetify 则由 get_form() 返回组件定义、宿主按定义渲染。本插件没有随仓库
分发前端产物，声明 vue 只会让宿主去取一个不存在的 remoteEntry.js 并报
「组件加载错误」，因此需要把配置模型翻译成宿主认识的组件定义

翻译的输入是配置模型的 JSON Schema，而不是 typing 内省：Schema 里 Optional、
嵌套模型和数组已经被 pydantic 展开成统一形式，不需要在这里重复判断 Union
"""

from typing import Any, Dict, List, Optional

from .config import ConfigManager

# 描述里出现这些词说明字段是多行文本（路径映射、目录列表、模板等），
# 用单行输入框会让用户没法看清内容
_MULTILINE_HINTS = (
    "路径",
    "目录",
    "映射",
    "列表",
    "每行",
    "格式",
    "模板",
    "正则",
    "爬虫",
    "cookie",
)

# 这些字段是插件自身的运行环境信息或由插件流程写入，不该出现在表单里让用户改
_SKIP_FIELDS = frozenset(
    {
        "PLUSIN_NAME",
        "DB_WAL_ENABLE",
        "PLUGIN_CONFIG_PATH",
        "PLUGIN_DB_PATH",
        "PLUGIN_DATABASE_SCRIPT_LOCATION",
        "PLUGIN_DATABASE_VERSION_LOCATIONS",
        "PLUGIN_TEMP_PATH",
        "aliyundrive_token",
    }
)


def _description_text(field_schema: Dict[str, Any]) -> str:
    """
    取字段描述，缺失时退回字段名

    :param field_schema: 字段的 JSON Schema 片段
    :return str: 用于控件 label 的文本
    """
    desc = field_schema.get("description")
    return desc.strip() if isinstance(desc, str) and desc.strip() else ""


def _unwrap_optional(field_schema: Dict[str, Any], defs: Dict[str, Any]) -> Dict[str, Any]:
    """
    展开 anyOf/oneOf，把 Optional 收敛成非 null 的那一支

    :param field_schema: 字段的 JSON Schema 片段
    :param defs: 顶层 $defs
    :return Dict: 去掉 null 分支后的 Schema
    """
    for keyword in ("anyOf", "oneOf"):
        branches = field_schema.get(keyword)
        if not isinstance(branches, list):
            continue
        real_branches = [
            branch
            for branch in branches
            if not (isinstance(branch, dict) and branch.get("type") == "null")
        ]
        if len(real_branches) == 1:
            merged = dict(real_branches[0])
            # 保留外层的 description/default，内层通常没有
            for key in ("description", "default", "title"):
                if key in field_schema and key not in merged:
                    merged[key] = field_schema[key]
            return _resolve_ref(merged, defs)
    return _resolve_ref(field_schema, defs)


def _resolve_ref(field_schema: Dict[str, Any], defs: Dict[str, Any]) -> Dict[str, Any]:
    """
    把 $ref 替换成它指向的定义

    :param field_schema: 字段的 JSON Schema 片段
    :param defs: 顶层 $defs
    :return Dict: 展开后的 Schema
    """
    ref = field_schema.get("$ref")
    if not isinstance(ref, str):
        return field_schema
    name = ref.rsplit("/", 1)[-1]
    target = defs.get(name)
    if not isinstance(target, dict):
        return field_schema
    merged = dict(target)
    for key in ("description", "default", "title"):
        if key in field_schema and key not in merged:
            merged[key] = field_schema[key]
    return merged


def _control_for(name: str, field_schema: Dict[str, Any], defs: Dict[str, Any]) -> Dict[str, Any]:
    """
    按字段 Schema 挑一个 Vuetify 控件

    :param name: 字段名，用于绑定 model
    :param field_schema: 字段的 JSON Schema 片段
    :param defs: 顶层 $defs
    :return Dict: 单个控件定义
    """
    resolved = _unwrap_optional(field_schema, defs)
    field_type = resolved.get("type")
    label = _description_text(resolved) or name

    # 枚举类字段不引入 VSelect：宿主对下拉组件的可用写法在不同 Vuetify
    # 版本上有差异，写错会整页渲染失败，退而把可选值写进 label 更稳妥
    enum_values = resolved.get("enum")
    if isinstance(enum_values, list) and enum_values:
        label = f"{label}（可选：{' / '.join(str(v) for v in enum_values)}）"

    if field_type == "boolean":
        return {
            "component": "VSwitch",
            "props": {"model": name, "label": label},
        }

    if field_type in ("integer", "number"):
        props: Dict[str, Any] = {"model": name, "label": label, "type": "number"}
        minimum = resolved.get("minimum")
        maximum = resolved.get("maximum")
        if minimum is not None:
            props["min"] = minimum
        if maximum is not None:
            props["max"] = maximum
        return {"component": "VTextField", "props": props}

    if field_type == "string":
        lowered = label.lower()
        if any(hint in lowered for hint in _MULTILINE_HINTS):
            return {
                "component": "VTextarea",
                "props": {
                    "model": name,
                    "label": label,
                    "rows": 3,
                    "auto-grow": True,
                },
            }
        return {"component": "VTextField", "props": {"model": name, "label": label}}

    # 数组、对象、以及任何识别不出类型的字段一律用多行 JSON 文本框兜底，
    # 保证字段不会因为类型复杂而从界面上消失
    return {
        "component": "VTextarea",
        "props": {
            "model": name,
            "label": f"{label}（JSON）",
            "rows": 3,
            "auto-grow": True,
        },
    }


def build_config_form() -> List[Dict[str, Any]]:
    """
    按配置模型生成 Vuetify 表单定义

    :return List[Dict]: 可直接交给宿主的组件定义列表
    """
    schema = ConfigManager.model_json_schema()
    properties = schema.get("properties") or {}
    defs = schema.get("$defs") or {}

    content: List[Dict[str, Any]] = [
        {
            "component": "VCol",
            "props": {"cols": 12, "md": 4},
            "content": [
                {
                    "component": "VSwitch",
                    "props": {"model": "enabled", "label": "启用插件"},
                }
            ],
        }
    ]

    for name, field_schema in properties.items():
        if name in _SKIP_FIELDS or not isinstance(field_schema, dict):
            continue
        control = _control_for(name, field_schema, defs)
        resolved = _unwrap_optional(field_schema, defs)
        is_wide = control["component"] == "VTextarea"
        content.append(
            {
                "component": "VCol",
                "props": {"cols": 12} if is_wide else {"cols": 12, "md": 6},
                "content": [control],
            }
        )

    return [{"component": "VForm", "content": [{"component": "VRow", "content": content}]}]


def build_config_model(current: Dict[str, Any], enabled: Optional[bool] = None) -> Dict[str, Any]:
    """
    组装表单初始数据，保证每个控件绑定的键都存在

    :param current: 当前配置字典
    :param enabled: 插件启用状态，未知时传 None
    :return Dict: 表单 model
    """
    model: Dict[str, Any] = dict(current)
    if enabled is not None:
        model["enabled"] = bool(enabled)
    else:
        model.setdefault("enabled", False)
    return model
