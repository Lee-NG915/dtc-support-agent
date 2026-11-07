import json

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.core.exceptions import LLMProviderError
from app.providers.deepseek import DeepSeekProvider

BASE_URL = "https://api.deepseek.com"


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "deepseek_api_key": SecretStr("test-key"),
        "deepseek_base_url": BASE_URL,
        "deepseek_model": "deepseek-flash",
        "llm_max_retries": 1,
    }
    values.update(overrides)
    return Settings(**values)


def _generate_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


@pytest.mark.asyncio
async def test_reasoning_compat_raises_integer_token_headroom() -> None:
    settings = _settings(deepseek_min_max_tokens=2048, deepseek_reasoning_effort="high")
    async with httpx.AsyncClient() as client:
        provider = DeepSeekProvider(settings, client=client)
        payload: dict[str, object] = {"max_tokens": 700}

        provider._apply_reasoning_compat(payload)

    assert payload == {"max_tokens": 2048, "reasoning_effort": "high"}


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("disabled", {"type": "disabled"}),
        ("enabled", {"type": "enabled"}),
        (None, None),
    ],
)
async def test_generate_applies_explicit_thinking_toggle_only_when_set(
    mode: str | None, expected: dict[str, str] | None
) -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return _generate_response()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    ) as client:
        provider = DeepSeekProvider(_settings(deepseek_thinking_mode=mode), client=client)
        await provider.generate([{"role": "user", "content": "hi"}], max_tokens=16)

    assert captured[0]["model"] == "deepseek-flash"
    if expected is None:
        assert "thinking" not in captured[0]
    else:
        assert captured[0]["thinking"] == expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("disabled", {"type": "disabled"}),
        ("enabled", {"type": "enabled"}),
        (None, None),
    ],
)
async def test_stream_applies_explicit_thinking_toggle_only_when_set(
    mode: str | None, expected: dict[str, str] | None
) -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n' b"data: [DONE]\n\n"),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    ) as client:
        provider = DeepSeekProvider(_settings(deepseek_thinking_mode=mode), client=client)
        chunks = [
            chunk
            async for chunk in provider.stream([{"role": "user", "content": "hi"}], max_tokens=16)
        ]

    assert chunks == ["ok"]
    if expected is None:
        assert "thinking" not in captured[0]
    else:
        assert captured[0]["thinking"] == expected


async def test_generate_and_stream_send_the_same_thinking_payload() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        if captured[-1].get("stream"):
            return httpx.Response(
                200,
                content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return _generate_response()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    ) as client:
        provider = DeepSeekProvider(_settings(deepseek_thinking_mode="disabled"), client=client)
        await provider.generate([{"role": "user", "content": "hi"}], max_tokens=16)
        async for _ in provider.stream([{"role": "user", "content": "hi"}], max_tokens=16):
            pass

    assert captured[0]["thinking"] == captured[1]["thinking"] == {"type": "disabled"}


async def test_http_400_stays_non_retryable_and_does_not_echo_response_body() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad test-key"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=BASE_URL
    ) as client:
        provider = DeepSeekProvider(_settings(), client=client)
        with pytest.raises(LLMProviderError) as excinfo:
            await provider.generate([{"role": "user", "content": "hi"}], max_tokens=16)

    assert calls == 1
    assert excinfo.value.retryable is False
    assert str(excinfo.value) == "DeepSeek HTTP 400"
    assert "test-key" not in str(excinfo.value)
