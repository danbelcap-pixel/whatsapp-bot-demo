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


def get_history(business_id: str, wa_id: str) -> list[dict]:
    """Lee el historial de conversacion desde Upstash. Lista vacia si no hay
    nada guardado o si Upstash no esta configurado (nunca lanza excepciones,
    para no tumbar la respuesta al cliente por un fallo de memoria).

    La clave incluye business_id porque el mismo número de cliente puede
    escribirle a bots de negocios distintos — sin el prefijo, sus
    conversaciones con negocios distintos se mezclarían entre sí."""
    url = _base_url()
    if not url:
        return []
    try:
        resp = requests.get(f"{url}/get/conv:{business_id}:{wa_id}", headers=_headers(), timeout=10)
        resp.raise_for_status()
        result = resp.json().get("result")
        return json.loads(result) if result else []
    except Exception:
        log.exception("No se pudo leer el historial de conversación desde Upstash")
        return []


def save_history(business_id: str, wa_id: str, history: list[dict]) -> None:
    """Guarda el historial con expiración de 3 días (SETEX) — así las
    conversaciones viejas se limpian solas, sin llenar el espacio gratis."""
    url = _base_url()
    if not url:
        return
    try:
        resp = requests.post(
            f"{url}/setex/conv:{business_id}:{wa_id}/{TTL_SECONDS}",
            headers=_headers(),
            data=json.dumps(history),
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        log.exception("No se pudo guardar el historial de conversación en Upstash")


NOTICE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 dias: se limpia sola si el dueño olvida quitarlo
BUSINESS_CONFIG_TTL_SECONDS = 5 * 60  # 5 minutos: balance entre no golpear Sheets en cada mensaje y que un alta/baja de cliente se refleje rápido


def _notice_key(business_id: str) -> str:
    return f"aviso:{business_id}"


def get_business_notice(business_id: str) -> str | None:
    """Aviso temporal del negocio (cierre especial, cambio de horario), o
    None si no hay ninguno vigente."""
    url = _base_url()
    if not url:
        return None
    try:
        resp = requests.get(f"{url}/get/{_notice_key(business_id)}", headers=_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json().get("result")
    except Exception:
        log.exception("No se pudo leer el aviso del negocio desde Upstash")
        return None


def save_business_notice(business_id: str, aviso: str) -> None:
    """Guarda el aviso con expiración de 7 días — el dueño no tiene que
    acordarse de quitarlo, se limpia solo."""
    url = _base_url()
    if not url:
        return
    try:
        resp = requests.post(
            f"{url}/setex/{_notice_key(business_id)}/{NOTICE_TTL_SECONDS}",
            headers=_headers(),
            data=aviso,
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        log.exception("No se pudo guardar el aviso del negocio en Upstash")


def get_cached_business_config(phone_number_id: str) -> dict | None:
    """Config de negocio cacheada (evita leer la pestaña 'Clientes' de
    Sheets en cada mensaje entrante). None si no hay nada en cache — no
    distingue "no existe el negocio" de "no está cacheado", quien llama debe
    ir a Sheets en ambos casos."""
    url = _base_url()
    if not url:
        return None
    try:
        resp = requests.get(f"{url}/get/negocio:{phone_number_id}", headers=_headers(), timeout=10)
        resp.raise_for_status()
        result = resp.json().get("result")
        return json.loads(result) if result else None
    except Exception:
        log.exception("No se pudo leer la config de negocio cacheada desde Upstash")
        return None


def save_cached_business_config(phone_number_id: str, config: dict) -> None:
    url = _base_url()
    if not url:
        return
    try:
        resp = requests.post(
            f"{url}/setex/negocio:{phone_number_id}/{BUSINESS_CONFIG_TTL_SECONDS}",
            headers=_headers(),
            data=json.dumps(config),
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        log.exception("No se pudo guardar la config de negocio en cache en Upstash")


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


WIDGET_RATE_LIMIT = 100  # mensajes por visitante por hora — ver ask_agent/widget
WIDGET_RATE_WINDOW_SECONDS = 60 * 60


def check_widget_rate_limit(visitor_id: str) -> bool:
    """True si este visitante del chat web todavía puede mandar otro
    mensaje, False si ya llegó al límite en la última hora. Es un límite
    generoso a propósito (ningún cliente real llega a 100 mensajes/hora) —
    solo frena scripts/ataques automatizados, no personas preguntando
    mucho. Sin Upstash configurado, no se puede contar y se deja pasar
    (mejor no bloquear a nadie que fallar por un problema de Redis)."""
    url = _base_url()
    if not url or not visitor_id:
        return True
    key = f"widget_rate:{visitor_id}"
    try:
        resp = requests.post(f"{url}/incr/{key}", headers=_headers(), timeout=10)
        resp.raise_for_status()
        count = resp.json().get("result", 0)
        if count == 1:
            # Primera vez que se usa esta llave: le pone expiración de 1h.
            # Si esto fallara, en el peor caso la llave nunca expira y el
            # visitante se queda bloqueado tras 100 mensajes para siempre —
            # por eso se registra el error, aunque no se bloquee el mensaje.
            expire_resp = requests.post(f"{url}/expire/{key}/{WIDGET_RATE_WINDOW_SECONDS}", headers=_headers(), timeout=10)
            expire_resp.raise_for_status()
        return count <= WIDGET_RATE_LIMIT
    except Exception:
        log.exception("No se pudo verificar el límite de mensajes del widget")
        return True
