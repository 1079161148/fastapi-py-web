"""本地文件系统存储（开发/单机）。

约束：
- 落盘在阻塞线程执行（asyncio.to_thread），不阻塞事件循环
- key 只允许字母、数字、下划线、连字符、点和斜杠；解析后必须仍在根目录内，
  双重校验防路径穿越（即便 key 目前全部由服务端生成，也不放松）
"""

import asyncio
import re
from pathlib import Path

SAFE_KEY = re.compile(r"^[A-Za-z0-9._/-]+$")


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        if not key or not SAFE_KEY.match(key) or ".." in key.split("/"):
            raise ValueError(f"非法的存储 key：{key!r}")
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise ValueError(f"存储 key 越界：{key!r}")
        return path

    async def save(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        await asyncio.to_thread(self._write, path, data)

    async def read(self, key: str) -> bytes:
        path = self._resolve(key)
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        await asyncio.to_thread(self._unlink, path)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def _unlink(path: Path) -> None:
        # 幂等删除：文件不存在不算错误（重试/重复删除场景）
        path.unlink(missing_ok=True)
