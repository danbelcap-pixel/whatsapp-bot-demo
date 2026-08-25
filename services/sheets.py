import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

log = logging.getLogger("whatsapp-bot")

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

_credentials = None
_known_tabs: set[str] = set()

# Reporte: 1 fila POR DÍA con contadores (no 1 fila por mensaje — con muchos
# negocios y mucho tráfico, una fila por evento haría la hoja inmanejable).
SUMMARY_HEADERS = [
    "Fecha", "Mensajes", "Citas solicitadas", "Citas confirmadas",
    "Citas rechazadas", "Errores", "No soportados (audio/sticker/etc)",
    "Alucinaciones bloqueadas", "Citas canceladas", "Citas modificadas",
    "Avisos de negocio actualizados",
]
EVENT_COLUMN = {
    "mensaje_respondido": 1,
    "cita_solicitada": 2,
    "cita_confirmada": 3,
    "cita_rechazada": 4,
    "error_envio": 5,
    "mensaje_no_soportado": 6,
    "alucinacion_detectada": 7,
    "cita_cancelada": 8,
    "aviso_negocio_actualizado": 10,
    "cita_modificada": 9,
}

CITAS_HEADERS = ["Folio", "Fecha", "customer_wa_id", "nombre", "servicio", "horario", "estado"]
_INACTIVE_STATES = ("rechazada", "cancelada_por_cliente")

# Pestaña de control con un renglón por negocio dado de alta — permite que
# un mismo despliegue atienda a varios negocios a la vez, cada uno con su
# propio número de WhatsApp, sin tocar código ni variables de entorno para
# dar de alta uno nuevo.
CLIENTES_TAB = "Clientes"
CLIENTES_HEADERS = [
    "Phone Number ID", "Nombre del negocio", "Teléfono del dueño",
    "Teléfonos adicionales", "Activo", "Información del negocio",
]


# ─── Capa de transporte: REST directo con `requests`, sin la librería ──────
# pesada google-api-python-client (que arrastra httplib2/protobuf/google-api-
# core y por sí sola inflaba la memoria del servidor hasta tumbarlo en el
# plan gratis de Render). Solo se usa google-auth, mucho más ligero, para
# firmar el token de acceso.

def _get_credentials():
    global _credentials
    if _credentials is not None:
        return _credentials
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        return None
    info = json.loads(creds_json)
    _credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return _credentials


def _auth_headers() -> dict | None:
    creds = _get_credentials()
    if not creds:
        return None
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


def _values_get(sheet_id: str, range_str: str) -> list[list]:
    headers = _auth_headers()
    if not headers:
        return []
    url = f"{SHEETS_API}/{sheet_id}/values/{quote(range_str, safe='')}"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json().get("values", [])


def _values_append(
    sheet_id: str, range_str: str, values: list[list],
    value_input_option: str = "USER_ENTERED", insert_data_option: str = "INSERT_ROWS",
) -> dict:
    headers = _auth_headers()
    if not headers:
        return {}
    url = f"{SHEETS_API}/{sheet_id}/values/{quote(range_str, safe='')}:append"
    params = {"valueInputOption": value_input_option, "insertDataOption": insert_data_option}
    resp = requests.post(url, headers=headers, params=params, json={"values": values}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _values_update(sheet_id: str, range_str: str, values: list[list], value_input_option: str = "RAW") -> None:
    headers = _auth_headers()
    if not headers:
        return
    url = f"{SHEETS_API}/{sheet_id}/values/{quote(range_str, safe='')}"
    params = {"valueInputOption": value_input_option}
    resp = requests.put(url, headers=headers, params=params, json={"values": values}, timeout=15)
    resp.raise_for_status()


def _batch_update(sheet_id: str, requests_body: list[dict]) -> dict:
    headers = _auth_headers()
    if not headers:
        return {}
    url = f"{SHEETS_API}/{sheet_id}:batchUpdate"
    resp = requests.post(url, headers=headers, json={"requests": requests_body}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _get_metadata(sheet_id: str) -> dict:
    headers = _auth_headers()
    if not headers:
        return {}
    resp = requests.get(f"{SHEETS_API}/{sheet_id}", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ─── Helpers de estructura de la hoja ───────────────────────────────────────

def _format_tab(sheet_id: str, sheet_tab_id: int, num_columns: int) -> None:
    """Encabezado en negritas, fila congelada, columnas ajustadas al contenido."""
    _batch_update(sheet_id, [
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
                "properties": {"sheetId": sheet_tab_id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_tab_id, "dimension": "COLUMNS",
                    "startIndex": 0, "endIndex": num_columns,
                }
            }
        },
    ])


def _ensure_tab_exists(sheet_id: str, tab_name: str, headers: list[str]) -> None:
    if tab_name in _known_tabs:
        return

    metadata = _get_metadata(sheet_id)
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in metadata.get("sheets", [])}

    if tab_name not in existing:
        add_result = _batch_update(sheet_id, [{"addSheet": {"properties": {"title": tab_name}}}])
        sheet_tab_id = add_result["replies"][0]["addSheet"]["properties"]["sheetId"]
        _values_append(sheet_id, f"'{tab_name}'!A1", [headers], value_input_option="RAW")
        _format_tab(sheet_id, sheet_tab_id, len(headers))

    _known_tabs.add(tab_name)


