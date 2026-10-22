"""敏感词过滤（M9）。

取舍：
- 只在服务端过滤。前端提示挡不住绕过；词库本身是敏感信息，
  下发到浏览器等于公开一份"什么词会被拦"的清单。
- 大小写不敏感 + 子串包含匹配。中文场景没有词边界，
  精确分词匹配漏得更多；宁可误报（转人工/留痕）也不放过。
- 过滤失败绝不能毁掉主链路：它是风控能力，不是对话能力。
  记录失败只记日志，对话照常进行。
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.support import SensitiveHit

logger = get_logger("sensitive")

# 命中片段截断长度：够看出为什么命中，又不把整段用户输入复制一份
SNIPPET_LENGTH = 200


def match(content: str, words: list[str]) -> list[str]:
    """返回命中的词（原样返回词库里的写法，便于前端展示）。"""
    text = content.lower() if content else ""
    if not text:
        return []
    hits: list[str] = []
    for word in words:
        cleaned = (word or "").strip()
        if cleaned and cleaned.lower() in text:
            hits.append(cleaned)
    return hits


async def record_hit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID | None,
    words: list[str],
    action: str,
    content: str,
) -> None:
    """落一条命中记录。block 时消息未落库，message_id 传 None。"""
    hit = SensitiveHit(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_id=message_id,
        words=words,
        action=action,
        snippet=(content or "")[:SNIPPET_LENGTH],
    )
    session.add(hit)
    try:
        await session.commit()
    except Exception:  # noqa: BLE001 - 过滤失败不能让整轮对话失败
        await session.rollback()
        logger.exception("sensitive_hit_record_failed", conversation_id=str(conversation_id))
