import json
import logging
import os

import requests

log = logging.getLogger("whatsapp-bot")

TTL_SECONDS = 3 * 24 * 60 * 60  # 3 dias: suficiente para retomar una conversacion sin acumular basura


def _base_url() -> str:
    return os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('UPSTASH_REDIS_REST_TOKEN', '')}"}


def get_history(wa_id: str) -> list[dict]:
    """Lee el historial de conversacion desde Upstash. Lista vacia si no hay
    nada guardado o si Upstash no esta configurado (nunca lanza excepciones,
    para no tumbar la respuesta al cliente por un fallo de memoria)."""
    url = _base_url()
    if not url:
        return []
    try:
        resp = requests.get(f"{url}/get/conv:{wa_id}", headers=_headers(), timeout=10)
        resp.raise_for_status()
        result = resp.json().get("result")
        return json.loads(result) if result else []
    except Exception:
        log.exception("No se pudo leer el historial de conversación desde Upstash")
        return []


def save_history(wa_id: str, history: list[dict]) -> None:
    """Guarda el historial con expiración de 3 días (SETEX) — así las
    conversaciones viejas se limpian solas, sin llenar el espacio gratis."""
    url = _base_url()
    if not url:
        return
    try:
        resp = requests.post(
            f"{url}/setex/conv:{wa_id}/{TTL_SECONDS}",
            headers=_headers(),
            data=json.dumps(history),
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        log.exception("No se pudo guardar el historial de conversación en Upstash")


NOTICE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 dias: se limpia sola si el dueño olvida quitarlo


def _notice_key() -> str:
    return f"aviso:{os.getenv('BUSINESS_NAME', 'default')}"


def get_business_notice() -> str | None:
    """Aviso temporal del negocio (cierre especial, cambio de horario), o
    None si no hay ninguno vigente."""
    url = _base_url()
    if not url:
        return None
    try:
        resp = requests.get(f"{url}/get/{_notice_key()}", headers=_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json().get("result")
    except Exception:
        log.exception("No se pudo leer el aviso del negocio desde Upstash")
        return None


def save_business_notice(aviso: str) -> None:
    """Guarda el aviso con expiración de 7 días — el dueño no tiene que
    acordarse de quitarlo, se limpia solo."""
    url = _base_url()
    if not url:
        return
    try:
        resp = requests.post(
            f"{url}/setex/{_notice_key()}/{NOTICE_TTL_SECONDS}",
            headers=_headers(),
            data=aviso,
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        log.exception("No se pudo guardar el aviso del negocio en Upstash")


def is_duplicate_message(message_id: str) -> bool:
    """Marca este message_id de WhatsApp como procesado y devuelve True si
    YA se había procesado antes (webhook reenviado por Meta). Usa SET...NX
    de Redis, que es atómico: si dos reintentos llegan casi al mismo tiempo,
    solo uno de los dos puede "ganar" el NX. Sin Upstash configurado, no se
    puede deduplicar y se deja pasar (mejor procesar de más que quedarse
    mudo)."""
    url = _base_url()
    if not url or not message_id:
        return False
    try:
        resp = requests.post(
            f"{url}/set/procesado:{message_id}/1/EX/600/NX",
            headers=_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        # NX devuelve "OK" si SÍ se guardó (primera vez); null si ya existía.
        return resp.json().get("result") != "OK"
    except Exception:
        log.exception("No se pudo verificar duplicados de mensaje en Upstash")
        return False
