import json
import logging
import os
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

log = logging.getLogger("whatsapp-bot")

_service = None
_known_tabs: set[str] = set()

REPORT_HEADERS = ["Fecha", "Evento", "Detalle"]
CITAS_HEADERS = ["Fecha", "customer_wa_id", "nombre", "servicio", "horario", "estado"]


def _get_service():
    global _service
    if _service is not None:
        return _service

    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        return None

    info = json.loads(creds_json)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    _service = build("sheets", "v4", credentials=credentials)
    return _service


def _ensure_tab_exists(service, sheet_id: str, tab_name: str, headers: list[str]) -> None:
    if tab_name in _known_tabs:
        return

    metadata = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"] for s in metadata.get("sheets", [])}

    if tab_name not in existing:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
        ).execute()
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()

    _known_tabs.add(tab_name)


def _citas_tab_name() -> str:
    negocio = os.getenv("BUSINESS_NAME", "Sin nombre")
    return f"{negocio} - Citas"


def log_event(evento: str, detalle: str = "") -> None:
    """Agrega una fila a la pestaña del negocio actual (BUSINESS_NAME).
    Crea la pestaña sola si es la primera vez que se usa. Nunca lanza
    excepciones — un fallo aquí no debe tumbar la respuesta al cliente."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return

    try:
        service = _get_service()
        if not service:
            return

        tab_name = os.getenv("BUSINESS_NAME", "Sin nombre")
        _ensure_tab_exists(service, sheet_id, tab_name, REPORT_HEADERS)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[timestamp, evento, detalle]]},
        ).execute()
    except Exception:
        log.exception("No se pudo registrar el evento en Google Sheets")


def add_pending_appointment(customer_wa_id: str, req: dict) -> None:
    """Guarda una solicitud de cita como 'pendiente' en Sheets, para que
    sobreviva aunque el servidor se reinicie antes de que el dueño conteste."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        log.warning("GOOGLE_SHEET_ID no configurado: la cita no queda persistida")
        return

    try:
        service = _get_service()
        if not service:
            return
        tab = _citas_tab_name()
        _ensure_tab_exists(service, sheet_id, tab, CITAS_HEADERS)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        row = [timestamp, customer_wa_id, req["nombre"], req["servicio"], req["horario"], "pendiente"]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except Exception:
        log.exception("No se pudo guardar la solicitud de cita en Google Sheets")


def get_oldest_pending_appointment() -> dict | None:
    """Devuelve la solicitud de cita 'pendiente' más antigua, con su número
    de fila (necesario para poder marcarla como resuelta después)."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return None

    try:
        service = _get_service()
        if not service:
            return None
        tab = _citas_tab_name()
        _ensure_tab_exists(service, sheet_id, tab, CITAS_HEADERS)

        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:F"
        ).execute()
        rows = result.get("values", [])

        for i, row in enumerate(rows, start=2):
            estado = row[5] if len(row) > 5 else ""
            if estado == "pendiente":
                return {
                    "row_number": i,
                    "customer_wa_id": row[1],
                    "nombre": row[2],
                    "servicio": row[3],
                    "horario": row[4],
                }
        return None
    except Exception:
        log.exception("No se pudo leer las citas pendientes de Google Sheets")
        return None


def mark_appointment_resolved(row_number: int, estado: str) -> None:
    """Actualiza la columna 'estado' de una fila específica de citas."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return

    try:
        service = _get_service()
        if not service:
            return
        tab = _citas_tab_name()
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!F{row_number}",
            valueInputOption="RAW",
            body={"values": [[estado]]},
        ).execute()
    except Exception:
        log.exception("No se pudo marcar la cita como resuelta en Google Sheets")
