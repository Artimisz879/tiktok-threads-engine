"""LLM client: structured output, malformed JSON retry, provider abstraction."""
import json

import httpx
import pytest
import respx

from app.config import Settings
from app.llm.client import LLMClient, LLMError, _extract_json
from app.schemas import ProductIntelligence


def make_settings(**kw):
    base = dict(
        _env_file=None,
        llm_provider="openai",
        llm_base_url="https://llm.test/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_max_retries=2,
        llm_timeout_seconds=5.0,
    )
    base.update(kw)
    return Settings(**base)


class TestExtractJson:
    def test_plain_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fenced(self):
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_prose_wrapped(self):
        assert _extract_json('Sure! Here is the result: {"a": 1} hope that helps') == {"a": 1}

    def test_array(self):
        assert _extract_json("[1, 2, 3]") == [1, 2, 3]

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            _extract_json("no json here at all")


class TestStructured:
    @pytest.mark.asyncio
    async def test_valid_response(self):
        llm = LLMClient(make_settings())
        good = {
            "product_summary": "A portable fan for hot Malaysian days, cheap and useful.",
            "target_audiences": ["students"],
            "problems_solved": ["heat"],
            "pain_points": ["hot"],
        }
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://llm.test/v1/chat/completions").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": json.dumps(good)}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                    },
                )
            )
            out = llm.generate_structured(
                ProductIntelligence, "sys", "user", purpose="test"
            )
        assert out.target_audiences == ["students"]

    @pytest.mark.asyncio
    async def test_malformed_then_valid(self):
        """First response is garbage, retry gets valid JSON. Must succeed."""
        llm = LLMClient(make_settings())
        good = {
            "product_summary": "A portable fan for hot Malaysian days, cheap and useful.",
            "target_audiences": ["students"],
            "problems_solved": ["heat"],
            "pain_points": ["hot"],
        }
        calls = {"n": 0}

        def side_effect(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "I cannot produce JSON right now"}}]},
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(good)}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                },
            )

        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://llm.test/v1/chat/completions").mock(side_effect=side_effect)
            out = llm.generate_structured(ProductIntelligence, "sys", "user", purpose="test")
        assert calls["n"] == 2
        assert out.target_audiences == ["students"]

    @pytest.mark.asyncio
    async def test_always_malformed_raises(self):
        llm = LLMClient(make_settings())
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://llm.test/v1/chat/completions").mock(
                return_value=httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "still no json"}}]},
                )
            )
            with pytest.raises(LLMError):
                llm.generate_structured(ProductIntelligence, "sys", "user", purpose="test")

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        llm = LLMClient(make_settings())
        with respx.mock(assert_all_called=False) as mock:
            mock.post("https://llm.test/v1/chat/completions").mock(
                return_value=httpx.Response(500, text="boom")
            )
            with pytest.raises(LLMError):
                llm.generate_structured(ProductIntelligence, "sys", "user", purpose="test")

    @pytest.mark.asyncio
    async def test_missing_key_raises(self):
        llm = LLMClient(make_settings(llm_api_key=""))
        with pytest.raises(LLMError):
            llm.generate_text("sys", "user")
