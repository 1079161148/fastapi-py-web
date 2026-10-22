"""cursor 分页（设计文档第 7 章：会话消息量大会持续增长）。

游标 = base64url("<排序时间 ISO>|<行 ID>")，与排序键 (时间, id) 一一对应。
查询用 OR/AND 展开的时间+ID 双重比较，而不是行比较 (a,b) < (c,d)：
同一毫秒内多条记录也不会重复或漏行。

游标是不透明字符串，前端不得解析——内部结构随时可换。
"""

import base64
import binascii
import uuid
from datetime import datetime

from app.core.exceptions import AppError


def encode_cursor(sort_time: datetime, row_id: uuid.UUID) -> str:
    raw = f"{sort_time.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        time_raw, id_raw = raw.rsplit("|", 1)
        return datetime.fromisoformat(time_raw), uuid.UUID(id_raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise AppError(
            code="INVALID_CURSOR", message="分页游标无效，请刷新后重试", status_code=400
        ) from exc
