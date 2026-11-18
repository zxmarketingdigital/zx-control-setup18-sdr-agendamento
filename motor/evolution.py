"""Cliente da Evolution API (WhatsApp): leitura de mensagens, envio, mídia e estado da instância."""

from __future__ import annotations

import urllib.parse

from .config import env_positive
from .http import HttpError, NetworkError, NetworkTimeout, http_json
from .util import to_e164, digits

# Teto do download de uma mídia recebida. Override: ZX_SDR_MEDIA_TIMEOUT.
MEDIA_TIMEOUT = env_positive("ZX_SDR_MEDIA_TIMEOUT", 60)
MEDIA_TYPES = {
    "audioMessage": "audio",
    "imageMessage": "image",
    "videoMessage": "video",
    "documentMessage": "document",
    "stickerMessage": "sticker",
}


class EvolutionError(Exception):
    """Falha da Evolution. `status` é o HTTP quando houve resposta."""

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        self.status = status


class EvolutionTimeout(EvolutionError):
    """Tempo esgotado: a Evolution pode ter enviado a mensagem. Nunca reenviar sozinho."""


class EvolutionFormatoDesconhecido(EvolutionError):
    """A resposta de findMessages não tem nenhuma forma conhecida (a Evolution mudou o envelope): não é 'vazio'."""


class EvolutionNoKeyId(EvolutionError):
    """Resposta 2xx sem key.id: a mensagem pode ter saído. Nunca é sucesso e nunca é reenviada sozinha."""


# 500/408/502/503/504: a Evolution (ou o proxy) pode ter entregue antes do erro. Resultado incerto, sem reenvio.
UNCERTAIN_STATUSES = (408, 500, 502, 503, 504)


def _failed_before_connect(exc: BaseException) -> bool:
    """A falha de rede aconteceu ANTES de a requisição sair (DNS, conexão recusada, rede inalcançável)? Só essas
    são seguras para reenviar; qualquer outra (reset, conexão encerrada, erro de leitura) pode ter vindo depois
    de a Evolution receber o pedido."""
    import socket
    import ssl

    before = (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError)
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, before):
            return True
        if isinstance(current, OSError) and getattr(current, "errno", None) in (51, 61, 65, 101, 111, 113):
            return True   # rede/host inalcançável, conexão recusada (macOS/Linux)
        pending.extend([getattr(current, "reason", None), current.__cause__, current.__context__])
    return False


def record_phone(key: dict) -> str:
    """Telefone E.164 do contato de uma conversa individual ('' para grupo, status ou @lid sem número)."""
    jid = key.get("remoteJid") or ""
    if jid.endswith("@s.whatsapp.net"):
        return to_e164(jid.split("@")[0])
    if jid.endswith("@lid"):
        alt = key.get("remoteJidAlt") or key.get("senderPn") or ""
        if alt.endswith("@s.whatsapp.net"):
            return to_e164(alt.split("@")[0])
    return ""


# Embrulhos do WhatsApp que carregam a mensagem de verdade dentro (mensagens temporárias, ver uma vez, legenda de
# documento, edição). Desembrulhados (recursivo, até 3 níveis) antes de ler texto e mídia.
WRAPPER_FIELDS = ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension",
                  "documentWithCaptionMessage", "editedMessage")
# Conteúdo que nunca é mensagem do lead: descartado em silêncio.
SILENT_FIELDS = ("reactionMessage", "protocolMessage", "senderKeyDistributionMessage")
UNWRAP_DEPTH = 3


def unwrap_message(message) -> dict:
    """Mensagem de conteúdo, sem os embrulhos (ephemeral, viewOnce, documentWithCaption, edited)."""
    current = message if isinstance(message, dict) else {}
    for _ in range(UNWRAP_DEPTH):
        inner = None
        for field in WRAPPER_FIELDS:
            wrapped = current.get(field)
            if isinstance(wrapped, dict) and isinstance(wrapped.get("message"), dict):
                inner = wrapped["message"]
                break
        if inner is None:
            inner = _edited_content(current)
        if inner is None:
            break
        current = inner
    return current


