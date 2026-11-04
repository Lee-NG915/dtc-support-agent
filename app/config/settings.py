"""Environment-backed settings with a cached accessor."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed runtime configuration.

    Secrets stay in SecretStr so accidental str() / logs do not leak keys.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "testing", "production"] = "development"
    app_name: str = "adaptive-rag-cs"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"

    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    # 采样温度覆盖：None 表示沿用各调用方传入值。
    # 部分 OpenAI 兼容模型（如 Kimi Code kimi-for-coding）只允许 temperature=1，
    # 此时在 .env 设 DEEPSEEK_TEMPERATURE=1，由 Provider 统一覆盖。
    deepseek_temperature: float | None = None
    # 推理模型（如 Kimi Code kimi-for-coding）适配：
    # reasoning_effort 非空时透传给 API（low/high/max），low 可降低延迟与推理 token 消耗。
    deepseek_reasoning_effort: str = ""
    # max_tokens 下限：推理模型的 reasoning_tokens 也计入 max_tokens，
    # 业务节点按非推理模型给的 256~700 预算会被推理耗尽，导致 JSON 截断。
    # 0 表示不调整；使用推理模型时建议 2048。
    deepseek_min_max_tokens: int = 0
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 3

    embedding_api_key: SecretStr = SecretStr("")
    embedding_base_url: str = ""
    embedding_model: str = "bge-small-zh-v1.5"
    embedding_dim: int = 512
    local_embedding_enabled: bool = True
    local_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    local_embedding_device: str = ""
    local_embedding_force: bool = False

    cross_encoder_enabled: bool = True
    cross_encoder_model: str = "BAAI/bge-reranker-base"
    cross_encoder_device: str = ""
    cross_encoder_max_length: int = 512
    cross_encoder_force: bool = False

    database_url: str = "sqlite+aiosqlite:///./data/local/app.db"
    redis_url: str = ""
    qdrant_url: str = ""
    qdrant_collection: str = "cs_knowledge"

    retrieval_top_k_simple: int = 3
    retrieval_top_k_complex: int = 8
    rrf_k: int = 60
    rerank_top_n: int = 5
    max_retrieval_hops: int = 3

    token_budget: int = 128_000
    compress_soft_ratio: float = 0.50
    compress_hard_ratio: float = 0.70
    auto_compact_ratio: float = 0.85

    max_agent_steps: int = 8
    max_tool_calls: int = 6

    rate_limit_per_minute: int = 60
    retrieval_cache_ttl_seconds: int = 60
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    admin_api_token: SecretStr = SecretStr("")
    knowledge_dir: str = "knowledge"
    unstructured_enabled: bool = True
    unstructured_strategy: str = "auto"
    unstructured_ocr_languages: str = "chi_sim+eng"
    unstructured_force: bool = False
    mcp_enabled: bool = True
    mcp_remote_url: str = ""

    @property
    def llm_configured(self) -> bool:
        return bool(self.deepseek_api_key.get_secret_value())

    @property
    def embedding_configured(self) -> bool:
        return bool(self.embedding_api_key.get_secret_value() and self.embedding_base_url)

    @property
    def local_embedding_configured(self) -> bool:
        """True when production will attempt to load the local dense model."""
        return self.local_embedding_enabled and (
            self.app_env != "testing" or self.local_embedding_force
        )

    @property
    def cross_encoder_configured(self) -> bool:
        """True when production path will attempt to load the Cross-Encoder.

        Tests stay on lexical fallback unless CROSS_ENCODER_FORCE is set.
        """
        return self.cross_encoder_enabled and (
            self.app_env != "testing" or self.cross_encoder_force
        )

    @property
    def unstructured_configured(self) -> bool:
        """True when ingest will attempt Unstructured partition.

        Tests stay on markdown heading split unless UNSTRUCTURED_FORCE is set.
        """
        return self.unstructured_enabled and (self.app_env != "testing" or self.unstructured_force)

    @property
    def redis_configured(self) -> bool:
        return bool(self.redis_url)

    @property
    def qdrant_configured(self) -> bool:
        return bool(self.qdrant_url)

    @property
    def use_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    """Used by tests after monkeypatching environment variables."""
    get_settings.cache_clear()
