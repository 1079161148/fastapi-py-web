"""文件/对象存储（设计文档 3.1）。"""

from app.storage.base import ObjectStorage
from app.storage.factory import get_storage

__all__ = ["ObjectStorage", "get_storage"]
