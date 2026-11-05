import httpx
import pytest

from app.config.settings import Settings
from app.providers.deepseek import DeepSeekProvider


@pytest.mark.asyncio
async def test_reasoning_compat_raises_integer_token_headroom() -> None:
    settings = Settings(deepseek_min_max_tokens=2048, deepseek_reasoning_effort="high")
    async with httpx.AsyncClient() as client:
        provider = DeepSeekProvider(settings, client=client)
        payload: dict[str, object] = {"max_tokens": 700}

        provider._apply_reasoning_compat(payload)

    assert payload == {"max_tokens": 2048, "reasoning_effort": "high"}
