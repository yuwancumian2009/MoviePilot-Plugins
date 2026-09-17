"""
MoviePilot V3 durable 整理管线兼容补丁

MoviePilot V3 起 TransferChain 重构为 durable 管线（准入 → 检查点 → 执行 → 终态结算），
TransferChain.transfer() 不再是 V2 中"只执行整理"的语义，而是会新建任务并重新准入的兼容命令
（execute_legacy_transfer_command）。TransferChainPatcher 的回退执行实现仍按 V2 写法调用它，
于是在 V3 宿主上必然撞上两层错误

1. 外层整理队列已为当前任务取得租约，内层兼容命令再次准入同一源文件一定冲突
   旧整理兼容命令执行失败：源文件已有活动整理任务或关联整理历史
2. 外层任务从未经过核心规划流程，拿不到 execution_checkpoint，随后由终态结算回调抛
   RuntimeError 非预览整理终态缺少持久执行检查点，任务被判失败

本模块把回退执行改为 V3 感知：在当前任务上委托宿主原生 __handle_transfer 完成规划、检查点与执行，
durable 生命周期得以闭环；V2 宿主上完全沿用原有实现
"""

from app.log import logger

from .transfer_chain import TransferChainPatcher

V3_DURABLE_ENTRY = "_TransferChain__plan_checkpoint_and_execute"

_installed = False


def _call_durable_transfer_part(cls, chain_self, task, callback):
    """
    在当前任务上执行 MoviePilot V3 durable 整理管线

    宿主原生方法引用缺失时的兜底路径：直接调用宿主的规划-检查点-执行入口，
    使 plan_checkpoint 与 execution_checkpoint 都绑定到同一个任务，后续由队列执行的
    durable 终态结算才能通过校验

    :param cls: TransferChainPatcher 类本身
    :param chain_self: TransferChain 实例
    :param task: 任务
    :param callback: 回调
    :return: 返回值
    """
    try:
        # 正在处理
        chain_self.jobview.running_task(task)

        # 获取源、目标存储操作对象
        select_oper = getattr(chain_self, "_TransferChain__select_storage_oper", None)
        source_oper = (
            select_oper(task.fileitem.storage) if callable(select_oper) else None
        )
        target_oper = (
            select_oper(task.target_storage) if callable(select_oper) else None
        )

        # 纯规划先提交 durable checkpoint，任何文件副作用只能发生在提交之后
        transferinfo = getattr(chain_self, V3_DURABLE_ENTRY)(
            task,
            source_oper=source_oper,
            target_oper=target_oper,
        )

        if not transferinfo:
            logger.error("文件整理模块运行失败")
            return False, "文件整理模块运行失败"

        if callback:
            return callback(task, transferinfo)

        return transferinfo.success, transferinfo.message

    except Exception as e:
        logger.error(f"【整理接管】执行 transfer 失败: {e}", exc_info=True)
        return False, f"整理失败: {e}"


def install_durable_compat() -> bool:
    """
    为 TransferChainPatcher 安装 V3 durable 兼容的回退执行路径

    :return: 是否安装成功
    """
    global _installed
    if _installed:
        return True

    descriptor = TransferChainPatcher.__dict__.get("_call_original_transfer_part")
    if descriptor is None:
        logger.warn(
            "【整理接管】未找到 _call_original_transfer_part，V3 durable 兼容补丁未安装"
        )
        return False

    original = getattr(descriptor, "__func__", descriptor)

    def call_original_transfer_part(cls, chain_self, task, callback=None):
        """
        调用原方法的 transfer 部分，V3 宿主改走 durable 管线

        :param cls: TransferChainPatcher 类本身
        :param chain_self: TransferChain 实例
        :param task: 任务
        :param callback: 回调
        :return: 返回值
        """
        if hasattr(chain_self, V3_DURABLE_ENTRY):
            native = cls._original_handle_transfer
            if native is not None:
                logger.debug(
                    "【整理接管】检测到 MoviePilot V3 durable 整理管线，委托原生方法执行整理"
                )
                return native(chain_self, task, callback)
            return _call_durable_transfer_part(cls, chain_self, task, callback)

        return original(cls, chain_self, task, callback)

    call_original_transfer_part.__name__ = getattr(
        original, "__name__", "call_original_transfer_part"
    )
    TransferChainPatcher._call_original_transfer_part = classmethod(
        call_original_transfer_part
    )
    _installed = True
    logger.debug("【整理接管】已安装 MoviePilot V3 durable 整理兼容补丁")
    return True
