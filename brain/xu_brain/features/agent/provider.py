"""Provider clients — OpenAI-compatible and Anthropic-compatible, normalized
to a single OpenAI tool-calling loop.

Internal message/tool format is OpenAI Chat Completions style:

    message = {"role": "user"|"assistant"|"tool"|"system", "content": str,
               "tool_calls": [{"id","type":"function","function":{"name","arguments"}}]}
    tool_result message = {"role":"tool","tool_call_id":id,"content": str}

Both providers expose the same :meth:`chat_stream` async generator yielding
:class:`StreamEvent` (delta text / tool_call / stop). The Anthropic client
translates its native content-blocks stream into the OpenAI shape so the agent
loop is provider-agnostic.

Keys live in the OS keychain (Rust ``keyring``); the brain asks the shell for
a key via the ``keychain.get`` RPC when it needs one. Until the shell wires
that, keys may also be supplied via ``XU_KEY_<ID>`` env (dev escape hatch) —
never persisted to disk.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Protocol

import httpx


# ---------------------------------------------------------------------------
# Public shapes
# ---------------------------------------------------------------------------


class StopReason(Enum):
    STOP = "stop"
    TOOL = "tool_calls"
    LENGTH = "length"
    ERROR = "error"


def _map_stop_reason(raw: Any) -> StopReason:
    """Tolerant finish_reason → StopReason mapping. Upstreams send values
    outside our enum ("content_filter", "function_call", …); raising on those
    killed the whole stream parser and discarded buffered tool_calls."""
    try:
        return StopReason(str(raw or "stop"))
    except ValueError:
        return StopReason.STOP


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string; loop parses


@dataclass
class StreamEvent:
    """One chunk from the normalized stream."""

    delta: str = ""
    reasoning: str = ""
    reasoning_signature: str = ""  # Anthropic signature_delta (multi-turn thinking continuity)
    tool_call: ToolCall | None = None
    stop_reason: StopReason | None = None
    usage: dict[str, int] | None = None
    error: str | None = None
    retryable: bool = False  # True for transient/network/5xx; False for 4xx request errors


@dataclass
class Provider:
    id: str
    type: str  # "openai-compatible" | "anthropic-compatible"
    base_url: str
    models: list[str] = field(default_factory=list)
    key_set: bool = False  # key present in keychain/env (never the key itself)
    name: str = ""  # user-visible label (defaults to id.upper() if empty)
    enabled: bool = True  # disabled providers are excluded from routing

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id.upper(),
            "type": self.type,
            "base_url": self.base_url,
            "models": list(self.models),
            "key_set": self.key_set,
            "enabled": self.enabled,
        }


class ProviderError(Exception):
    pass


class KeychainLike(Protocol):
    async def get_key(self, provider_id: str) -> str | None: ...

_CB_THRESHOLD = 3  # consecutive transient failures before opening the breaker
_CB_COOLDOWN = 30.0  # seconds to skip a tripped provider before retrying



# ---------------------------------------------------------------------------
# Provider registry + chain resolution
# ---------------------------------------------------------------------------


class ProviderManager:
    """Persisted provider registry. Keys stay in the shell keychain.

    Brain ↔ shell key contract: when a key is needed, the manager calls
    ``await keychain.get_key(provider.id)``. The shell implementation (Rust
    ``keyring``) resolves it; a dev env override ``XU_KEY_<ID>`` (uppercase id)
    is honored first so the brain is testable without the shell.
    """

    def __init__(self, data_home, keychain: KeychainLike | None = None) -> None:
        self._file = data_home / "providers.json"
        self._providers: dict[str, Provider] = {}
        self._dev_keys: dict[str, str] = {}
        self.keychain = keychain
        # Shared connection-pooled client (HTTP/1.1 keep-alive; HTTP/2 where
        # supported).  Reused across turns, retries, and compress requests so
        # we skip per-request TCP+TLS handshakes.  Read timeout is large so a
        # long-thinking stream isn't killed mid-token; an idle stream is
        # bounded by the cooperative stop signal instead.
        try:
            import h2  # noqa: F401
            _http2 = True
        except ImportError:
            _http2 = False  # h2 not installed — fall back to HTTP/1.1
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            http2=_http2,
        )
        # Circuit breaker: {provider_id: {"failures": N, "open_until": ts}}.
        # After _CB_THRESHOLD consecutive transient failures the provider is
        # skipped for _COOLDOWN seconds before being retried.
        self._breakers: dict[str, dict[str, float]] = {}
        self.load()

    def load(self) -> None:
        if not self._file.exists():
            return
        try:
            raw = json.loads(self._file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in raw:
            known = {k: v for k, v in item.items() if k in Provider.__dataclass_fields__}
            pid = known.get("id", "")
            self._providers[pid] = Provider(**known)
            # Dev-only key persistence: restore keys saved when no shell
            # keychain was present. Never exposed via to_dict / RPC.
            dev_key = item.get("_dev_key")
            if isinstance(dev_key, str) and dev_key:
                self._dev_keys[pid] = dev_key

    def save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        items = []
        for p in self._providers.values():
            d = p.to_dict()
            # In dev (no shell keychain), persist the key so it survives
            # brain restarts; production shells keep keys in the keychain.
            if self.keychain is None and p.id in self._dev_keys:
                d["_dev_key"] = self._dev_keys[p.id]
            items.append(d)
        self._file.write_text(json.dumps(items, indent=2), "utf-8")
        try:
            self._file.chmod(0o600)
        except OSError:
            pass

    def upsert(self, provider: Provider, api_key: str | None = None) -> Provider:
        existing = self._providers.get(provider.id)
        if api_key:
            provider.key_set = True
            self._store_key(provider.id, api_key)
        elif existing is not None:
            # Keep prior key presence + models unless the caller overwrote them.
            provider.key_set = existing.key_set or provider.key_set
            if not provider.models and existing.models:
                provider.models = list(existing.models)
            if not provider.name and existing.name:
                provider.name = existing.name
            provider.enabled = existing.enabled
        self._providers[provider.id] = provider
        self.save()
        return provider

    def delete(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)
        self._dev_keys.pop(provider_id, None)
        self.save()

    def list(self) -> list[Provider]:
        return list(self._providers.values())

    def get(self, provider_id: str) -> Provider | None:
        return self._providers.get(provider_id)

    def find_model(self, model: str, provider_id: str | None = None) -> tuple[Provider, str] | None:
        """Locate the enabled provider owning a model id.

        When ``provider_id`` is given (an explicit UI pick), resolve against
        that provider only so two providers exposing the same model id route
        correctly. Disabled providers are never selected."""
        if provider_id:
            # Accept the provider id OR its display name, case-insensitive:
            # older shells pinned the display name (e.g. "Mooxy") while the
            # id is lowercase ("mooxy") — both must resolve.
            key = provider_id.lower()
            p = next(
                (c for c in self._providers.values()
                 if c.id.lower() == key or (c.name or "").lower() == key),
                None,
            )
            if p and p.enabled and model in p.models:
                return p, model
            return None
        for p in self._providers.values():
            if p.enabled and model in p.models:
                return p, model
        return None

    def resolve(self, model: str | None, provider_id: str | None = None) -> tuple[Provider, str] | None:
        """Resolve a model id (+ optional explicit provider) to ``(provider, model)``.

        No explicit model → first *enabled* provider with a detected model list
        so a fresh session Just Works. Returns None only when no enabled
        provider has models (all disabled or none configured)."""
        if model:
            found = self.find_model(model, provider_id)
            if found:
                return found
            # An explicitly requested model that no enabled provider owns must
            # not silently fall back to a different provider's model.
            return None
        for p in self._providers.values():
            if p.enabled and p.models:
                return p, p.models[0]
        return None

    # ---- circuit breaker ----

    def _breaker_open(self, provider_id: str) -> bool:
        """True if the provider is currently tripped and still in cooldown."""
        st = self._breakers.get(provider_id)
        if not st:
            return False
        import time as _t
        if _t.time() < st.get("open_until", 0):
            return True
        # cooldown elapsed — half-open: let the next call through
        return False

    def _breaker_record_failure(self, provider_id: str) -> None:
        st = self._breakers.setdefault(provider_id, {"failures": 0, "open_until": 0})
        st["failures"] = st.get("failures", 0) + 1
        if st["failures"] >= _CB_THRESHOLD:
            import time as _t
            st["open_until"] = _t.time() + _CB_COOLDOWN

    def _breaker_record_success(self, provider_id: str) -> None:
        self._breakers.pop(provider_id, None)

    def reset_breakers(self) -> None:
        """Clear all provider breakers (called at the start of each turn)."""
        self._breakers.clear()

    # ---- key access ----

    def _store_key(self, provider_id: str, key: str) -> None:
        """Keys are owned by the shell keychain; the brain never sends them over RPC.

        In dev (no shell keychain) the key is held in memory and mirrored into
        ``providers.json`` (mode 0600) by :meth:`save` so it survives restarts."""
        self._dev_keys[provider_id] = key

    async def get_key(self, provider_id: str) -> str | None:
        env = os.environ.get(f"XU_KEY_{provider_id.upper()}")
        if env:
            return env
        if provider_id in self._dev_keys:
            return self._dev_keys[provider_id]
        if self.keychain is not None:
            return await self.keychain.get_key(provider_id)
        return None

    # ---- connectivity + model fetch ----

    async def fetch_models(self, provider: Provider) -> tuple[bool, int, list[str], str | None]:
        """Returns (ok, latency_ms, models, error).

        Localhost still receives a key when one is set — many local gateways
        (Mooxy, OpenWebUI proxies, auth-wrapped Ollama) require it. Only the
        *absence* of a key is treated as optional for loopback hosts.
        """
        url = provider.base_url.rstrip("/")
        is_anthropic = provider.type == "anthropic-compatible"
        # OpenAI-compatible base_url already includes /v1 (…/v1); Anthropic
        # base is typically the host root so we append /v1/models.
        models_url = url + ("/v1/models" if is_anthropic else "/models")
        key = await self.get_key(provider.id)
        headers: dict[str, str] = {"Accept": "application/json"}
        if key:
            if is_anthropic:
                headers["x-api-key"] = key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {key}"
                # Some local OpenAI-compat gateways only accept X-API-Key.
                headers["X-API-Key"] = key
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(models_url, headers=headers)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = exc.response.text[:240]
            except Exception:
                pass
            detail = f"HTTP {exc.response.status_code}"
            if body:
                detail = f"{detail}: {body}"
            return False, 0, [], detail
        except httpx.HTTPError as exc:
            return False, 0, [], str(exc)
        latency = int((time.perf_counter() - t0) * 1000)
        try:
            data = resp.json()
        except ValueError:
            return False, latency, [], "models endpoint returned non-JSON"
        models = _extract_model_ids(data, is_anthropic)
        return True, latency, models, None

    # ---- chat streaming ----

    async def chat_stream(
        self,
        provider: Provider,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
        signal: asyncio.Event | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if self._breaker_open(provider.id):
            yield StreamEvent(
                error=f"provider '{provider.id}' circuit breaker open (cooldown)",
                stop_reason=StopReason.ERROR,
                retryable=False,
            )
            return
        had_transient = False
        if provider.type == "anthropic-compatible":
            async for ev in _anthropic_stream(
                provider, model, messages, tools, max_tokens, self, signal,
                client=self._client,
            ):
                if ev.error and ev.retryable:
                    had_transient = True
                yield ev
        else:
            async for ev in _openai_stream(
                provider, model, messages, tools, max_tokens, self, signal,
                client=self._client,
            ):
                if ev.error and ev.retryable:
                    had_transient = True
                yield ev
        # Breaker bookkeeping: a transient failure bumps the count; a clean
        # completion (even a stop) resets it.
        if had_transient:
            self._breaker_record_failure(provider.id)
        else:
            self._breaker_record_success(provider.id)




def _tidy_upstream_error(status: int, raw: str) -> str:
    """Reduce an upstream error body to one readable line.

    Walks nested ``{"error": …}`` / ``{"message": …}`` shells; on truncated or
    non-JSON bodies, falls back to a regex for the deepest ``"message"``.
    """
    import re as _re

    body: str = raw.strip()
    for _ in range(6):
        candidate = body
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            brace = candidate.find("{")
            if brace < 0:
                break
            try:
                parsed = json.loads(candidate[brace:])
            except (json.JSONDecodeError, ValueError):
                break
        if not isinstance(parsed, dict):
            break
        err = parsed.get("error")
        if isinstance(err, str):
            body = err
            continue
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            body = err["message"]
            continue
        if isinstance(parsed.get("message"), str):
            body = parsed["message"]
            continue
        break
    if '"message"' in body:
        msgs = _re.findall(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"?', body)
        if msgs:
            body = msgs[-1]
    if len(body) > 280:
        body = body[:280] + "…"
    return f"{status} {body}"


def _mid_stream_error(err: Any) -> str:
    """Readable text for an error a gateway injected into an SSE chunk.

    Accepts the shapes seen in the wild: a bare string, ``{"message": …}``, or a
    nested ``{"error": {"message": …}}``.
    """
    for _ in range(4):
        if isinstance(err, dict):
            err = err.get("message") or err.get("error") or str(err)
            continue
        break
    text = str(err or "upstream stream error").strip()
    return text[:280] + "…" if len(text) > 280 else text


def _extract_model_ids(data: Any, anthropic: bool) -> list[str]:
    if anthropic:
        data = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for m in data:
        if isinstance(m, dict):
            mid = m.get("id") or m.get("name")
            if mid:
                out.append(mid)
        elif isinstance(m, str):
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# OpenAI-compatible streaming client
# ---------------------------------------------------------------------------


async def _openai_stream(
    provider: Provider,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int | None,
    mgr: ProviderManager,
    signal: asyncio.Event | None,
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[StreamEvent]:
    url = provider.base_url.rstrip("/") + "/chat/completions"
    key = await mgr.get_key(provider.id)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-API-Key"] = key
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
    if max_tokens:
        body["max_tokens"] = max_tokens
    # Buffer by call id, not index: some relays reuse index 0 for parallel calls.
    tool_buffers: dict[int | str, dict[str, str]] = {}
    call_keys: dict[int, int | str] = {}  # stream index -> current buffer key
    _owns = client is None
    if _owns:
        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0))
    try:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                raw = (await resp.aread()).decode("utf-8", "replace")
                transient = resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500
                yield StreamEvent(error=_tidy_upstream_error(resp.status_code, raw), stop_reason=StopReason.ERROR, retryable=transient)
                return
            async for line in resp.aiter_lines():
                if signal is not None and signal.is_set():
                    yield StreamEvent(stop_reason=StopReason.STOP)
                    return
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    # Some gateways emit a stray non-JSON chunk; skipping it is
                    # better than aborting a stream that is otherwise fine.
                    continue
                # A gateway that dies mid-stream cannot fail the HTTP request —
                # it already sent 200 and started streaming. Several (Mooxy,
                # OpenRouter, LiteLLM) instead append a chunk carrying a
                # top-level "error" alongside finish_reason:"stop". That key is
                # not in the OpenAI schema, so reading only `choices` turned a
                # dropped socket into a clean stop: no banner, no retry, the
                # turn just ended early and the user had to say "continue".
                if chunk.get("error"):
                    yield StreamEvent(
                        error=_mid_stream_error(chunk["error"]),
                        stop_reason=StopReason.ERROR,
                        retryable=True,  # a mid-stream drop is transient by definition
                    )
                    return
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta", {})
                think = delta.get("reasoning_content") or delta.get("reasoning")
                if think:
                    yield StreamEvent(reasoning=think)
                if "content" in delta and delta["content"]:
                    yield StreamEvent(delta=delta["content"])
                tc_list = delta.get("tool_calls")
                if tc_list:
                    for tc in tc_list:
                        idx = tc.get("index", 0)
                        # Some upstreams (twohub-style relays) reuse index 0
                        # for parallel calls. A new call is signalled by a
                        # (new) id at the same index; continuation chunks
                        # carry no id and must land in the current buffer.
                        # Keying on index alone merges two calls into one
                        # with invalid JSON arguments, which poisons every
                        # later request in the turn (upstream answers 200 +
                        # empty stream).
                        tc_id = tc.get("id")
                        if tc_id:
                            # A (new) id at this index starts a fresh buffer and
                            # becomes the target for the continuation chunks that
                            # follow at the same index.
                            key = tc_id
                            tool_buffers[key] = {"id": tc_id, "name": "", "args": ""}
                        else:
                            # No id: a continuation of whatever call currently
                            # owns this index.
                            key = call_keys.get(idx, idx)
                        call_keys[idx] = key
                        buf = tool_buffers.setdefault(key, {"id": "", "name": "", "args": ""})
                        fn = tc.get("function", {})
                        if tc.get("id"):
                            buf["id"] = tc["id"]
                        if fn.get("name"):
                            buf["name"] = fn["name"]
                        if fn.get("arguments"):
                            buf["args"] += fn["arguments"]
                if choice.get("finish_reason"):
                    for buf in tool_buffers.values():
                        if not buf["id"]:
                            continue
                        args = buf["args"]
                        if args:
                            try:
                                json.loads(args)
                            except ValueError:
                                # Merged/corrupt arguments must never reach a
                                # tool or the conversation history: relays
                                # reject such requests with an empty stream and
                                # the whole turn poisons.
                                continue
                        yield StreamEvent(
                            tool_call=ToolCall(
                                id=buf["id"], name=buf["name"], arguments=args
                            )
                        )
                    yield StreamEvent(stop_reason=_map_stop_reason(choice["finish_reason"]))
                if chunk.get("usage"):
                    yield StreamEvent(usage=chunk["usage"])
    except httpx.HTTPError as exc:
        yield StreamEvent(error=str(exc), stop_reason=StopReason.ERROR, retryable=True)
    finally:
        if _owns and not client.is_closed:
            await client.aclose()


# ---------------------------------------------------------------------------
# Anthropic-compatible streaming client (normalized to OpenAI shape)
# ---------------------------------------------------------------------------


async def _anthropic_stream(
    provider: Provider,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int | None,
    mgr: ProviderManager,
    signal: asyncio.Event | None,
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[StreamEvent]:
    url = provider.base_url.rstrip("/") + "/v1/messages"
    key = await mgr.get_key(provider.id)
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "Accept": "text/event-stream",
    }
    if key:
        headers["x-api-key"] = key

    sys_msgs = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    convo, tool_results = _anthropic_from_openai(messages)
    body: dict[str, Any] = {
        "model": model,
        "messages": convo,
        "max_tokens": max_tokens or 4096,
        "stream": True,
    }
    if sys_msgs:
        body["system"] = "\n\n".join(str(s) for s in sys_msgs)
    if tools:
        body["tools"] = [_anthropic_tool(t) for t in tools]
    if tool_results:
        # Anthropic wants tool_result blocks on a user turn; we already folded
        # them into `convo` via _anthropic_from_openai.
        pass

    cur_tool: dict[str, str] | None = None
    _owns = client is None
    if _owns:
        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0))
    try:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                raw = (await resp.aread()).decode("utf-8", "replace")
                transient = resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500
                yield StreamEvent(error=_tidy_upstream_error(resp.status_code, raw), stop_reason=StopReason.ERROR, retryable=transient)
                return
            event_type = ""
            start_usage: dict[str, Any] = {}
            async for line in resp.aiter_lines():
                if signal is not None and signal.is_set():
                    yield StreamEvent(stop_reason=StopReason.STOP)
                    return
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                # Some OpenAI-shaped gateways proxying Anthropic append [DONE].
                # Stop reading, but never yield a stop reason here: message_delta
                # may already have reported TOOL and the loop keeps the last one.
                if payload == "[DONE]":
                    break
                if not payload:
                    continue
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    # Some gateways emit a stray non-JSON chunk; skipping it is
                    # better than aborting a stream that is otherwise fine.
                    continue
                t = chunk.get("type", event_type)
                # Anthropic's own mid-stream failure shape (`event: error`), plus
                # the same top-level "error" a gateway may inject. Without this
                # the event fell through every `t == …` branch and was ignored,
                # ending the turn as a silent stop.
                if t == "error" or chunk.get("error"):
                    yield StreamEvent(
                        error=_mid_stream_error(chunk.get("error") or chunk),
                        stop_reason=StopReason.ERROR,
                        retryable=True,
                    )
                    return
                if t == "message_start":
                    start_usage = (chunk.get("message") or {}).get("usage") or chunk.get("usage") or {}
                if t == "content_block_start":
                    block = chunk.get("content_block", {})
                    if block.get("type") == "tool_use":
                        cur_tool = {"id": block.get("id", ""), "name": block.get("name", ""), "args": ""}
                elif t == "content_block_delta":
                    delta = chunk.get("delta", {})
                    if delta.get("type") == "thinking_delta":
                        yield StreamEvent(reasoning=delta.get("thinking", ""))
                    elif delta.get("type") == "signature_delta":
                        yield StreamEvent(reasoning_signature=delta.get("signature", ""))
                    elif delta.get("type") == "text_delta":
                        yield StreamEvent(delta=delta.get("text", ""))
                    elif delta.get("type") == "input_json_delta" and cur_tool is not None:
                        cur_tool["args"] += delta.get("partial_json", "")
                elif t == "content_block_stop" and cur_tool is not None:
                    yield StreamEvent(
                        tool_call=ToolCall(
                            id=cur_tool["id"], name=cur_tool["name"], arguments=cur_tool["args"]
                        )
                    )
                    cur_tool = None
                elif t == "message_delta":
                    d = chunk.get("delta", {})
                    if d.get("stop_reason"):
                        reason = d["stop_reason"]
                        yield StreamEvent(
                            stop_reason=StopReason.TOOL if reason == "tool_use" else StopReason.STOP
                        )
                    # Anthropic splits usage across events: message_start has the
                    # input/cache counts, the final message_delta only output.
                    # Merge + normalize to OpenAI keys so the turn loop's
                    # token-meter anchor gets real prompt totals.
                    merged = {**start_usage, **(chunk.get("usage") or {})}
                    if merged:
                        inp = int(merged.get("input_tokens") or merged.get("prompt_tokens") or 0) \
                            + int(merged.get("cache_read_input_tokens") or 0) \
                            + int(merged.get("cache_creation_input_tokens") or 0)
                        out = int(merged.get("output_tokens") or merged.get("completion_tokens") or 0)
                        yield StreamEvent(usage={
                            "prompt_tokens": inp,
                            "completion_tokens": out,
                            "total_tokens": inp + out,
                        })
    except httpx.HTTPError as exc:
        yield StreamEvent(error=str(exc), stop_reason=StopReason.ERROR, retryable=True)
    finally:
        if _owns and not client.is_closed:
            await client.aclose()


def _anthropic_from_openai(
    messages: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert OpenAI-style history to Anthropic messages.

    - system messages are stripped (caller sets ``system``).
    - assistant tool_calls → Anthropic tool_use content blocks.
    - role=tool messages → user tool_result blocks.
    Consecutive user blocks are merged (Anthropic disallows user→user).
    """
    convo: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": _coerce_text(m.get("content")),
                }
            )
            continue
        # Flush pending tool results onto a user turn before any non-tool message.
        if tool_results and role != "tool":
            convo.append({"role": "user", "content": tool_results})
            tool_results = []
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            # Anthropic extended thinking: prior thinking blocks must be echoed
            # back with their original signature for multi-turn continuity.
            rtext = m.get("reasoning_content") or m.get("reasoning") or ""
            rsig = m.get("reasoning_signature", "")
            if rtext and rsig:
                blocks.append({"type": "thinking", "thinking": _coerce_text(rtext), "signature": rsig})
            if m.get("content"):
                blocks.append({"type": "text", "text": _coerce_text(m["content"])})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    inp = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    inp = {}
                blocks.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""), "input": inp})
            convo.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif role == "user":
            convo.append({"role": "user", "content": _coerce_user_content(m.get("content"))})
    if tool_results:
        convo.append({"role": "user", "content": tool_results})
    return convo, []


