"""对象存储接口（设计文档 3.1 / 9.3）。

为什么先抽象再落地：MinIO 镜像在当前网络被镜像源拦截（见 deploy/docker-compose.yml），
M5 语音上线前必须解决。业务层只依赖本 Protocol，届时切换后端不改调用方。
"""

from typing import Protocol


class ObjectStorage(Protocol):
    """key 由服务端生成（tenant/kb/document 路径），不接受用户输入直接拼路径。"""

    async def save(self, key: str, data: bytes) -> None: ...

    async def read(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...
