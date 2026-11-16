"""Client for an OpenAI-compatible chat completion endpoint (in-house LLM). Stdlib only.

Every failure carries a code, so `mailbrief diagnose` can group failures by cause.
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from typing import Protocol

from .config import LLMConfig

# Failure codes (see docs/CLASSIFICATION_GUIDE.md)
E_TRANSPORT = "E_TRANSPORT"        # timeout, connection refused, HTTP 5xx, proxy
E_AUTH = "E_AUTH"                  # HTTP 401/403
E_CONTEXT = "E_CONTEXT"            # prompt too long for the model context
E_HTTP = "E_HTTP"                  # other HTTP 4xx
E_EMPTY = "E_EMPTY"                # model returned empty content
E_TRUNCATED = "E_TRUNCATED"        # finish_reason == "length": output cut by max_tokens
E_NO_JSON = "E_NO_JSON"            # no JSON object in output
E_BAD_JSON = "E_BAD_JSON"          # JSON is broken and repair failed
E_SCHEMA = "E_SCHEMA"              # JSON is valid but has wrong shape


class LLMError(RuntimeError):
    def __init__(self, msg: str, code: str = E_TRANSPORT, raw: str = "") -> None:
        super().__init__(msg)
        self.code = code
        self.raw = raw


class ChatModel(Protocol):
    def chat(self, system: str, user: str, json_output: bool = True) -> str: ...


_CTX_HINTS = ("context length", "maximum context", "too many tokens", "context_length_exceeded",
              "prompt is too long", "input is too long", "max_model_len")


class OpenAICompatClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        if not cfg.verify_tls:
            self._ctx = ssl._create_unverified_context()  # only for internal endpoints with self-signed certs
        elif cfg.ca_bundle:
            self._ctx = ssl.create_default_context(cafile=cfg.ca_bundle)
        else:
            self._ctx = ssl.create_default_context()
        self._json_mode_ok = cfg.json_mode

    def chat(self, system: str, user: str, json_output: bool = True) -> str:
        return self.chat_ex(system, user, json_output)[0]

    def chat_ex(self, system: str, user: str, json_output: bool = True) -> tuple[str, str]:
        """Return (content, finish_reason)."""
        if self.cfg.system_in_user:
            messages = [{"role": "user", "content": f"{system}\n\n---\n\n{user}"}]
        else:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        payload = {
            **self.cfg.extra_body,
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if json_output and self._json_mode_ok:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"

        last: LLMError | None = None
        attempts = self.cfg.max_retries + 1
        i = 0
        while i < attempts:
            req = urllib.request.Request(url, json.dumps(payload).encode("utf-8"), headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout_s, context=self._ctx) as r:
                    data = json.loads(r.read().decode("utf-8"))
                ch = data["choices"][0]
                return (ch.get("message") or {}).get("content") or "", ch.get("finish_reason") or ""
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:500]
                low = body.lower()
                if e.code in (401, 403):
                    raise LLMError(f"HTTP {e.code}: {body}", E_AUTH)
                if e.code in (400, 413, 422) and any(h in low for h in _CTX_HINTS):
                    raise LLMError(f"HTTP {e.code}: {body}", E_CONTEXT)
                if e.code in (400, 422) and "response_format" in payload:
                    # Server does not support JSON mode: drop it for this and later calls. No sleep.
                    payload.pop("response_format")
                    self._json_mode_ok = False
                    last = LLMError(f"HTTP {e.code} with response_format: {body}", E_HTTP)
                    continue
                code = E_TRANSPORT if e.code >= 500 or e.code == 429 else E_HTTP
                last = LLMError(f"HTTP {e.code}: {body}", code)
                if code == E_HTTP:
                    raise last
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = LLMError(f"{type(e).__name__}: {e}", E_TRANSPORT)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                last = LLMError(f"Unexpected response format: {e}", E_TRANSPORT)
            i += 1
            if i < attempts:
                time.sleep(min(2 ** i, 8))
        raise last or LLMError("LLM call failed", E_TRANSPORT)


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def _repair(t: str) -> str:
    t = re.sub(r",\s*([}\]])", r"\1", t)                      # trailing commas
    t = re.sub(r"\bTrue\b", "true", t)
    t = re.sub(r"\bFalse\b", "false", t)
    t = re.sub(r"\bNone\b", "null", t)
    t = re.sub(r"//[^\n\"]*$", "", t, flags=re.M)              # // comments copied from the schema
    return t


def _close_brackets(t: str) -> str:
    """Close an output that was cut in the middle (best effort)."""
    stack, in_str, esc = [], False, False
    for ch in t:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        t += '"'
    t = re.sub(r",\s*$", "", t.rstrip())
    t = re.sub(r':\s*$', ": null", t)
    if stack and stack[-1] == "}" and re.search(r'[{,]\s*"[^"]*"$', t):   # cut right after a key
        t += ": null"
    return t + "".join(reversed(stack))


def parse_json(text: str, allow_truncated: bool = False) -> dict:
    """Parse one JSON object from model output. Handles <think> blocks, code fences, noise, small syntax errors."""
    if not text or not text.strip():
        raise LLMError("Empty model output", E_EMPTY, text)
    t = _THINK_RE.sub("", text).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", t.strip())
    start = t.find("{")
    if start < 0:
        raise LLMError("No JSON object in model output", E_NO_JSON, text)
    end = t.rfind("}")
    cand = t[start : end + 1] if end > start else t[start:]
    for variant in (cand, _repair(cand)):
        try:
            obj = json.loads(variant)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    if allow_truncated:
        try:
            obj = json.loads(_close_brackets(_repair(t[start:])))
            if isinstance(obj, dict):
                obj["_repaired_truncation"] = True
                return obj
        except json.JSONDecodeError:
            pass
    raise LLMError("Invalid JSON from model", E_BAD_JSON, text)
