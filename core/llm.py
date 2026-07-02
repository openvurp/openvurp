"""
openvurp Core — LLM Client

Streaming, failover, model routing, multi-backend.
Function calling nativo per OpenAI/Anthropic/Groq, fallback regex per Ollama.
Cache integrata per evitare chiamate duplicate.
"""

from __future__ import annotations

import json
import time
import sys
import uuid
from dataclasses import dataclass, field
from typing import Iterator, Optional


class LLMError(Exception):
    """Errore LLM con classificazione."""
    def __init__(self, message: str, retryable: bool = False, backend: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.backend = backend


@dataclass
class ToolCall:
    """Tool call normalizzato (uguale per tutti i backend)."""
    id: str
    name: str
    args: dict

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "args": self.args}


@dataclass
class LLMResponse:
    """Risposta normalizzata dal LLM."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    raw: object = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class LLMClient:
    def __init__(self, backend: str, model: str, **kwargs):
        self.backend = backend
        self.model = model
        self.api_key = kwargs.get("api_key", "")
        self.base_url = kwargs.get("base_url", "")
        self.temperature = kwargs.get("temperature", 0.7)
        # Temperatura più bassa quando il modello deve chiamare tool:
        # il tool-calling è più affidabile con sampling meno creativo.
        self.tool_temperature = kwargs.get("tool_temperature", min(float(kwargs.get("temperature", 0.7)), 0.2))
        self.max_tokens = kwargs.get("max_tokens", 8192)
        # think: None = lascia decidere al modello; False = disabilita il
        # "thinking" (Ollama). Serve ai modelli reasoning (es. nemotron) usati
        # come guardiani veloci: senza questo il pensiero consuma num_predict e
        # `content` torna VUOTO → il chiamante crede che il modello taccia.
        self.think = kwargs.get("think", None)
        self.fallback_backend = kwargs.get("fallback_backend", "")
        self.fallback_model = kwargs.get("fallback_model", "")

        # Cache (opzionale, impostata dall'agent)
        self._cache = None
        self._ollama_tools_supported: bool | None = None

        self._client = None
        self._init_client()

    @property
    def supports_function_calling(self) -> bool:
        """Questo backend supporta function calling nativo?"""
        if self.backend == "ollama":
            return self._ollama_tools_supported is not False
        return self.backend in ("openai", "openai_compatible", "anthropic", "groq")

    def _init_client(self):
        """Inizializza il client per il backend."""
        b = self.backend

        if b == "ollama":
            import requests
            self._requests = requests
        elif b in ("openai", "openai_compatible"):
            from openai import OpenAI
            kw = {}
            if self.api_key:
                kw["api_key"] = self.api_key
            if self.base_url:
                kw["base_url"] = self.base_url
            self._client = OpenAI(**kw)
        elif b == "anthropic":
            import anthropic
            kw = {}
            if self.api_key:
                kw["api_key"] = self.api_key
            self._client = anthropic.Anthropic(**kw)
        elif b == "groq":
            from groq import Groq
            kw = {}
            if self.api_key:
                kw["api_key"] = self.api_key
            self._client = Groq(**kw)

    # ── Chiamate semplici (senza tool — backward compatible) ──

    def call(self, messages: list[dict], **_kwargs) -> str:
        """Chiamata sincrona — restituisce testo completo.

        `_kwargs` è accettato per compatibilità con call site più vecchi
        che passano opzioni come `thinking_level`.
        """
        # Check cache
        if self._cache:
            cached = self._cache.get(messages, self.model)
            if cached is not None:
                return cached

        try:
            text = self._do_call(self.backend, self.model, messages)
        except LLMError as e:
            if e.retryable and self.fallback_backend:
                text = self._do_call(self.fallback_backend, self.fallback_model, messages)
            else:
                raise

        # Salva in cache
        if self._cache and text:
            self._cache.put(messages, text, self.model)

        return text

    def call_with_timing(self, messages: list[dict], **kwargs) -> tuple[str, int, int, int]:
        """Chiamata con timing. Returns (text, duration_ms, input_tokens_est, output_tokens_est)."""
        start = time.time()
        text = self.call(messages, **kwargs)
        duration = int((time.time() - start) * 1000)

        input_tokens = self._estimate_input_tokens(messages)
        output_tokens = len(text) // 4

        return text, duration, input_tokens, output_tokens

    # ── Function calling nativo ──

    def call_with_tools(self, messages: list[dict],
                        tools_schema: list[dict]) -> LLMResponse:
        """Chiamata con function calling nativo.

        Args:
            messages: Lista messaggi in formato neutro openvurp
            tools_schema: Schema tool nel formato del backend

        Returns:
            LLMResponse con testo e tool_calls normalizzati
        """
        try:
            if self.backend in ("openai", "openai_compatible", "groq"):
                return self._call_openai_tools(self.model, messages, tools_schema)
            elif self.backend == "anthropic":
                return self._call_anthropic_tools(self.model, messages, tools_schema)
            elif self.backend == "ollama":
                return self._call_ollama_tools(self.model, messages, tools_schema)
            else:
                # Ollama: niente function calling, ritorna solo testo
                text = self._do_call(self.backend, self.model, messages)
                return LLMResponse(text=text)
        except LLMError:
            raise
        except Exception as e:
            retryable = self._is_retryable(e)
            raise LLMError(str(e), retryable=retryable, backend=self.backend)

    def call_with_tools_timed(self, messages: list[dict],
                              tools_schema: list[dict]) -> tuple[LLMResponse, int, int, int]:
        """Function calling con timing."""
        start = time.time()
        response = self.call_with_tools(messages, tools_schema)
        duration = int((time.time() - start) * 1000)

        input_tokens, output_tokens = self._extract_usage(response.raw)
        if input_tokens is None:
            input_tokens = self._estimate_input_tokens(messages)
        if output_tokens is None:
            output_tokens = len(response.text) // 4 + sum(
                len(json.dumps(tc.args)) // 4 for tc in response.tool_calls
            )

        return response, duration, input_tokens, output_tokens

    # ── Function calling con streaming ──

    def call_with_tools_streamed(self, messages: list[dict],
                                 tools_schema: list[dict],
                                 on_text=None) -> LLMResponse:
        """Come call_with_tools, ma streamma il testo via callback on_text(delta).

        I tool call vengono accumulati e restituiti normalizzati a fine stream.
        Se lo streaming fallisce prima di emettere testo, fa fallback
        trasparente alla chiamata non-streamed.
        """
        emitted = {"n": 0}

        def _emit(delta: str):
            if delta:
                emitted["n"] += 1
                if on_text:
                    on_text(delta)

        try:
            if self.backend in ("openai", "openai_compatible", "groq"):
                return self._stream_openai_tools(self.model, messages, tools_schema, _emit)
            elif self.backend == "anthropic":
                return self._stream_anthropic_tools(self.model, messages, tools_schema, _emit)
            elif self.backend == "ollama":
                return self._stream_ollama_tools(self.model, messages, tools_schema, _emit)
            return self.call_with_tools(messages, tools_schema)
        except LLMError:
            raise
        except Exception as e:
            if emitted["n"] == 0:
                # Nessun output ancora mostrato: riprova senza streaming.
                return self.call_with_tools(messages, tools_schema)
            raise LLMError(str(e), retryable=self._is_retryable(e), backend=self.backend)

    def call_with_tools_streamed_timed(self, messages: list[dict],
                                       tools_schema: list[dict],
                                       on_text=None) -> tuple[LLMResponse, int, int, int]:
        """Streaming con timing. Returns (response, duration_ms, tok_in, tok_out)."""
        start = time.time()
        response = self.call_with_tools_streamed(messages, tools_schema, on_text=on_text)
        duration = int((time.time() - start) * 1000)

        input_tokens, output_tokens = self._extract_usage(response.raw)
        if input_tokens is None:
            input_tokens = self._estimate_input_tokens(messages)
        if output_tokens is None:
            output_tokens = len(response.text) // 4 + sum(
                len(json.dumps(tc.args)) // 4 for tc in response.tool_calls
            )

        return response, duration, input_tokens, output_tokens

    def _stream_anthropic_tools(self, model: str, messages: list[dict],
                                tools_schema: list[dict], on_text) -> LLMResponse:
        system, ant_messages = self._to_anthropic_messages(messages)

        kwargs = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": ant_messages,
            "temperature": self.tool_temperature if tools_schema else self.temperature,
        }
        if system:
            kwargs["system"] = self._anthropic_system_param(system)
        if tools_schema:
            kwargs["tools"] = self._anthropic_cached_tools(tools_schema)

        with self._client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                on_text(text)
            final = stream.get_final_message()

        text = ""
        tool_calls = []
        for block in final.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    args=block.input if isinstance(block.input, dict) else {},
                ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=final.stop_reason or "",
            raw=final,
        )

    def _stream_openai_tools(self, model: str, messages: list[dict],
                             tools_schema: list[dict], on_text) -> LLMResponse:
        oai_messages = self._to_openai_messages(messages)

        kwargs = {
            "model": model,
            "messages": oai_messages,
            "temperature": self.tool_temperature if tools_schema else self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if tools_schema:
            kwargs["tools"] = tools_schema
        if self.backend == "openai":
            kwargs["stream_options"] = {"include_usage": True}

        stream = self._client.chat.completions.create(**kwargs)

        text = ""
        finish = ""
        usage = None
        acc: dict[int, dict] = {}

        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                text += delta.content
                on_text(delta.content)
            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason

        tool_calls = []
        for idx in sorted(acc):
            slot = acc[idx]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                args = {"_raw": slot["args"]}
            tool_calls.append(ToolCall(
                id=slot["id"] or f"call_{idx}_{uuid.uuid4().hex[:8]}",
                name=slot["name"],
                args=args if isinstance(args, dict) else {"_raw": args},
            ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=finish,
            raw=usage,
        )

    def _ollama_post(self, url: str, payload: dict, timeout, stream: bool = False,
                     retries: int = 2, backoff: float = 1.5):
        """POST verso Ollama con piccoli retry sugli errori di connessione:
        se il server sta riavviando non si butta via il turno per un hiccup.
        I Timeout NON si ritentano (il modello può essere solo lento)."""
        import time as _time
        last_exc = None
        for attempt in range(retries + 1):
            try:
                r = self._requests.post(url, json=payload, timeout=timeout,
                                        stream=stream)
                r.raise_for_status()
                return r
            except self._requests.exceptions.ConnectionError as e:
                last_exc = e
                if attempt < retries:
                    _time.sleep(backoff * (attempt + 1))
        raise last_exc

    def _stream_ollama_tools(self, model: str, messages: list[dict],
                             tools_schema: list[dict], on_text) -> LLMResponse:
        payload = {
            "model": model,
            "messages": self._to_ollama_messages(messages),
            "stream": True,
            "options": {
                "temperature": self.tool_temperature if tools_schema else self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        if self.think is not None:
            payload["think"] = self.think
        if tools_schema:
            payload["tools"] = tools_schema

        base = self.base_url or "http://localhost:11434"
        timeout = getattr(self, "_ollama_timeout", 120)

        try:
            r = self._ollama_post(f"{base}/api/chat", payload, timeout, stream=True)
        except self._requests.exceptions.Timeout:
            raise LLMError(
                f"Ollama timeout dopo {timeout}s — il modello è troppo lento o il contesto è troppo grande. "
                f"Prova a ridurre la conversazione o usa un modello più leggero.",
                retryable=False, backend="ollama"
            )
        except self._requests.exceptions.ConnectionError:
            raise LLMError(
                "Ollama non raggiungibile — assicurati che sia in esecuzione.",
                retryable=True, backend="ollama"
            )
        except self._requests.exceptions.HTTPError as e:
            response = getattr(e, "response", None)
            if response is not None and response.status_code in (400, 404):
                # Server vecchio: niente tool/streaming, usa il path classico.
                self._ollama_tools_supported = False
                return self.call_with_tools(messages, tools_schema)
            raise

        self._ollama_tools_supported = True

        text = ""
        stop_reason = ""
        raw_tool_calls: list = []
        last_data: dict = {}

        for line in r.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            last_data = data if isinstance(data, dict) else {}
            message = last_data.get("message", {}) or {}
            token = message.get("content", "")
            if token:
                text += token
                on_text(token)
            for tc in message.get("tool_calls", []) or []:
                raw_tool_calls.append(tc)
            if last_data.get("done"):
                stop_reason = last_data.get("done_reason", "")

        tool_calls = []
        for idx, tc in enumerate(raw_tool_calls):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_raw": args}
            if not name:
                continue
            tool_calls.append(ToolCall(
                id=f"ollama_{idx}_{uuid.uuid4().hex[:8]}",
                name=name,
                args=args,
            ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw=last_data,
        )

    # ── Conversione messaggi formato neutro → backend ──

    def _to_openai_messages(self, messages: list[dict]) -> list[dict]:
        """Converte messaggi openvurp → formato OpenAI."""
        result = []
        for m in messages:
            role = m.get("role", "user")

            if role == "tool_result":
                result.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                })
            elif m.get("tool_calls"):
                msg = {
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["args"], ensure_ascii=False),
                            }
                        }
                        for tc in m["tool_calls"]
                    ]
                }
                result.append(msg)
            else:
                result.append({"role": role, "content": m.get("content", "")})
        return result

    def _to_anthropic_messages(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """Converte messaggi openvurp → formato Anthropic.

        Returns: (system_prompt, messages)
        Anthropic richiede alternanza user/assistant e tool_result in user msg.
        """
        system = ""
        result = []
        pending_tool_results = []

        for m in messages:
            role = m.get("role", "user")

            if role == "system":
                system = m.get("content", "")
                continue

            if role == "tool_result":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                })
                continue

            # Flush pending tool results prima del prossimo messaggio non-tool
            if pending_tool_results:
                result.append({"role": "user", "content": pending_tool_results})
                pending_tool_results = []

            if m.get("tool_calls"):
                content = []
                text = m.get("content", "")
                if text:
                    content.append({"type": "text", "text": text})
                for tc in m["tool_calls"]:
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["args"],
                    })
                result.append({"role": "assistant", "content": content})
            else:
                content = m.get("content", "")
                result.append({"role": role, "content": content})

        # Flush finale
        if pending_tool_results:
            result.append({"role": "user", "content": pending_tool_results})

        # Fix alternanza: Anthropic vuole user/assistant alternati
        result = self._fix_anthropic_alternation(result)

        return system, result

    def _to_ollama_messages(self, messages: list[dict]) -> list[dict]:
        """Converte messaggi openvurp → formato Ollama chat/tool calling."""
        result = []

        for m in messages:
            role = m.get("role", "user")

            if role == "tool_result":
                tool_msg = {
                    "role": "tool",
                    "content": m.get("content", ""),
                }
                tool_name = m.get("name") or m.get("tool_name", "")
                if tool_name:
                    tool_msg["tool_name"] = tool_name
                result.append(tool_msg)
                continue

            if m.get("tool_calls"):
                msg = {
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "index": idx,
                                "name": tc["name"],
                                "arguments": tc["args"],
                            },
                        }
                        for idx, tc in enumerate(m["tool_calls"])
                    ],
                }
                result.append(msg)
                continue

            result.append({
                "role": role,
                "content": m.get("content", ""),
            })

        return result

    def _fix_anthropic_alternation(self, messages: list[dict]) -> list[dict]:
        """Garantisce alternanza user/assistant per Anthropic."""
        if not messages:
            return messages

        fixed = [messages[0]]
        for m in messages[1:]:
            if m["role"] == fixed[-1]["role"]:
                # Stessi ruoli consecutivi: merge
                prev_content = fixed[-1].get("content", "")
                this_content = m.get("content", "")
                if isinstance(prev_content, str) and isinstance(this_content, str):
                    fixed[-1]["content"] = prev_content + "\n" + this_content
                elif isinstance(prev_content, list) and isinstance(this_content, list):
                    fixed[-1]["content"] = prev_content + this_content
                else:
                    # Tipo misto: converti tutto a list
                    prev = prev_content if isinstance(prev_content, list) else [{"type": "text", "text": str(prev_content)}]
                    this = this_content if isinstance(this_content, list) else [{"type": "text", "text": str(this_content)}]
                    fixed[-1]["content"] = prev + this
            else:
                fixed.append(m)

        return fixed

    # ── Prompt caching (Anthropic) ──

    @staticmethod
    def _anthropic_system_param(system: str):
        """System come blocco con cache_control: il prefisso stabile
        (istruzioni + workspace) viene cachato lato API tra le iterazioni."""
        return [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]

    @staticmethod
    def _anthropic_cached_tools(tools_schema: list[dict]) -> list[dict]:
        """Copia lo schema tool marcando l'ultimo con cache_control."""
        if not tools_schema:
            return tools_schema
        cached = [dict(t) for t in tools_schema]
        cached[-1]["cache_control"] = {"type": "ephemeral"}
        return cached

    # ── Usage reale dalle risposte API ──

    def _extract_usage(self, raw) -> tuple[Optional[int], Optional[int]]:
        """Estrae (input_tokens, output_tokens) reali dalla risposta del backend.

        Returns (None, None) se il backend non li fornisce.
        """
        if raw is None:
            return None, None

        # Ollama: dict con prompt_eval_count / eval_count
        if isinstance(raw, dict):
            tok_in = raw.get("prompt_eval_count")
            tok_out = raw.get("eval_count")
            if isinstance(tok_in, int) or isinstance(tok_out, int):
                return (tok_in if isinstance(tok_in, int) else None,
                        tok_out if isinstance(tok_out, int) else None)
            return None, None

        usage = getattr(raw, "usage", raw)
        if usage is None:
            return None, None

        # Anthropic: input_tokens/output_tokens (+ cache read/write)
        tok_in = getattr(usage, "input_tokens", None)
        tok_out = getattr(usage, "output_tokens", None)
        if isinstance(tok_in, int):
            for extra in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                v = getattr(usage, extra, None)
                if isinstance(v, int):
                    tok_in += v
            return tok_in, tok_out if isinstance(tok_out, int) else None

        # OpenAI/Groq: prompt_tokens/completion_tokens
        tok_in = getattr(usage, "prompt_tokens", None)
        tok_out = getattr(usage, "completion_tokens", None)
        if isinstance(tok_in, int) or isinstance(tok_out, int):
            return (tok_in if isinstance(tok_in, int) else None,
                    tok_out if isinstance(tok_out, int) else None)

        return None, None

    # ── Function calling per backend ──

    def _call_openai_tools(self, model: str, messages: list[dict],
                           tools_schema: list[dict]) -> LLMResponse:
        """OpenAI/Groq function calling."""
        oai_messages = self._to_openai_messages(messages)
        client = self._client

        if self.backend == "groq":
            client = self._client

        kwargs = {
            "model": model,
            "messages": oai_messages,
            "temperature": self.tool_temperature if tools_schema else self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools_schema:
            kwargs["tools"] = tools_schema

        r = client.chat.completions.create(**kwargs)
        msg = r.choices[0].message

        text = msg.content or ""
        tool_calls = []

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {"_raw": tc.function.arguments}

                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    args=args,
                ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=r.choices[0].finish_reason or "",
            raw=r,
        )

    def _call_anthropic_tools(self, model: str, messages: list[dict],
                              tools_schema: list[dict]) -> LLMResponse:
        """Anthropic function calling."""
        system, ant_messages = self._to_anthropic_messages(messages)

        kwargs = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": ant_messages,
            "temperature": self.tool_temperature if tools_schema else self.temperature,
        }
        if system:
            kwargs["system"] = self._anthropic_system_param(system)
        if tools_schema:
            kwargs["tools"] = self._anthropic_cached_tools(tools_schema)

        r = self._client.messages.create(**kwargs)

        text = ""
        tool_calls = []

        for block in r.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    args=block.input if isinstance(block.input, dict) else {},
                ))

        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=r.stop_reason or "",
            raw=r,
        )

    def _call_ollama_tools(self, model: str, messages: list[dict],
                           tools_schema: list[dict]) -> LLMResponse:
        """Ollama tool calling nativo con fallback al path legacy se il server non lo supporta."""
        if not tools_schema:
            return LLMResponse(text=self._call_ollama(model, messages))

        payload = {
            "model": model,
            "messages": self._to_ollama_messages(messages),
            "tools": tools_schema,
            "stream": False,
            "options": {
                "temperature": self.tool_temperature,
                "num_predict": self.max_tokens,
            },
        }
        if self.think is not None:
            payload["think"] = self.think

        base = self.base_url or "http://localhost:11434"
        timeout = getattr(self, "_ollama_timeout", 120)

        try:
            r = self._ollama_post(f"{base}/api/chat", payload, timeout)
            data = r.json()
        except self._requests.exceptions.Timeout:
            raise LLMError(
                f"Ollama timeout dopo {timeout}s — il modello è troppo lento o il contesto è troppo grande. "
                f"Prova a ridurre la conversazione o usa un modello più leggero.",
                retryable=False, backend="ollama"
            )
        except self._requests.exceptions.ConnectionError:
            raise LLMError(
                "Ollama non raggiungibile — assicurati che sia in esecuzione.",
                retryable=True, backend="ollama"
            )
        except self._requests.exceptions.HTTPError as e:
            response = getattr(e, "response", None)
            if response is not None and response.status_code in (400, 404):
                # Server Ollama vecchio o senza tool calling: torna al path legacy
                self._ollama_tools_supported = False
                return LLMResponse(text=self._call_ollama(model, messages))
            raise

        self._ollama_tools_supported = True

        message = data.get("message", {}) if isinstance(data, dict) else {}
        text = message.get("content", "") if isinstance(message, dict) else ""
        raw_tool_calls = message.get("tool_calls", []) if isinstance(message, dict) else []
        tool_calls = []

        for idx, tc in enumerate(raw_tool_calls or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_raw": args}
            if not name:
                continue
            tool_calls.append(ToolCall(
                id=f"ollama_{idx}_{uuid.uuid4().hex[:8]}",
                name=name,
                args=args,
            ))

        return LLMResponse(
            text=text or "",
            tool_calls=tool_calls,
            stop_reason=data.get("done_reason", "") if isinstance(data, dict) else "",
            raw=data,
        )

    # ── Chiamate semplici per backend ──

    def _do_call(self, backend: str, model: str, messages: list[dict]) -> str:
        """Esegue la chiamata per il backend specificato."""
        try:
            if backend == "ollama":
                return self._call_ollama(model, messages)
            elif backend in ("openai", "openai_compatible"):
                return self._call_openai(model, messages)
            elif backend == "anthropic":
                return self._call_anthropic(model, messages)
            elif backend == "groq":
                return self._call_groq(model, messages)
            else:
                raise LLMError(f"Backend sconosciuto: {backend}")
        except LLMError:
            raise
        except Exception as e:
            retryable = self._is_retryable(e)
            raise LLMError(str(e), retryable=retryable, backend=backend)

    def _call_ollama(self, model: str, messages: list[dict]) -> str:
        # Filtra campi extra dai messaggi per Ollama
        clean = [{"role": m["role"], "content": m.get("content", "")}
                 for m in messages if m.get("role") != "tool_result"]
        base = self.base_url or "http://localhost:11434"
        timeout = getattr(self, '_ollama_timeout', 120)
        payload = {
            "model": model, "messages": clean, "stream": False,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens}
        }
        if self.think is not None:
            payload["think"] = self.think
        try:
            r = self._ollama_post(f"{base}/api/chat", payload, timeout)
            msg = r.json().get("message", {}) or {}
            content = msg.get("content", "") or ""
            # Modelli reasoning: se il thinking ha consumato tutto e content è
            # vuoto, recupera il thinking così il chiamante non riceve "" (che
            # interpreterebbe come silenzio/errore).
            if not content.strip():
                content = msg.get("thinking", "") or ""
            return content
        except self._requests.exceptions.Timeout:
            raise LLMError(
                f"Ollama timeout dopo {timeout}s — il modello è troppo lento o il contesto è troppo grande. "
                f"Prova a ridurre la conversazione o usa un modello più leggero.",
                retryable=False, backend="ollama"
            )
        except self._requests.exceptions.ConnectionError:
            raise LLMError(
                "Ollama non raggiungibile — assicurati che sia in esecuzione.",
                retryable=True, backend="ollama"
            )

    def _call_openai(self, model: str, messages: list[dict]) -> str:
        oai_messages = self._to_openai_messages(messages)
        r = self._client.chat.completions.create(
            model=model, messages=oai_messages,
            temperature=self.temperature, max_tokens=self.max_tokens,
        )
        return r.choices[0].message.content

    def _call_anthropic(self, model: str, messages: list[dict]) -> str:
        system, ant_messages = self._to_anthropic_messages(messages)
        kwargs = {
            "model": model, "max_tokens": self.max_tokens,
            "messages": ant_messages, "temperature": self.temperature,
        }
        if system:
            kwargs["system"] = self._anthropic_system_param(system)
        r = self._client.messages.create(**kwargs)
        return r.content[0].text

    def _call_groq(self, model: str, messages: list[dict]) -> str:
        oai_messages = self._to_openai_messages(messages)
        r = self._client.chat.completions.create(
            model=model, messages=oai_messages,
            temperature=self.temperature, max_tokens=self.max_tokens,
        )
        return r.choices[0].message.content

    # ── Streaming ──

    def stream(self, messages: list[dict]) -> Iterator[str]:
        """Streaming token-by-token."""
        b = self.backend

        try:
            if b == "ollama":
                yield from self._stream_ollama(messages)
            elif b in ("openai", "openai_compatible"):
                yield from self._stream_openai(messages)
            elif b == "anthropic":
                yield from self._stream_anthropic(messages)
            elif b == "groq":
                yield from self._stream_openai(messages)
            else:
                yield self.call(messages)
        except Exception:
            yield self.call(messages)

    def _stream_ollama(self, messages: list[dict]) -> Iterator[str]:
        clean = [{"role": m["role"], "content": m.get("content", "")}
                 for m in messages if m.get("role") != "tool_result"]
        base = self.base_url or "http://localhost:11434"
        r = self._ollama_post(f"{base}/api/chat", {
            "model": self.model, "messages": clean, "stream": True,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens}
        }, timeout=300, stream=True)

        for line in r.iter_lines():
            if line:
                try:
                    data = json.loads(line)
                    token = data.get("message", {}).get("content", "")
                    if token:
                        yield token
                except Exception:
                    continue

    def _stream_openai(self, messages: list[dict]) -> Iterator[str]:
        oai_messages = self._to_openai_messages(messages)
        stream = self._client.chat.completions.create(
            model=self.model, messages=oai_messages,
            temperature=self.temperature, max_tokens=self.max_tokens,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def _stream_anthropic(self, messages: list[dict]) -> Iterator[str]:
        system, ant_messages = self._to_anthropic_messages(messages)
        kwargs = {
            "model": self.model, "max_tokens": self.max_tokens,
            "messages": ant_messages, "temperature": self.temperature,
        }
        if system:
            kwargs["system"] = self._anthropic_system_param(system)

        with self._client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text

    # ── Helpers ──

    def _estimate_input_tokens(self, messages: list[dict]) -> int:
        parts = []
        for m in messages:
            c = m.get("content", "")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                for block in c:
                    if isinstance(block, dict):
                        parts.append(block.get("text", block.get("content", "")))
                    elif isinstance(block, str):
                        parts.append(block)
        return len(" ".join(parts)) // 4

    def _is_retryable(self, error: Exception) -> bool:
        """Determina se un errore e retryable."""
        msg = str(error).lower()
        name = type(error).__name__.lower()
        retryable_keywords = ['timeout', 'rate limit', 'overloaded', '429', '503', '502']
        return any(kw in msg or kw in name for kw in retryable_keywords)


def _config_attr(cfg, *names, default=""):
    for name in names:
        if hasattr(cfg, name):
            value = getattr(cfg, name)
            if value not in (None, ""):
                return value
    return default


def _resolve_backend_kwargs(cfg, backend: str) -> dict:
    backend = (backend or "").strip().lower()
    kwargs = {}
    if backend == "groq":
        api_key = _config_attr(cfg, "GROQ_API_KEY", "LLM_API_KEY", default="")
        if api_key:
            kwargs["api_key"] = api_key
    elif backend == "openai":
        api_key = _config_attr(cfg, "OPENAI_API_KEY", "LLM_API_KEY", default="")
        base_url = _config_attr(cfg, "OPENAI_BASE_URL", default="")
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
    elif backend == "anthropic":
        api_key = _config_attr(cfg, "ANTHROPIC_API_KEY", "LLM_API_KEY", default="")
        if api_key:
            kwargs["api_key"] = api_key
    elif backend == "openai_compatible":
        api_key = _config_attr(cfg, "OPENAI_COMPATIBLE_API_KEY", "LLM_API_KEY", default="")
        base_url = _config_attr(cfg, "OPENAI_COMPATIBLE_BASE_URL", "LLM_BASE_URL", default="")
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
    else:
        base_url = _config_attr(cfg, "LLM_BASE_URL", default="")
        api_key = _config_attr(cfg, "LLM_API_KEY", default="")
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
    return kwargs


def create_llm_client(backend: str = "", model: str = "") -> LLMClient:
    """Factory: crea LLMClient dalla configurazione."""
    import config as cfg

    resolved_backend = backend or cfg.LLM_BACKEND
    resolved_model = model or cfg.LLM_MODEL
    kwargs = {
        "temperature": getattr(cfg, 'TEMPERATURE', 0.7),
        "tool_temperature": getattr(cfg, 'TOOL_TEMPERATURE', 0.2),
        "max_tokens": getattr(cfg, 'MAX_TOKENS', 8192),
    }
    # think: false (default) = il modello risponde diretto, niente monologo di
    # ragionamento in chat; true = forza il thinking; auto = lascia decidere al
    # modello (vecchio comportamento, può far trapelare il ragionamento).
    _t = (getattr(cfg, "LLM_THINK", "false") or "false").strip().lower()
    kwargs["think"] = {"true": True, "1": True, "yes": True, "on": True,
                       "false": False, "0": False, "no": False, "off": False}.get(_t, None)
    kwargs.update(_resolve_backend_kwargs(cfg, resolved_backend))

    return LLMClient(
        backend=resolved_backend,
        model=resolved_model,
        **kwargs
    )
