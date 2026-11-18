"""Cliente HTTP mínimo (urllib) usado por Supabase, Evolution e IA."""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request

from .config import env_positive

# Chamadas curtas (PostgREST, Evolution). A IA tem o próprio teto em ai.py.
HTTP_TIMEOUT = env_positive("ZX_SDR_HTTP_TIMEOUT", 30)

urlopen = urllib.request.urlopen  # os testes substituem esta referência


class HttpError(Exception):
    """Resposta HTTP de erro (4xx/5xx)."""

    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


class NetworkError(Exception):
    """Falha de rede, DNS, conexão recusada ou tempo esgotado."""


class NetworkTimeout(NetworkError):
    """Tempo esgotado: a requisição pode ter sido processada pelo servidor (resultado incerto)."""


_UNCERTAIN = (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected, http.client.BadStatusLine,
              http.client.IncompleteRead)


def http_json(method: str, url: str, *, headers: dict | None = None, body=None, timeout: float | None = None):
    data = None
    all_headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    all_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urlopen(request, timeout=timeout or HTTP_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            detail = ""
        raise HttpError(exc.code, detail, url) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError)):
            raise NetworkTimeout(str(exc)) from exc
        if isinstance(exc, _UNCERTAIN) or isinstance(reason, _UNCERTAIN):
            # a conexão caiu depois de enviar o corpo: o servidor pode ter processado (resultado incerto)
            raise NetworkTimeout(str(exc)) from exc
        raise NetworkError(str(exc)) from exc
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"_raw": raw.decode("utf-8", "replace")}
