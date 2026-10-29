"""Bounded Meta HTTP adapter. Mock transports are always labelled simulated."""
import asyncio
from contextvars import ContextVar
from copy import deepcopy
import json
import re

import httpx

from app.ai.client import AIUnavailableError
from app.ai.prompt import CONTRACT_VERSION, PROMPT_VERSION, provider_request
from app.config import Settings
from app.models.schemas.ai import AnalysisRequest


class MetaProviderError(AIUnavailableError):
    def __init__(self, category: str):
        self.category = category
        super().__init__("Meta analysis failed: " + category)


class MetaConfigurationError(ValueError):
    """Only fixed safe messages are used for provider configuration failures."""


def strict_json(raw: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Nonfinite JSON number")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


class MetaAIClient:
    provider = "Meta Model API"
    prompt_version = PROMPT_VERSION

    def __init__(self, settings: Settings, *, transport: httpx.MockTransport | None = None):
        settings = Settings(_env_file=None, **settings.model_dump())
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise MetaConfigurationError("Simulation requires an explicit HTTPX MockTransport")
        if transport is None and not settings.meta_live_enabled:
            raise MetaConfigurationError("Live access disabled; configuration remains mock-only")
        if settings.model_api_key is None or not settings.model_api_key.get_secret_value().strip():
            raise MetaConfigurationError("MODEL_API_KEY is required for the Meta adapter")
        self.settings = settings
        self.model_name = settings.meta_model
        self.data_source = "meta_simulated" if transport is not None else "meta_live"
        self.inference_mode = "simulated" if transport is not None else "live"
        self._key = settings.model_api_key.get_secret_value()
        self._trace = ContextVar("meta_call_details", default=None)
        self._semaphore = asyncio.Semaphore(settings.meta_max_concurrency)
        self._budget_lock = asyncio.Lock()
        self.calls_reserved = 0
        self.tokens_reserved = 0
        self._budget_blocked = False
        self._client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(settings.meta_read_timeout,
                                  connect=settings.meta_connect_timeout, write=5.0, pool=2.0),
            limits=httpx.Limits(max_connections=settings.meta_max_concurrency),
        )

    def sanitize(self, text: str) -> str:
        def clean_string(value: str, depth: int) -> str:
            cleaned = value.replace(self._key, "[REDACTED]")
            cleaned = re.sub(r"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", cleaned)
            # Provider envelopes can contain JSON encoded inside JSON strings.
            # Decode bounded layers so escaped credentials cannot survive redaction.
            if depth < 4 and cleaned.lstrip().startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(cleaned)
                    redacted = walk(decoded, depth + 1)
                    if redacted != decoded:
                        return json.dumps(redacted, ensure_ascii=True, separators=(",", ":"))
                except (ValueError, TypeError, RecursionError):
                    pass
            return cleaned

        def walk(value, depth: int):
            if isinstance(value, str):
                return clean_string(value, depth)
            if isinstance(value, dict):
                return {clean_string(key, depth): walk(item, depth) for key, item in value.items()}
            if isinstance(value, list):
                return [walk(item, depth) for item in value]
            return value

        return clean_string(text, 0)

    def call_trace(self) -> dict:
        return deepcopy(self._trace.get() or {})

    async def _reserve(self, token_bound: int):
        async with self._budget_lock:
            if (self._budget_blocked or self.calls_reserved >= self.settings.meta_max_calls
                    or self.tokens_reserved + token_bound > self.settings.meta_max_total_tokens):
                raise MetaProviderError("budget_exhausted")
            # Failed and ambiguous calls retain their full pessimistic reservation.
            self.calls_reserved += 1
            self.tokens_reserved += token_bound

    async def extract_and_strategise(self, payload: AnalysisRequest) -> str:
        body = provider_request(payload, self.model_name, self.settings.meta_max_output_tokens)
        encoded = self.sanitize(json.dumps(body, ensure_ascii=True, separators=(",", ":"))).encode()
        trace = {
            "provider": self.provider, "inference_mode": self.inference_mode,
            "prompt_version": self.prompt_version, "contract_version": CONTRACT_VERSION,
            "provider_request": strict_json(encoded.decode()), "attempts": 0,
            "raw_provider_response": None, "usage": None, "error_category": None,
            "reservation_tokens_per_attempt": len(encoded) + self.settings.meta_max_output_tokens,
            "attempt_history": [],
        }
        self._trace.set(trace)
        try:
            if self._client.is_closed:
                raise MetaProviderError("client_closed")
            if len(encoded) > self.settings.meta_max_request_bytes:
                raise MetaProviderError("input_too_large")
            async with asyncio.timeout(self.settings.meta_total_timeout):
                async with self._semaphore:
                    for attempt in range(self.settings.meta_max_retries + 1):
                        await self._reserve(len(encoded) + self.settings.meta_max_output_tokens)
                        trace["attempts"] += 1
                        trace.update(http_status=None, provider_request_id=None, raw_provider_response=None)
                        async with self._client.stream(
                            "POST", "https://api.meta.ai/v1/chat/completions",
                            content=encoded, headers={
                                "Authorization": "Bearer " + self._key,
                                "Content-Type": "application/json",
                            },
                        ) as response:
                            trace["http_status"] = response.status_code
                            trace["provider_request_id"] = self.sanitize(response.headers.get("x-request-id", ""))[:128]
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(chunks) + len(chunk) > self.settings.meta_max_response_bytes:
                                    raise MetaProviderError("response_too_large")
                                chunks.extend(chunk)
                            try:
                                raw = bytes(chunks).decode("utf-8")
                            except UnicodeDecodeError:
                                raise MetaProviderError("invalid_encoding") from None
                            trace["raw_provider_response"] = self.sanitize(raw)
                            status = response.status_code
                            trace["attempt_history"].append({
                                "attempt": trace["attempts"], "http_status": status,
                                "raw_response": trace["raw_provider_response"],
                                "provider_request_id": trace["provider_request_id"],
                            })
                            if status == 429 and attempt < self.settings.meta_max_retries:
                                retry = response.headers.get("retry-after", "1")
                                try:
                                    delay = float(retry)
                                except ValueError:
                                    delay = -1
                                if not 0 <= delay <= 2:
                                    raise MetaProviderError("rate_limited")
                                await asyncio.sleep(delay)
                                continue
                            if status != 200:
                                category = {400: "bad_request", 401: "authentication", 403: "forbidden",
                                            429: "rate_limited"}.get(status, "server_error" if status >= 500 else "http_error")
                                raise MetaProviderError(category)
                            if response.headers.get("content-type", "").split(";")[0].lower() != "application/json":
                                raise MetaProviderError("invalid_content_type")
                            return self._content(raw, trace)
            raise MetaProviderError("rate_limited")
        except MetaProviderError as exc:
            trace["error_category"] = exc.category
            raise
        except (httpx.TimeoutException, TimeoutError):
            trace["error_category"] = "timeout"
            raise MetaProviderError("timeout") from None
        except httpx.HTTPError:
            trace["error_category"] = "transport_error"
            raise MetaProviderError("transport_error") from None

    def _content(self, raw: str, trace: dict) -> str:
        try:
            envelope = strict_json(raw)
            if not isinstance(envelope, dict) or envelope.get("model") != self.model_name:
                raise MetaProviderError("model_mismatch")
            choices = envelope["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise MetaProviderError("invalid_envelope")
            choice = choices[0]
            message = choice["message"]
            if message.get("refusal"):
                raise MetaProviderError("refusal")
            if choice.get("finish_reason") != "stop":
                raise MetaProviderError("incomplete_generation")
            if message.get("role") != "assistant" or message.get("tool_calls"):
                raise MetaProviderError("invalid_envelope")
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise MetaProviderError("empty_content")
            usage = envelope.get("usage")
            if usage is not None:
                keys = ("prompt_tokens", "completion_tokens", "total_tokens")
                if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in keys)
                        or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
                    raise MetaProviderError("invalid_usage")
                trace["usage"] = {k: usage[k] for k in keys}
                if (usage["completion_tokens"] > self.settings.meta_max_output_tokens
                        or usage["total_tokens"] > trace["reservation_tokens_per_attempt"]):
                    self._budget_blocked = True
                    raise MetaProviderError("usage_limit_breach")
            return self.sanitize(content)
        except MetaProviderError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            raise MetaProviderError("invalid_envelope") from None

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

