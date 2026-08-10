import logging
import os
import re

import requests
from dotenv import load_dotenv
from flask import Flask, request

from agent.client import ask_agent
from services.sheets import (
    add_pending_appointment,
    count_pending_appointments,
    get_oldest_pending_appointment,
    get_pending_appointment_by_folio,
    log_event,
    mark_appointment_resolved,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("whatsapp-bot")

app = Flask(__name__)

GRAPH_API_VERSION = "v21.0"


def normalize_mx_number(wa_id: str) -> str:
    """Meta antepone un '1' extra tras el '52' en el campo 'from' de
    mensajes entrantes de México, pero no lo acepta al enviar/autorizar."""
    if wa_id.startswith("521") and len(wa_id) == 13:
        return "52" + wa_id[3:]
    return wa_id


def send_whatsapp_message(to: str, body: str) -> None:
    token = os.getenv("WHATSAPP_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        log.error("Error enviando mensaje a WhatsApp: %s %s", resp.status_code, resp.text)
        alert_daniel(f"Fallo al enviar mensaje a {to}: HTTP {resp.status_code} — {resp.text[:200]}")
        log_event("error_envio")


def alert_daniel(message: str) -> None:
    """Manda la alerta por Telegram: canal independiente de WhatsApp/Meta,
    para que siga funcionando aunque WhatsApp sea justo lo que está fallando."""
    bot_token = os.getenv("TELEGRAM_ALERT_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID")
    if not bot_token or not chat_id:
        return
    negocio = os.getenv("BUSINESS_NAME", "bot")
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": f"⚠️ [{negocio}] {message}"},
            timeout=10,
        )
    except requests.RequestException:
        log.exception("No se pudo mandar la alerta por Telegram")


def notify_owner_new_request(req: dict, folio: int | None, pending_count: int) -> None:
    owner = os.getenv("OWNER_PHONE_NUMBER")
    if not owner:
        alert_daniel(
            f"Se pidió una cita pero OWNER_PHONE_NUMBER no está configurado: {req}"
        )
        return

    folio_txt = f"#{folio}" if folio else "#?"
    instrucciones = f"Contesta *{folio_txt} SI* para confirmar, *{folio_txt} NO* para rechazar, u otro horario."
    if pending_count <= 1:
        # Es la única pendiente — no hace falta que escriba el folio
        instrucciones = "Contesta *SI* para confirmar, *NO* para rechazar, u otro horario."

    send_whatsapp_message(
        owner,
        f"📅 Solicitud de cita {folio_txt}\n"
        f"Nombre: {req['nombre']}\n"
        f"Servicio: {req['servicio']}\n"
        f"Horario pedido: {req['horario']}\n\n"
        f"{instrucciones}",
    )


def handle_owner_reply(text: str) -> None:
    owner = os.getenv("OWNER_PHONE_NUMBER")

    folio_match = re.search(r"\d+", text)
    if folio_match:
        req = get_pending_appointment_by_folio(int(folio_match.group()))
        # Quita el número para quedarnos solo con la palabra de respuesta
        text_sin_folio = re.sub(r"\d+", "", text).strip()
    else:
        req = get_oldest_pending_appointment()
        text_sin_folio = text

    if not req:
        send_whatsapp_message(
            owner,
            "No encontré esa solicitud de cita pendiente (o ya no hay ninguna). "
            "Si tienes varias a la vez, incluye el folio, ej. '3 SI'.",
        )
        return

    reply_normalized = text_sin_folio.strip().lower()

    if reply_normalized in ("si", "sí", "yes", "ok", "dale"):
        send_whatsapp_message(
            req["customer_wa_id"],
            f"¡Confirmado! Tu cita para {req['servicio']} quedó agendada "
            f"el {req['horario']}. Te esperamos 🙌",
        )
        mark_appointment_resolved(req["row_number"], "confirmada")
        log_event("cita_confirmada")
    elif reply_normalized == "no":
        send_whatsapp_message(
            req["customer_wa_id"],
            "Ese horario no está disponible. ¿Tienes otro horario que te "
            "funcione? Escríbenos de nuevo para revisar otra opción.",
        )
        mark_appointment_resolved(req["row_number"], "rechazada")
        log_event("cita_rechazada")
    else:
        # El dueño escribió un horario alternativo en texto libre
        send_whatsapp_message(
            req["customer_wa_id"],
            f"Tu horario solicitado no estaba disponible, pero te "
            f"proponemos: {text_sin_folio}. ¿Te funciona?",
        )
        mark_appointment_resolved(req["row_number"], f"horario_alternativo: {text_sin_folio[:100]}")


@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    expected_token = os.getenv("WHATSAPP_VERIFY_TOKEN")
    if mode == "subscribe" and token == expected_token:
        log.info("Webhook verificado por Meta.")
        return challenge, 200

    log.warning("Verificación de webhook fallida (token no coincide).")
    return "Forbidden", 403


@app.post("/webhook")
def receive_message():
    payload = request.get_json(silent=True) or {}

    try:
        change_value = payload["entry"][0]["changes"][0]["value"]
        messages = change_value.get("messages")
        if not messages:
            # Eventos que no son mensajes entrantes (estados de entrega, etc.)
            return "OK", 200

        incoming = messages[0]
        wa_id = normalize_mx_number(incoming["from"])
        text = incoming.get("text", {}).get("body", "")
    except (KeyError, IndexError):
        log.warning("Payload de webhook con formato inesperado: %s", payload)
        return "OK", 200

    log.info("Mensaje de %s: %s", wa_id, text)

    owner = os.getenv("OWNER_PHONE_NUMBER")
    if owner and wa_id == owner:
        handle_owner_reply(text)
        return "OK", 200

    reply, booking_request = ask_agent(wa_id, text)
    send_whatsapp_message(wa_id, reply)
    log_event("mensaje_respondido")

    if booking_request:
        folio = add_pending_appointment(wa_id, booking_request)
        booking_request["customer_wa_id"] = wa_id
        pending_count = count_pending_appointments()
        notify_owner_new_request(booking_request, folio, pending_count)
        log_event("cita_solicitada")

    return "OK", 200


@app.get("/")
def health():
    return "WhatsApp bot demo activo", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
