"""Provedores de IA (Gemini, OpenAI, Claude) com retry e backoff. Só stdlib."""

from __future__ import annotations

import base64
import time

from .config import env_positive
from .http import HttpError, NetworkError, http_json

# Teto de UMA chamada de IA (prompt comprido pode passar de minutos). Override: ZX_SDR_AI_TIMEOUT.
AI_TIMEOUT = env_positive("ZX_SDR_AI_TIMEOUT", 600)
# Esperas entre tentativas (segundos): 1 chamada + 2 repetições.
AI_BACKOFF = (2, 6)
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MAX_OUTPUT_TOKENS = 1500


class AIError(Exception):
    """Falha da IA depois de esgotar as tentativas."""


class _Permanent(Exception):
    """Erro que não adianta repetir (chave inválida, requisição malformada)."""


class AIClient:
    def __init__(self, provider: str, api_key: str, model: str, log, sleep_fn=time.sleep, timeout: float | None = None):
        self.provider = provider
        self.key = api_key
        self.model = model
        self.log = log
        self.sleep = sleep_fn
        self.timeout = timeout or AI_TIMEOUT

    # ---- API pública ----
    def complete(self, system: str, messages: list[dict]) -> str:
        """messages: [{'role': 'user'|'assistant', 'content': str}]. Devolve o texto bruto da IA."""
        return self._with_retry(lambda: self._once(system, messages), "conversa")

    def transcribe(self, data: bytes, mime: str) -> str:
        """Transcreve áudio / descreve imagem (só Gemini aceita mídia direto)."""
        if self.provider != "gemini":
            raise AIError("transcrição de mídia só está disponível com o provedor gemini")
        return self._with_retry(lambda: self._gemini_media(data, mime), "mídia")

    # ---- retry ----
    def _with_retry(self, call, label: str) -> str:
        last = None
        attempts = len(AI_BACKOFF) + 1
        for attempt in range(attempts):
            t0 = time.monotonic()
            try:
                text = call()
                self.log.info("ia %s (%s) levou %.0fs (teto %.0fs)", self.provider, label, time.monotonic() - t0, self.timeout)
                return text
            except _Permanent as exc:
                self.log.error("ia %s (%s) recusou: %s", self.provider, label, exc)
                raise AIError(str(exc)) from exc
            except (HttpError, NetworkError, ValueError) as exc:
                last = exc
                if isinstance(exc, HttpError) and 400 <= exc.status < 500 and exc.status not in (408, 429):
                    self.log.error("ia %s (%s) erro %s: %s", self.provider, label, exc.status, exc.body[:200])
                    raise AIError(str(exc)) from exc
                self.log.warning("ia %s (%s) falhou em %.0fs (tentativa %d/%d): %s",
                                 self.provider, label, time.monotonic() - t0, attempt + 1, attempts, exc)
                if attempt < len(AI_BACKOFF):
                    self.sleep(AI_BACKOFF[attempt])
        raise AIError(f"IA indisponível após {attempts} tentativas: {last}")

    # ---- provedores ----
    def _once(self, system: str, messages: list[dict]) -> str:
        if self.provider == "gemini":
            return self._gemini(system, messages)
        if self.provider == "openai":
            return self._openai(system, messages)
        if self.provider == "anthropic":
            return self._anthropic(system, messages)
        raise _Permanent(f"provedor de IA desconhecido: {self.provider}")

    def _gemini(self, system: str, messages: list[dict]) -> str:
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
        ]
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS, "temperature": 0.6,
                                 "responseMimeType": "application/json"},
        }
        return self._gemini_text(body)

    def _gemini_media(self, data: bytes, mime: str) -> str:
        prompt = ("Transcreva o áudio em português, fielmente, sem comentários. "
                  "Se for uma imagem, descreva em uma frase o que ela mostra e copie qualquer texto legível.")
        body = {"contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inlineData": {"mimeType": mime.split(";")[0], "data": base64.b64encode(data).decode("ascii")}},
        ]}], "generationConfig": {"maxOutputTokens": 800, "temperature": 0.0}}
        return self._gemini_text(body)

    def _gemini_text(self, body: dict) -> str:
        result = http_json("POST", GEMINI_URL.format(model=self.model), headers={"x-goog-api-key": self.key},
                           body=body, timeout=self.timeout)
        parts = []
        for candidate in result.get("candidates", []) or []:
            parts.extend((candidate.get("content") or {}).get("parts", []) or [])
        # partes com thought=True são raciocínio interno: nunca vão para o lead
        text = "".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought")).strip()
        if not text:
            raise ValueError("A IA não devolveu texto")
        return text

    def _openai(self, system: str, messages: list[dict]) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.6,
            "response_format": {"type": "json_object"},
        }
        result = http_json("POST", OPENAI_URL, headers={"Authorization": f"Bearer {self.key}"},
                           body=body, timeout=self.timeout)
        choices = result.get("choices") or []
        text = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
        if not text:
            raise ValueError("A IA não devolveu texto")
        return text

    def _anthropic(self, system: str, messages: list[dict]) -> str:
        body = {"model": self.model, "max_tokens": MAX_OUTPUT_TOKENS, "system": system, "messages": messages}
        result = http_json("POST", ANTHROPIC_URL,
                           headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"},
                           body=body, timeout=self.timeout)
        text = "".join(b.get("text", "") for b in result.get("content", []) or [] if b.get("type") == "text").strip()
        if not text:
            raise ValueError("A IA não devolveu texto")
        return text

    def ping(self) -> str:
        reply = self._once("Responda apenas com a palavra OK.", [{"role": "user", "content": "ping"}])
        return f"{self.provider}/{self.model}: {reply[:30]}"