def _citas_tab_name(business_name: str) -> str:
    return f"{business_name} - Citas"


def _row_to_appointment(row: list, row_number: int) -> dict:
    return {
        "row_number": row_number,
        "folio": row[0],
        "customer_wa_id": row[2],
        "nombre": row[3],
        "servicio": row[4],
        "horario": row[5],
        "estado": row[6] if len(row) > 6 else "",
    }


# ─── Reporte diario ──────────────────────────────────────────────────────

def log_event(business_name: str, evento: str) -> None:
    """Suma 1 al contador del evento en la fila del día de hoy (crea la fila
    si es la primera vez hoy). Nunca lanza excepciones — un fallo aquí no
    debe tumbar la respuesta al cliente."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id or evento not in EVENT_COLUMN:
        return

    try:
        tab = business_name
        _ensure_tab_exists(sheet_id, tab, SUMMARY_HEADERS)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        col_index = EVENT_COLUMN[evento]
        col_letter = chr(ord("A") + col_index)

        rows = _values_get(sheet_id, f"'{tab}'!A2:A")
        dates = [row[0] if row else "" for row in rows]

        if today in dates:
            row_number = dates.index(today) + 2
            current = _values_get(sheet_id, f"'{tab}'!{col_letter}{row_number}")
            current_value = current[0][0] if current and current[0] else "0"
            _values_update(sheet_id, f"'{tab}'!{col_letter}{row_number}", [[int(current_value or 0) + 1]])
        else:
            row = [today] + [0] * (len(SUMMARY_HEADERS) - 1)
            row[col_index] = 1
            _values_append(sheet_id, f"'{tab}'!A1", [row])
    except Exception:
        log.exception("No se pudo registrar el evento en Google Sheets")


# ─── Citas ───────────────────────────────────────────────────────────────

def add_pending_appointment(business_name: str, customer_wa_id: str, req: dict) -> int | None:
    """Guarda una solicitud de cita como 'pendiente' en Sheets, para que
    sobreviva aunque el servidor se reinicie antes de que el dueño conteste.
    Devuelve el folio (número de fila) para poder referenciarla después."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        log.warning("GOOGLE_SHEET_ID no configurado: la cita no queda persistida")
        return None

    try:
        tab = _citas_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, CITAS_HEADERS)

        rows = _values_get(sheet_id, f"'{tab}'!A2:A")
        folio = len(rows) + 1

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        row = [folio, timestamp, customer_wa_id, req["nombre"], req["servicio"], req["horario"], "pendiente"]
        _values_append(sheet_id, f"'{tab}'!A1", [row])
        return folio
    except Exception:
        log.exception("No se pudo guardar la solicitud de cita en Google Sheets")
        return None


