import logging
import os
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

log = logging.getLogger("whatsapp-bot")

CALENDAR_API = "https://www.googleapis.com/calendar/v3"

_credentials = None


# El negocio comparte SU Google Calendar con el correo de esta misma cuenta
# de servicio (la que ya se usa para Sheets) dándole permiso de "Realizar
# cambios en los eventos" — así se evita construir un flujo de autorización
# OAuth por negocio, que además requeriría que Google revise la app para
# uso en producción con terceros.
def _get_credentials():
    global _credentials
    if _credentials is not None:
        return _credentials
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        return None
    info = json.loads(creds_json)
    _credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return _credentials


def _auth_headers() -> dict | None:
    creds = _get_credentials()
    if not creds:
        return None
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}


def _parse_horario(horario: str, zona: str = "America/Mexico_City", ahora: datetime | None = None) -> datetime | None:
    """El bot siempre guarda el horario de una cita como fecha absoluta en
    español (ej. "viernes 6 de septiembre de 2026 a las 4pm" — ver la
    instrucción en agent/client.py que obliga a resolver fechas relativas).
    Aquí se interpreta ese texto para poder crear el evento de Calendar. Si
    el formato no coincide con lo esperado (alguien lo escribió distinto a
    mano, por ejemplo desde Telegram), devuelve None — mejor no crear el
    evento a crear uno con la fecha equivocada.

    "zona" es el huso horario del NEGOCIO (columna "Zona horaria" en
    Sheets) — México tiene varios husos, "las 5pm" que dice un cliente en
    Ensenada no es la misma hora real que "las 5pm" en Ciudad de México."""
    zona_info = ZoneInfo(zona)
    ahora = ahora or datetime.now(zona_info)
    texto = horario.lower()

    m = re.search(r"(\d{1,2})\s+de\s+([a-zñ]+)(?:\s+de\s+(\d{4}))?", texto)
    if not m:
        return None
    dia = int(m.group(1))
    mes = _MESES.get(m.group(2))
    if not mes:
        return None
    anio = int(m.group(3)) if m.group(3) else ahora.year

    hora, minuto = 12, 0  # si no se especifica hora, mediodía por default
    h = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", texto[m.end():])
    if h:
        hora = int(h.group(1))
        minuto = int(h.group(2)) if h.group(2) else 0
        periodo = h.group(3)
        if periodo == "pm" and hora != 12:
            hora += 12
        elif periodo == "am" and hora == 12:
            hora = 0

    try:
        return datetime(anio, mes, dia, hora, minuto, tzinfo=zona_info)
    except ValueError:
        return None


def create_event(
    calendar_id: str, resumen: str, descripcion: str, horario_texto: str,
    zona: str = "America/Mexico_City",
) -> str | None:
    """Crea el evento en el calendario del negocio. Devuelve el ID del
    evento creado (para poder borrarlo/actualizarlo después), o None si no
    se pudo crear (calendario no compartido, horario no interpretable,
    etc.) — nunca lanza excepción, un fallo aquí no debe tumbar la
    confirmación de la cita en sí."""
    headers = _auth_headers()
    if not headers or not calendar_id:
        return None

    inicio = _parse_horario(horario_texto, zona=zona)
    if not inicio:
        log.warning(f"No se pudo interpretar el horario '{horario_texto}' para Calendar")
        return None
    fin = inicio + timedelta(hours=1)

    body = {
        "summary": resumen,
        "description": descripcion,
        "start": {"dateTime": inicio.isoformat(), "timeZone": zona},
        "end": {"dateTime": fin.isoformat(), "timeZone": zona},
    }
    try:
        resp = requests.post(
            f"{CALENDAR_API}/calendars/{requests.utils.quote(calendar_id, safe='')}/events",
            headers=headers, json=body, timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("id")
    except Exception:
        log.exception(f"No se pudo crear el evento de Calendar para '{calendar_id}'")
        return None


def delete_event(calendar_id: str, event_id: str) -> None:
    """Borra un evento ya creado (cita cancelada o modificada). Nunca lanza
    excepción — si el evento ya no existe o el calendario ya no está
    compartido, simplemente se ignora."""
    headers = _auth_headers()
    if not headers or not calendar_id or not event_id:
        return
    try:
        resp = requests.delete(
            f"{CALENDAR_API}/calendars/{requests.utils.quote(calendar_id, safe='')}/events/{event_id}",
            headers=headers, timeout=15,
        )
        if resp.status_code not in (200, 204, 404, 410):
            resp.raise_for_status()
    except Exception:
        log.exception(f"No se pudo borrar el evento de Calendar '{event_id}'")
