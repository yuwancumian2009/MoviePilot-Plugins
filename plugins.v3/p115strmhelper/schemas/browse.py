from typing import List, Optional

from pydantic import BaseModel, Field


class BrowseDirParams(BaseModel):
    """
    浏览目录请求参数
    """

    path: str = Field(default="/", description="目录路径")
    is_local: Optional[bool] = Field(
        default=None,
        description="是否本地目录，不传时按路径是否存在于当前运行环境自动判断",
    )


class DirectoryItem(BaseModel):
    """
    目录条目
    """

    name: str = Field(..., description="文件/目录名")
    path: str = Field(..., description="路径")
    is_dir: bool = Field(..., description="是否为目录")


class BrowseDirData(BaseModel):
    """
    浏览目录响应数据
    """

    path: str = Field(..., description="当前路径")
    items: List[DirectoryItem] = Field(..., description="目录内容列表")