def get_pending_appointment_by_folio(business_name: str, folio: int) -> dict | None:
    """Busca una solicitud pendiente por su número de folio (para cuando el
    dueño tiene varias solicitudes a la vez y contesta una específica)."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return None
    try:
        tab = _citas_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, CITAS_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:G")
        for i, row in enumerate(rows, start=2):
            if len(row) >= 7 and row[6] == "pendiente" and str(row[0]) == str(folio):
                return _row_to_appointment(row, i)
        return None
    except Exception:
        log.exception("No se pudo buscar la cita por folio en Google Sheets")
        return None


def list_pending_appointments(business_name: str) -> list[dict]:
    """Devuelve TODAS las solicitudes pendientes (no solo la más antigua) —
    para mostrárselas completas al dueño cuando tiene que elegir cuál."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return []
    try:
        tab = _citas_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, CITAS_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:G")
        return [
            _row_to_appointment(row, i)
            for i, row in enumerate(rows, start=2)
            if len(row) >= 7 and row[6] == "pendiente"
        ]
    except Exception:
        log.exception("No se pudo listar las citas pendientes de Google Sheets")
        return []


def get_customer_active_appointments(business_name: str, wa_id: str) -> list[dict]:
    """Citas activas (ni rechazadas ni canceladas) de un cliente específico —
    para que el bot sepa qué citas ya tiene antes de crear, cancelar o
    modificar, en vez de adivinar o confundir una cita nueva con una vieja."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return []
    try:
        tab = _citas_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, CITAS_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:G")
        return [
            _row_to_appointment(row, i)
            for i, row in enumerate(rows, start=2)
            if len(row) >= 7 and row[2] == wa_id and row[6] not in _INACTIVE_STATES
        ]
    except Exception:
        log.exception("No se pudo leer las citas activas del cliente en Google Sheets")
        return []


def get_appointment_by_folio(business_name: str, folio: int) -> dict | None:
    """Busca una cita por folio sin importar su estado (a diferencia de
    get_pending_appointment_by_folio, que solo busca 'pendiente') — para
    validar cancelaciones/modificaciones contra el estado real."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return None
    try:
        tab = _citas_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, CITAS_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:G")
        for i, row in enumerate(rows, start=2):
            if len(row) >= 7 and str(row[0]) == str(folio):
                return _row_to_appointment(row, i)
        return None
    except Exception:
        log.exception("No se pudo buscar la cita por folio en Google Sheets")
        return None


def update_appointment_horario(business_name: str, row_number: int, nuevo_horario: str) -> None:
    """Actualiza solo la columna 'horario' de una fila específica."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return
    try:
        tab = _citas_tab_name(business_name)
        _values_update(sheet_id, f"'{tab}'!F{row_number}", [[nuevo_horario]])
    except Exception:
        log.exception("No se pudo actualizar el horario de la cita en Google Sheets")


def mark_appointment_resolved(business_name: str, row_number: int, estado: str) -> None:
    """Actualiza la columna 'estado' de una fila específica de citas."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return
    try:
        tab = _citas_tab_name(business_name)
        _values_update(sheet_id, f"'{tab}'!G{row_number}", [[estado]])
    except Exception:
        log.exception("No se pudo marcar la cita como resuelta en Google Sheets")


# ─── Config de negocios (multi-tenant) ──────────────────────────────────

def get_business_config_row(phone_number_id: str) -> dict | None:
    """Busca en la pestaña 'Clientes' el negocio dueño de este
    phone_number_id (el número de WhatsApp que recibió el mensaje). None si
    no hay ningún negocio dado de alta con ese número, o si está marcado
    como inactivo (cliente canceló mantenimiento — se apaga sin borrar
    nada, por si vuelve a activarse después)."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return None
    try:
        _ensure_tab_exists(sheet_id, CLIENTES_TAB, CLIENTES_HEADERS)
        rows = _values_get(sheet_id, f"'{CLIENTES_TAB}'!A2:F")
        for row in rows:
            if len(row) >= 1 and row[0].strip() == phone_number_id:
                activo = row[4].strip().upper() if len(row) > 4 else "SI"
                if activo not in ("SI", "SÍ", "YES", "TRUE", "1"):
                    return None
                return {
                    "phone_number_id": row[0].strip(),
                    "name": row[1].strip() if len(row) > 1 else "",
                    "owner_phone": row[2].strip() if len(row) > 2 else "",
                    "notify_also": row[3].strip() if len(row) > 3 else "",
                    "info": row[5].strip() if len(row) > 5 else "",
                }
        return None
    except Exception:
        log.exception("No se pudo leer la pestaña Clientes de Google Sheets")
        return None
