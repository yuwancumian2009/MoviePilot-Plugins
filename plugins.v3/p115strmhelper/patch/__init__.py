from .u115_open import U115Patcher
from .transfer_chain import TransferChainPatcher
from .p115disk_upload import P115DiskPatcher
from .app_ver import AppVerPatcher
from .durable_compat import install_durable_compat


__all__ = [
    "U115Patcher",
    "TransferChainPatcher",
    "P115DiskPatcher",
    "AppVerPatcher",
]


# MoviePilot V3 的 durable 整理管线需要不同的回退执行路径，详见 durable_compat 模块
install_durable_compat()
