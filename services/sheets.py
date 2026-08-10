import json
import logging
import os
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

log = logging.getLogger("whatsapp-bot")

_service = None
_known_tabs: set[str] = set()

# Reporte: 1 fila POR DÍA con contadores (no 1 fila por mensaje — con muchos
# negocios y mucho tráfico, una fila por evento haría la hoja inmanejable).
SUMMARY_HEADERS = [
    "Fecha", "Mensajes", "Citas solicitadas", "Citas confirmadas",
    "Citas rechazadas", "Errores", "No soportados (audio/sticker/etc)",
]
EVENT_COLUMN = {
    "mensaje_respondido": 1,
    "cita_solicitada": 2,
    "cita_confirmada": 3,
    "cita_rechazada": 4,
    "error_envio": 5,
    "mensaje_no_soportado": 6,
}

CITAS_HEADERS = ["Folio", "Fecha", "customer_wa_id", "nombre", "servicio", "horario", "estado"]


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


def _format_tab(service, sheet_id: str, sheet_tab_id: int, num_columns: int) -> None:
    """Encabezado en negritas, fila congelada, columnas ajustadas al contenido."""
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_tab_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True},
                                "backgroundColor": {"red": 0.90, "green": 0.90, "blue": 0.90},
                            }
                        },
                        "fields": "userEnteredFormat(textFormat,backgroundColor)",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_tab_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_tab_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": num_columns,
                        }
                    }
                },
            ]
        },
    ).execute()


def _ensure_tab_exists(service, sheet_id: str, tab_name: str, headers: list[str]) -> None:
    if tab_name in _known_tabs:
        return

    metadata = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in metadata.get("sheets", [])}

    if tab_name not in existing:
        add_result = service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
        ).execute()
        sheet_tab_id = add_result["replies"][0]["addSheet"]["properties"]["sheetId"]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab_name}'!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()
        _format_tab(service, sheet_id, sheet_tab_id, len(headers))

    _known_tabs.add(tab_name)


def _citas_tab_name() -> str:
    negocio = os.getenv("BUSINESS_NAME", "Sin nombre")
    return f"{negocio} - Citas"


def log_event(evento: str) -> None:
    """Suma 1 al contador del evento en la fila del día de hoy (crea la fila
    si es la primera vez hoy). Nunca lanza excepciones — un fallo aquí no
    debe tumbar la respuesta al cliente."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id or evento not in EVENT_COLUMN:
        return

    try:
        service = _get_service()
        if not service:
            return

        tab = os.getenv("BUSINESS_NAME", "Sin nombre")
        _ensure_tab_exists(service, sheet_id, tab, SUMMARY_HEADERS)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        col_index = EVENT_COLUMN[evento]
        col_letter = chr(ord("A") + col_index)

        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:A"
        ).execute()
        dates = [row[0] if row else "" for row in result.get("values", [])]

        if today in dates:
            row_number = dates.index(today) + 2
            current = service.spreadsheets().values().get(
                spreadsheetId=sheet_id, range=f"'{tab}'!{col_letter}{row_number}"
            ).execute()
            current_value = current.get("values", [["0"]])[0][0] if current.get("values") else "0"
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"'{tab}'!{col_letter}{row_number}",
                valueInputOption="RAW",
                body={"values": [[int(current_value or 0) + 1]]},
            ).execute()
        else:
            row = [today] + [0] * (len(SUMMARY_HEADERS) - 1)
            row[col_index] = 1
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range=f"'{tab}'!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
    except Exception:
        log.exception("No se pudo registrar el evento en Google Sheets")


def add_pending_appointment(customer_wa_id: str, req: dict) -> int | None:
    """Guarda una solicitud de cita como 'pendiente' en Sheets, para que
    sobreviva aunque el servidor se reinicie antes de que el dueño conteste.
    Devuelve el folio (número de fila) para poder referenciarla después."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        log.warning("GOOGLE_SHEET_ID no configurado: la cita no queda persistida")
        return None

    try:
        service = _get_service()
        if not service:
            return None
        tab = _citas_tab_name()
        _ensure_tab_exists(service, sheet_id, tab, CITAS_HEADERS)

        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:A"
        ).execute()
        folio = len(result.get("values", [])) + 1

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        row = [folio, timestamp, customer_wa_id, req["nombre"], req["servicio"], req["horario"], "pendiente"]
        service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return folio
    except Exception:
        log.exception("No se pudo guardar la solicitud de cita en Google Sheets")
        return None


def _row_to_appointment(row: list, row_number: int) -> dict:
    return {
        "row_number": row_number,
        "folio": row[0],
        "customer_wa_id": row[2],
        "nombre": row[3],
        "servicio": row[4],
        "horario": row[5],
    }


def get_pending_appointment_by_folio(folio: int) -> dict | None:
    """Busca una solicitud pendiente por su número de folio (para cuando el
    dueño tiene varias solicitudes a la vez y contesta una específica)."""
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
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:G"
        ).execute()
        rows = result.get("values", [])

        for i, row in enumerate(rows, start=2):
            if len(row) >= 7 and row[6] == "pendiente" and str(row[0]) == str(folio):
                return _row_to_appointment(row, i)
        return None
    except Exception:
        log.exception("No se pudo buscar la cita por folio en Google Sheets")
        return None


def get_oldest_pending_appointment() -> dict | None:
    """Devuelve la solicitud de cita 'pendiente' más antigua (fallback
    cuando el dueño no especifica un folio)."""
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
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:G"
        ).execute()
        rows = result.get("values", [])

        for i, row in enumerate(rows, start=2):
            if len(row) >= 7 and row[6] == "pendiente":
                return _row_to_appointment(row, i)
        return None
    except Exception:
        log.exception("No se pudo leer las citas pendientes de Google Sheets")
        return None


def count_pending_appointments() -> int:
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return 0
    try:
        service = _get_service()
        if not service:
            return 0
        tab = _citas_tab_name()
        _ensure_tab_exists(service, sheet_id, tab, CITAS_HEADERS)
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A2:G"
        ).execute()
        rows = result.get("values", [])
        return sum(1 for row in rows if len(row) >= 7 and row[6] == "pendiente")
    except Exception:
        log.exception("No se pudo contar las citas pendientes en Google Sheets")
        return 0


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
            range=f"'{tab}'!G{row_number}",
            valueInputOption="RAW",
            body={"values": [[estado]]},
        ).execute()
    except Exception:
        log.exception("No se pudo marcar la cita como resuelta en Google Sheets")