def _edited_content(message: dict):
    """protocolMessage de EDIÇÃO (type 14 / MESSAGE_EDIT, ou com editedMessage): o conteúdo novo da mensagem editada.
    Outros protocolMessage (revoke etc.) devolvem None e seguem silenciosos."""
    protocol = message.get("protocolMessage")
    if not isinstance(protocol, dict) or message.get("conversation") or message.get("extendedTextMessage"):
        return None   # mensagem que já traz texto próprio não é um registro de edição
    edited = protocol.get("editedMessage")
    if not isinstance(edited, dict) or not edited:
        return None
    if isinstance(edited.get("message"), dict):
        return edited["message"]
    return edited


def is_silent_record(record: dict) -> bool:
    """Reação, protocolo, distribuição de chave ou só messageContextInfo: nunca é mensagem do lead."""
    message = unwrap_message(record.get("message"))
    if any(field in message for field in SILENT_FIELDS):
        return True
    return bool(message) and set(message) <= {"messageContextInfo"}


def record_text(record: dict) -> str:
    message = unwrap_message(record.get("message"))
    text = (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or (message.get("imageMessage") or {}).get("caption")
        or (message.get("videoMessage") or {}).get("caption")
        or (message.get("documentMessage") or {}).get("caption")
        or ""
    )
    return str(text).strip()


def record_media(record: dict) -> str | None:
    message = unwrap_message(record.get("message"))
    for field, kind in MEDIA_TYPES.items():
        if field in message:
            return kind
    return None


def _timestamp_value(raw) -> float | None:
    """Epoch em segundos de um messageTimestamp: número, string numérica, string ISO 8601 ou objeto {low, high}
    (Long do protobuf). Nada legível, zero ou negativo: None."""
    import math
    from datetime import datetime, timezone

    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, dict):
        if "low" not in raw and "high" not in raw:
            return None
        try:
            low, high = int(raw.get("low") or 0), int(raw.get("high") or 0)
        except (TypeError, ValueError):
            return None
        value = float((high << 32) + (low & 0xFFFFFFFF))
    elif isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        try:
            value = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            value = parsed.timestamp()
    else:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    if value > 1e11:
        value /= 1000.0   # milissegundos
    return value


def record_timestamp_or_none(record: dict) -> float | None:
    """Instante da mensagem (epoch s) ou None quando o campo some ou não dá para ler. None NUNCA vira 'agora'."""
    return _timestamp_value((record or {}).get("messageTimestamp"))


def record_timestamp(record: dict) -> float:
    """Só para ordenar: 0.0 quando ilegível. Decisão de resposta usa record_timestamp_or_none."""
    return record_timestamp_or_none(record) or 0.0