def _coerce_user_content(v: Any) -> Any:
    """OpenAI content → Anthropic content list. Strings pass through (kept as
    strings for the common text-only path); OpenAI image_url parts become
    Anthropic image blocks so attached images survive the provider swap."""
    if isinstance(v, str):
        return v
    if not isinstance(v, list):
        return _coerce_text(v)
    blocks: list[dict[str, Any]] = []
    for part in v:
        if not isinstance(part, dict):
            blocks.append({"type": "text", "text": _coerce_text(part)})
            continue
        img = part.get("image_url") or part.get("image")
        if isinstance(img, dict):
            url = img.get("url") or ""
            if url.startswith("data:"):
                # data:image/png;base64,<b64>
                media_type, _, b64 = url.partition(",")
                mime = media_type[len("data:"):].split(";")[0] or "image/png"
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            else:
                blocks.append({"type": "text", "text": _coerce_text(part.get("text", ""))})
        elif part.get("type") == "text" or "text" in part:
            blocks.append({"type": "text", "text": _coerce_text(part["text"])})
        else:
            blocks.append({"type": "text", "text": _coerce_text(part)})
    return blocks or [{"type": "text", "text": ""}]


def _coerce_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)


def _anthropic_tool(openai_tool: dict[str, Any]) -> dict[str, Any]:
    fn = openai_tool.get("function", openai_tool)
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }
