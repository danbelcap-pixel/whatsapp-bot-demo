import base64
import logging
import os
import re

import requests
from dotenv import load_dotenv
from flask import Flask, request

from agent.client import ask_agent, interpret_owner_instruction
from services.memory import is_duplicate_message, save_business_notice
from services.sheets import (
    add_pending_appointment,
    count_pending_appointments,
    get_appointment_by_folio,
    get_pending_appointment_by_folio,
    list_pending_appointments,
    log_event,
    mark_appointment_resolved,
    update_appointment_horario,
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


def get_owner_number() -> str | None:
    """OWNER_PHONE_NUMBER normalizado — así no importa si se capturó con o
    sin el '1' extra mexicano, siempre coincide contra el wa_id normalizado
    de los mensajes entrantes (si no, el dueño se trataría como cliente)."""
    owner = os.getenv("OWNER_PHONE_NUMBER")
    return normalize_mx_number(owner) if owner else None


def get_notify_also_numbers() -> list[str]:
    """Números adicionales (recepcionista, encargados) que reciben copia
    informativa de los avisos de citas — opcional, solo si el negocio lo
    pide. A diferencia del dueño, no pueden confirmar/cancelar/mover citas:
    si le escriben al bot, se les trata como clientes normales, para no
    tener dos personas con poder de decisión sobre la misma solicitud."""
    raw = os.getenv("NOTIFY_ALSO_PHONE_NUMBERS", "")
    return [normalize_mx_number(n.strip()) for n in raw.split(",") if n.strip()]


def notify_staff(owner_message: str, staff_note: str) -> None:
    """Manda el aviso completo (con instrucciones de acción) al dueño, y una
    copia informativa (sin instrucciones, para no confundirlos con acciones
    que no pueden tomar) a cualquier encargado adicional configurado."""
    owner = get_owner_number()
    if owner:
        send_whatsapp_message(owner, owner_message)
    for number in get_notify_also_numbers():
        send_whatsapp_message(
            number,
            f"(Aviso informativo — solo el encargado principal puede "
            f"confirmar/cancelar)\n\n{staff_note}",
        )


def fetch_whatsapp_media(media_id: str) -> tuple[bytes, str] | None:
    """Descarga un archivo multimedia de WhatsApp (foto, audio, etc.).
    Devuelve (bytes_del_archivo, mime_type) o None si algo falla."""
    token = os.getenv("WHATSAPP_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        meta_resp = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}",
            headers=headers,
            timeout=15,
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()

        file_resp = requests.get(meta["url"], headers=headers, timeout=20)
        file_resp.raise_for_status()
        return file_resp.content, meta.get("mime_type", "image/jpeg")
    except requests.RequestException:
        log.exception("No se pudo descargar el archivo multimedia %s", media_id)
        return None


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
    if not get_owner_number():
        alert_daniel(
            f"Se pidió una cita pero OWNER_PHONE_NUMBER no está configurado: {req}"
        )
        return

    folio_txt = f"#{folio}" if folio else "#?"
    instrucciones = f"Contesta *{folio_txt} SI* para confirmar, *{folio_txt} NO* para rechazar, u otro horario."
    if pending_count <= 1:
        # Es la única pendiente — no hace falta que escriba el folio
        instrucciones = "Contesta *SI* para confirmar, *NO* para rechazar, u otro horario."

    hechos = (
        f"📅 Solicitud de cita {folio_txt}\n"
        f"Nombre: {req['nombre']}\n"
        f"Servicio: {req['servicio']}\n"
        f"Horario pedido: {req['horario']}"
    )
    notify_staff(f"{hechos}\n\n{instrucciones}", hechos)


def notify_owner_cancellation(cita: dict) -> None:
    if not get_owner_number():
        return
    hechos = (
        f"❌ Folio #{cita['folio']} cancelada por el cliente\n"
        f"Nombre: {cita['nombre']}\n"
        f"Servicio: {cita['servicio']}\n"
        f"Horario: {cita['horario']}"
    )
    notify_staff(hechos, hechos)


def notify_owner_modification(cita: dict, nuevo_horario: str, pending_count: int) -> None:
    if not get_owner_number():
        alert_daniel(
            f"Se pidió mover el folio #{cita['folio']} pero OWNER_PHONE_NUMBER "
            f"no está configurado."
        )
        return

    folio_txt = f"#{cita['folio']}"
    instrucciones = f"Contesta *{folio_txt} SI* para confirmar, *{folio_txt} NO* para rechazar, u otro horario."
    if pending_count <= 1:
        instrucciones = "Contesta *SI* para confirmar, *NO* para rechazar, u otro horario."

    hechos = (
        f"🔄 Folio {folio_txt} — cambio de horario solicitado\n"
        f"Nombre: {cita['nombre']}\n"
        f"Servicio: {cita['servicio']}\n"
        f"Horario anterior: {cita['horario']}\n"
        f"Horario nuevo pedido: {nuevo_horario}"
    )
    notify_staff(f"{hechos}\n\n{instrucciones}", hechos)


def _resolve_citas_reply(req: dict, reply_text: str) -> None:
    """Aplica la respuesta del dueño (SI/NO/horario alternativo) a una
    solicitud de cita específica ya identificada sin ambigüedad."""
    reply_normalized = reply_text.strip().lower()

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
            f"proponemos: {reply_text}. ¿Te funciona?",
        )
        mark_appointment_resolved(req["row_number"], f"horario_alternativo: {reply_text[:100]}")


def _send_pending_list_prompt(owner: str, pending: list[dict]) -> None:
    lista = "\n".join(
        f"#{p['folio']} — {p['nombre']} ({p['servicio']}, {p['horario']})" for p in pending
    )
    send_whatsapp_message(
        owner,
        f"Tienes {len(pending)} solicitudes de cita pendientes — dime a cuál te "
        f"refieres empezando tu respuesta con su folio, ej. '#{pending[0]['folio']} SI':\n\n{lista}",
    )


def handle_owner_reply(text: str) -> None:
    owner = get_owner_number()
    text = text.strip()

    # Formato estricto y explícito para referenciar un folio: '#3 SI'.
    # A propósito NO se busca cualquier número suelto en el texto — una
    # respuesta como "a las 17:00" contiene un número que NO es un folio,
    # y confundirlo con uno resolvería la cita equivocada. Cero margen para
    # eso: o el folio viene marcado sin ambigüedad, o no se adivina nada.
    folio_match = re.match(r"^#(\d+)\s+(.*)$", text)
    if folio_match:
        req = get_pending_appointment_by_folio(int(folio_match.group(1)))
        if not req:
            send_whatsapp_message(
                owner,
                f"No encontré una solicitud pendiente con el folio #{folio_match.group(1)}. "
                f"Revisa el número e intenta de nuevo.",
            )
            return
        _resolve_citas_reply(req, folio_match.group(2).strip())
        return

    pending = list_pending_appointments()
    is_exact_reply = text.lower() in ("si", "sí", "yes", "ok", "dale", "no")

    if pending and len(pending) == 1 and is_exact_reply:
        # Único caso simple e inequívoco sin necesitar folio ni Claude.
        _resolve_citas_reply(pending[0], text)
        return

    if pending and len(pending) > 1 and is_exact_reply:
        # SI/NO sin folio con varias pendientes — no se adivina cuál.
        _send_pending_list_prompt(owner, pending)
        return

    # Aquí el texto no es un SI/NO limpio (o no hay nada pendiente) — es
    # ambiguo entre "propuesta de horario para la única pendiente" y "aviso
    # de negocio" (cierre, cambio de horario). No se asume, se le pregunta
    # a Claude qué es en realidad.
    aviso = interpret_owner_instruction(text)
    if aviso:
        save_business_notice(aviso)
        send_whatsapp_message(owner, f'Anotado — les voy a avisar a los clientes: "{aviso}"')
        log_event("aviso_negocio_actualizado")
        return

    if not pending:
        send_whatsapp_message(
            owner,
            "No tengo ninguna solicitud de cita pendiente ahora mismo. "
            "Si quieres avisar de un cierre especial o cambio de horario, "
            "dime algo como 'mañana cerraremos' o 'el jueves cerramos a las 2pm'.",
        )
    elif len(pending) == 1:
        _resolve_citas_reply(pending[0], text)
    else:
        _send_pending_list_prompt(owner, pending)


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
        msg_type = incoming.get("type", "text")
        message_id = incoming.get("id", "")
    except (KeyError, IndexError):
        log.warning("Payload de webhook con formato inesperado: %s", payload)
        return "OK", 200

    if is_duplicate_message(message_id):
        log.info("Mensaje %s duplicado (reintento de Meta), ignorado.", message_id)
        return "OK", 200

    log.info("Mensaje de %s (tipo: %s)", wa_id, msg_type)

    owner = get_owner_number()
    if owner and wa_id == owner:
        handle_owner_reply(incoming.get("text", {}).get("body", ""))
        return "OK", 200

    stored_content = None  # si se define, es lo que se guarda en el historial en vez de user_content

    if msg_type == "text":
        user_content = incoming.get("text", {}).get("body", "")

    elif msg_type == "image":
        media = fetch_whatsapp_media(incoming["image"]["id"])
        if not media:
            send_whatsapp_message(wa_id, "No pude abrir tu foto, ¿me la puedes describir por texto?")
            return "OK", 200
        image_bytes, mime_type = media
        caption = incoming["image"].get("caption", "")
        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode(),
                },
            },
            {"type": "text", "text": caption or "(el cliente mandó esta foto sin descripción)"},
        ]
        # No guardamos la foto completa en el historial — si se queda ahí,
        # se reenvía entera en cada turno futuro de la conversación, e infla
        # memoria y costo de API cada vez más mientras dure el chat.
        stored_content = f"[Foto adjunta{f': {caption}' if caption else ' (sin descripción)'}]"

    else:
        send_whatsapp_message(
            wa_id,
            "Por ahora solo puedo leer mensajes de texto o fotos — ¿me lo escribes? 🙏",
        )
        log_event("mensaje_no_soportado")
        return "OK", 200

    reply, action, hallucination_detected = ask_agent(
        wa_id, user_content, stored_message=stored_content
    )
    send_whatsapp_message(wa_id, reply)
    log_event("mensaje_respondido")

    if hallucination_detected:
        alert_daniel(
            f"El bot casi confirma una acción falsa de cita a {wa_id} sin "
            f"usar la herramienta real — se bloqueó automáticamente, pero "
            f"revisa el prompt, esto no debería pasar."
        )
        log_event("alucinacion_detectada")

    if action and action["type"] == "crear":
        folio = add_pending_appointment(wa_id, action)
        action["customer_wa_id"] = wa_id
        pending_count = count_pending_appointments()
        notify_owner_new_request(action, folio, pending_count)
        log_event("cita_solicitada")

    elif action and action["type"] in ("cancelar", "modificar"):
        # Verificación de seguridad: el folio debe pertenecer a QUIEN
        # escribió, sin importar lo que haya dicho Claude — nunca se confía
        # ciegamente en que el modelo eligió el folio correcto.
        cita = get_appointment_by_folio(action["folio"])
        if not cita or cita["customer_wa_id"] != wa_id:
            alert_daniel(
                f"{wa_id} intentó {action['type']} el folio #{action['folio']}, "
                f"que no existe o no le pertenece — bloqueado."
            )
        elif action["type"] == "cancelar":
            mark_appointment_resolved(cita["row_number"], "cancelada_por_cliente")
            notify_owner_cancellation(cita)
            log_event("cita_cancelada")
        else:  # modificar
            update_appointment_horario(cita["row_number"], action["nuevo_horario"])
            mark_appointment_resolved(cita["row_number"], "pendiente")  # requiere reconfirmar
            pending_count = count_pending_appointments()
            notify_owner_modification(cita, action["nuevo_horario"], pending_count)
            log_event("cita_modificada")

    return "OK", 200


@app.get("/")
def health():
    return "WhatsApp bot demo activo", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