def _parse_find_messages(result) -> list:
    """Lista de registros de uma resposta de findMessages. Vazio REAL (lista vazia, records vazio, total 0) devolve [];
    formato desconhecido levanta EvolutionFormatoDesconhecido (nunca vira 'sem mensagens' em silêncio). Só as chaves de
    topo vão na mensagem, nunca o corpo."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, dict):
            if isinstance(messages.get("records"), list):
                return messages["records"]
            if messages.get("total") == 0:
                return []
        elif isinstance(messages, list):
            return messages
        if isinstance(result.get("records"), list):
            return result["records"]
        keys = ", ".join(sorted(str(k)[:40] for k in result)[:10])
    else:
        keys = f"tipo {type(result).__name__}"
    raise EvolutionFormatoDesconhecido(f"formato de resposta desconhecido em findMessages: chaves {keys}")


class EvolutionClient:
    def __init__(self, base_url: str, api_key: str, instance: str):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.instance = urllib.parse.quote(instance, safe="")
        self.instance_name = instance

    def _call(self, method: str, endpoint: str, body=None, timeout: float | None = None):
        url = f"{self.base}{endpoint}/{self.instance}"
        try:
            return http_json(method, url, headers={"apikey": self.key}, body=body, timeout=timeout)
        except NetworkTimeout as exc:
            raise EvolutionTimeout(str(exc)) from exc
        except HttpError as exc:
            raise EvolutionError(str(exc), status=exc.status) from exc
        except NetworkError as exc:
            if _failed_before_connect(exc):
                raise EvolutionError(str(exc)) from exc   # nada saiu: reenviar é seguro
            raise EvolutionTimeout(str(exc)) from exc   # caiu no meio: a Evolution pode ter processado

    def recent_messages(self, limit: int = 50, page: int = 1) -> list:
        """Uma página de mensagens da instância (a página 1 traz as mais recentes)."""
        result = self._call("POST", "/chat/findMessages", {"where": {}, "page": int(page), "offset": limit})
        return _parse_find_messages(result)

    def chat_messages(self, phone: str, limit: int = 10, jid: str | None = None) -> list:
        """Últimas mensagens do chat de UM contato (leitura direcionada, mais recentes primeiro). `jid` consulta
        outro identificador do mesmo contato (o @lid); sem ele usa <número>@s.whatsapp.net."""
        jid = jid or f"{digits(phone)}@s.whatsapp.net"
        result = self._call("POST", "/chat/findMessages",
                            {"where": {"key": {"remoteJid": jid}}, "page": 1, "offset": int(limit)})
        return _parse_find_messages(result)

    def send_text(self, phone: str, text: str, delay_ms: int = 0) -> str:
        """Envia texto e devolve o key.id. Sem key.id na resposta o envio NÃO conta como enviado."""
        body = {"number": digits(phone), "text": text}
        if delay_ms:
            body["delay"] = int(delay_ms)
        result = self._call("POST", "/message/sendText", body)
        key_id = ""
        if isinstance(result, dict):
            key_id = (result.get("key") or {}).get("id") or ""
        if not key_id:
            raise EvolutionNoKeyId("A Evolution respondeu sem key.id: envio não confirmado")
        return key_id

    def media_base64(self, record: dict) -> tuple[bytes, str] | None:
        """Baixa o conteúdo de uma mídia recebida. Devolve (bytes, mimetype) ou None."""
        import base64

        result = self._call("POST", "/chat/getBase64FromMediaMessage", {"message": {"key": record.get("key")}}, timeout=MEDIA_TIMEOUT)
        if not isinstance(result, dict) or not result.get("base64"):
            return None
        raw = result["base64"]
        if "," in raw[:80]:
            raw = raw.split(",", 1)[1]
        return base64.b64decode(raw), result.get("mimetype") or "application/octet-stream"

    def connect(self):
        """Pede à Evolution o QR Code (e o código de pareamento) para conectar o número. Dict com `base64`,
        `pairingCode` e `code`. 404 = a instância não existe (ver create_instance)."""
        data = self._call("GET", "/instance/connect")
        return data if isinstance(data, dict) else {}

    def logout(self) -> None:
        """Desconecta o número atual da instância (a instância continua existindo)."""
        self._call("DELETE", "/instance/logout")

    def create_instance(self) -> None:
        """Cria a instância quando ela não existe mais (só usado durante um pedido de reconexão do painel)."""
        body = {"instanceName": self.instance_name, "qrcode": True, "integration": "WHATSAPP-BAILEYS"}
        try:
            http_json("POST", f"{self.base}/instance/create", headers={"apikey": self.key}, body=body)
        except NetworkTimeout as exc:
            raise EvolutionTimeout(str(exc)) from exc
        except HttpError as exc:
            raise EvolutionError(str(exc), status=exc.status) from exc
        except NetworkError as exc:
            raise EvolutionError(str(exc)) from exc

    def connection_state(self) -> str:
        data = self._call("GET", "/instance/connectionState")
        if isinstance(data, dict):
            return str((data.get("instance") or data).get("state", "?"))
        return "?"

    def owner_jid(self) -> str:
        """Número da instância (para não responder a si mesmo). Vazio se não der para saber."""
        try:
            data = http_json(
                "GET",
                f"{self.base}/instance/fetchInstances?instanceName={self.instance}",
                headers={"apikey": self.key},
            )
        except (HttpError, NetworkError):
            return ""
        rows = data if isinstance(data, list) else []
        if rows:
            first = rows[0].get("instance", rows[0]) if isinstance(rows[0], dict) else {}
            return str(first.get("ownerJid") or first.get("owner") or "")
        return ""
