"""LLM 客户端工厂：集中读取配置并做"缺失即失败"的校验。"""

from app.core.config import get_settings
from app.llm.base import EmbeddingClient, LLMClient, LLMNotConfiguredError, RerankClient
from app.llm.openai_compatible import OpenAICompatibleClient
from app.llm.rerank import OpenAIRerankClient


def get_llm_client() -> LLMClient:
    """按环境变量构造客户端；未配置时抛 503 而非返回"哑客户端"。

    三条配置缺一不可：缺 key 无法鉴权，缺 base_url 无法定位端点，
    缺 model 无法指定模型。任何一条为空都只会把问题拖到运行期，
    不如在请求入口明确拒绝。
    """
    settings = get_settings()
    required = {
        "AI_API_KEY": settings.ai_api_key,
        "AI_BASE_URL": settings.ai_base_url,
        "AI_MODEL": settings.ai_model,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise LLMNotConfiguredError(missing)

    return OpenAICompatibleClient(
        api_key=settings.ai_api_key,
        base_url=settings.ai_base_url,
        model=settings.ai_model,
        timeout_seconds=settings.ai_timeout_seconds,
    )


def get_embedding_client() -> EmbeddingClient:
    """嵌入客户端：密钥与端点可回退到对话配置，**模型必须单独指定**。

    设计文档允许"缺省回退到对话模型端点"，这里只回退 key/base_url。
    原因是几乎不存在既能对话又能嵌入的模型：把 AI_MODEL 拿去调 /embeddings
    只会得到供应商端的晦涩报错，不如启动依赖时就说清缺 AI_EMBEDDING_MODEL。
    """
    settings = get_settings()
    api_key = settings.ai_embedding_api_key.strip() or settings.ai_api_key.strip()
    base_url = settings.ai_embedding_base_url.strip() or settings.ai_base_url.strip()
    model = settings.ai_embedding_model.strip()

    missing: list[str] = []
    if not api_key:
        missing.append("AI_EMBEDDING_API_KEY（或 AI_API_KEY）")
    if not base_url:
        missing.append("AI_EMBEDDING_BASE_URL（或 AI_BASE_URL）")
    if not model:
        missing.append("AI_EMBEDDING_MODEL")
    if missing:
        raise LLMNotConfiguredError(missing)

    return OpenAICompatibleClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        # 嵌入只处理一条查询或一批分块，远快于对话；超过 30s 多半是网关挂了。
        # 早失败早降级：OpenRouter 免费模型上游 60s 才回 504，等它等于让用户干等
        timeout_seconds=min(settings.ai_timeout_seconds, 30.0),
        expected_embedding_dim=settings.ai_embedding_dim,
    )


def get_rerank_client() -> RerankClient | None:
    """重排客户端：未配置模型或缺少凭证时返回 None（按设计"缺省关闭"）。

    返回 None 而不是抛错：rerank 是可选增强，缺省应退化为向量+BM25 的 RRF 结果。
    """
    settings = get_settings()
    if not settings.ai_rerank_model.strip():
        return None
    api_key = settings.ai_embedding_api_key.strip() or settings.ai_api_key.strip()
    base_url = settings.ai_embedding_base_url.strip() or settings.ai_base_url.strip()
    if not api_key or not base_url:
        return None
    return OpenAIRerankClient(
        api_key=api_key,
        base_url=base_url,
        model=settings.ai_rerank_model,
        timeout_seconds=min(settings.ai_timeout_seconds, 30.0),
    )
