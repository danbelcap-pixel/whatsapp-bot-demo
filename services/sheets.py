import json
import logging
import os
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

log = logging.getLogger("whatsapp-bot")

_service = None
_known_tabs: set[str] = set()


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


def _ensure_tab_exists(service, sheet_id: str, tab_name: str) -> None:
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
            body={"values": [["Fecha", "Evento", "Detalle"]]},
        ).execute()

    _known_tabs.add(tab_name)


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
        _ensure_tab_exists(service, sheet_id, tab_name)

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
