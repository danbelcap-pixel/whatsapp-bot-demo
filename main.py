import base64
import hashlib
import hmac
import logging
import os
import re

import requests
from dotenv import load_dotenv
from flask import Flask, request

from agent.client import ask_agent, interpret_owner_instruction
from services.business import get_business_config
from services.memory import is_duplicate_message, save_business_notice
from services.sheets import (
    add_pending_appointment,
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


def get_owner_number(business: dict) -> str | None:
    """Teléfono del dueño de ESTE negocio, normalizado — así no importa si
    se capturó con o sin el '1' extra mexicano, siempre coincide contra el
    wa_id normalizado de los mensajes entrantes (si no, el dueño se
    trataría como cliente)."""
    owner = business.get("owner_phone")
    return normalize_mx_number(owner) if owner else None


def get_notify_also_numbers(business: dict) -> list[str]:
    """Números adicionales (recepcionista, encargados) que reciben copia
    informativa de los avisos de citas — opcional, solo si el negocio lo
    pide. A diferencia del dueño, no pueden confirmar/cancelar/mover citas:
    si le escriben al bot, se les trata como clientes normales, para no
    tener dos personas con poder de decisión sobre la misma solicitud."""
    raw = business.get("notify_also", "") or ""
    return [normalize_mx_number(n.strip()) for n in raw.split(",") if n.strip()]


def notify_staff(
    business: dict,
    owner_template: str, owner_params: list[str],
    notify_template: str, notify_params: list[str],
) -> None:
    """Manda la plantilla con instrucciones de acción al dueño, y una
    plantilla informativa (sin instrucciones, para no confundirlos con
    acciones que no pueden tomar) a cualquier encargado adicional
    configurado. Usa plantillas, no texto libre, porque este aviso lo
    inicia el bot por su cuenta — no es respuesta a un mensaje reciente."""
    owner = get_owner_number(business)
    if owner:
        send_whatsapp_template(business, owner, owner_template, owner_params)
    for number in get_notify_also_numbers(business):
        send_whatsapp_template(business, number, notify_template, notify_params)


def fetch_whatsapp_media(media_id: str) -> tuple[bytes, str] | None:
    """Descarga un archivo multimedia de WhatsApp (foto, audio, etc.).
    Devuelve (bytes_del_archivo, mime_type) o None si algo falla. No
    depende del negocio: el token es el mismo para toda la cuenta de Meta,
    sin importar qué número de la cuenta recibió el archivo."""
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


def send_whatsapp_message(business: dict, to: str, body: str) -> None:
    """Manda el mensaje DESDE el número de WhatsApp de este negocio
    específico — cada negocio dado de alta en la pestaña Clientes tiene su
    propio phone_number_id, todos bajo la misma cuenta de Meta y el mismo
    token, así que un solo despliegue puede mandar mensajes "como" varios
    negocios distintos al mismo tiempo."""
    token = os.getenv("WHATSAPP_TOKEN")
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{business['phone_number_id']}/messages"

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
        alert_daniel(business, f"Fallo al enviar mensaje a {to}: HTTP {resp.status_code} — {resp.text[:200]}")
        if business:
            log_event(business["name"], "error_envio")


def send_whatsapp_template(business: dict, to: str, template_name: str, params: list[str]) -> None:
    """Manda un mensaje usando una plantilla pre-aprobada por Meta. A
    diferencia de send_whatsapp_message (texto libre), esta SÍ funciona
    aunque hayan pasado más de 24h desde el último mensaje de esa persona —
    WhatsApp exige plantilla para que el negocio inicie una conversación
    fuera de esa ventana, y rechaza el texto libre en ese caso. Se usa para
    todo aviso que el bot manda por su cuenta (no como respuesta inmediata
    a algo que la persona acaba de escribir)."""
    token = os.getenv("WHATSAPP_TOKEN")
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{business['phone_number_id']}/messages"

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": "es_MX"},
                "components": (
                    [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
                    if params else []
                ),
            },
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        log.error("Error enviando plantilla '%s': %s %s", template_name, resp.status_code, resp.text)
        alert_daniel(business, f"Fallo al enviar plantilla '{template_name}' a {to}: HTTP {resp.status_code} — {resp.text[:200]}")
        log_event(business["name"], "error_envio")


def alert_daniel(business: dict | None, message: str) -> None:
    """Manda la alerta por Telegram: canal independiente de WhatsApp/Meta,
    para que siga funcionando aunque WhatsApp sea justo lo que está
    fallando. Se etiqueta con el nombre del negocio para poder distinguir
    de cuál de varios clientes activos viene la alerta."""
    bot_token = os.getenv("TELEGRAM_ALERT_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID")
    if not bot_token or not chat_id:
        return
    negocio = business["name"] if business else "número no registrado"
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": f"⚠️ [{negocio}] {message}"},
            timeout=10,
        )
    except requests.RequestException:
        log.exception("No se pudo mandar la alerta por Telegram")


def notify_owner_new_request(business: dict, req: dict, folio: int | None) -> None:
    if not get_owner_number(business):
        alert_daniel(business, f"Se pidió una cita pero este negocio no tiene teléfono del dueño configurado: {req}")
        return

    params = [str(folio) if folio else "?", req["nombre"], req["servicio"], req["horario"]]
    notify_staff(business, "cita_nueva_solicitud", params, "cita_nueva_informativa", params)


def notify_owner_cancellation(business: dict, cita: dict) -> None:
    if not get_owner_number(business):
        return
    params = [str(cita["folio"]), cita["nombre"], cita["servicio"], cita["horario"]]
    notify_staff(business, "cita_cancelada", params, "cita_cancelada", params)


def notify_owner_modification(business: dict, cita: dict, nuevo_horario: str) -> None:
    if not get_owner_number(business):
        alert_daniel(business, f"Se pidió mover el folio #{cita['folio']} pero este negocio no tiene teléfono del dueño configurado.")
        return

    params = [str(cita["folio"]), cita["nombre"], cita["servicio"], cita["horario"], nuevo_horario]
    notify_staff(business, "cita_cambio_solicitado", params, "cita_cambio_informativa", params)


def _resolve_citas_reply(business: dict, req: dict, reply_text: str) -> None:
    """Aplica la respuesta del dueño (SI/NO/horario alternativo) a una
    solicitud de cita específica ya identificada sin ambigüedad."""
    negocio = business["name"]
    reply_normalized = reply_text.strip().lower()

    # Estos avisos van al CLIENTE, y el bot los inicia por su cuenta — el
    # dueño pudo haber tardado horas en contestar, así que no se puede
    # asumir que la ventana de 24h del cliente siga abierta. Por eso usan
    # plantilla, no texto libre.
    if reply_normalized in ("si", "sí", "yes", "ok", "dale"):
        send_whatsapp_template(
            business, req["customer_wa_id"], "cita_confirmada_cliente",
            [req["servicio"], req["horario"]],
        )
        mark_appointment_resolved(negocio, req["row_number"], "confirmada")
        log_event(negocio, "cita_confirmada")
    elif reply_normalized == "no":
        send_whatsapp_template(business, req["customer_wa_id"], "cita_rechazada_cliente", [])
        mark_appointment_resolved(negocio, req["row_number"], "rechazada")
        log_event(negocio, "cita_rechazada")
    else:
        # El dueño escribió un horario alternativo en texto libre
        send_whatsapp_template(
            business, req["customer_wa_id"], "cita_horario_alternativo_cliente",
            [reply_text],
        )
        mark_appointment_resolved(negocio, req["row_number"], f"horario_alternativo: {reply_text[:100]}")


def _send_pending_list_prompt(business: dict, owner: str, pending: list[dict]) -> None:
    lista = "\n".join(
        f"#{p['folio']} — {p['nombre']} ({p['servicio']}, {p['horario']})" for p in pending
    )
    send_whatsapp_message(
        business,
        owner,
        f"Tienes {len(pending)} solicitudes de cita pendientes — dime a cuál te "
        f"refieres empezando tu respuesta con su folio, ej. '#{pending[0]['folio']} SI':\n\n{lista}",
    )


def handle_owner_reply(business: dict, text: str) -> None:
    negocio = business["name"]
    owner = get_owner_number(business)
    text = text.strip()

    # Formato estricto y explícito para referenciar un folio: '#3 SI'.
    # A propósito NO se busca cualquier número suelto en el texto — una
    # respuesta como "a las 17:00" contiene un número que NO es un folio,
    # y confundirlo con uno resolvería la cita equivocada. Cero margen para
    # eso: o el folio viene marcado sin ambigüedad, o no se adivina nada.
    folio_match = re.match(r"^#(\d+)\s+(.*)$", text)
    if folio_match:
        req = get_pending_appointment_by_folio(negocio, int(folio_match.group(1)))
        if not req:
            send_whatsapp_message(
                business,
                owner,
                f"No encontré una solicitud pendiente con el folio #{folio_match.group(1)}. "
                f"Revisa el número e intenta de nuevo.",
            )
            return
        _resolve_citas_reply(business, req, folio_match.group(2).strip())
        return

    pending = list_pending_appointments(negocio)
    is_exact_reply = text.lower() in ("si", "sí", "yes", "ok", "dale", "no")

    if pending and len(pending) == 1 and is_exact_reply:
        # Único caso simple e inequívoco sin necesitar folio ni Claude.
        _resolve_citas_reply(business, pending[0], text)
        return

    if pending and len(pending) > 1 and is_exact_reply:
        # SI/NO sin folio con varias pendientes — no se adivina cuál.
        _send_pending_list_prompt(business, owner, pending)
        return

    # Aquí el texto no es un SI/NO limpio (o no hay nada pendiente) — es
    # ambiguo entre "propuesta de horario para la única pendiente" y "aviso
    # de negocio" (cierre, cambio de horario). No se asume, se le pregunta
    # a Claude qué es en realidad.
    aviso = interpret_owner_instruction(negocio, text)
    if aviso:
        save_business_notice(business["phone_number_id"], aviso)
        send_whatsapp_message(business, owner, f'Anotado — les voy a avisar a los clientes: "{aviso}"')
        log_event(negocio, "aviso_negocio_actualizado")
        return

    if not pending:
        send_whatsapp_message(
            business,
            owner,
            "No tengo ninguna solicitud de cita pendiente ahora mismo. "
            "Si quieres avisar de un cierre especial o cambio de horario, "
            "dime algo como 'mañana cerraremos' o 'el jueves cerramos a las 2pm'.",
        )
    elif len(pending) == 1:
        _resolve_citas_reply(business, pending[0], text)
    else:
        _send_pending_list_prompt(business, owner, pending)


def _verify_webhook_signature(payload_body: bytes, signature_header: str | None) -> bool:
    """Verifica la firma X-Hub-Signature-256 que Meta manda en cada POST real
    al webhook, calculada con el App Secret sobre el cuerpo crudo del
    request. Sin esto, cualquiera que descubra la URL del webhook podría
    mandar mensajes falsos (haciéndose pasar por el dueño o por un cliente)
    sin pasar por WhatsApp para nada — por eso se rechaza de plano cualquier
    request sin firma válida, en vez de solo registrar una advertencia."""
    app_secret = os.getenv("WHATSAPP_APP_SECRET")
    if not app_secret:
        log.error("WHATSAPP_APP_SECRET no configurado: no se puede verificar la firma del webhook, se rechaza por seguridad.")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), payload_body, hashlib.sha256).hexdigest()
    received = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, received)


@app.get("/webhook")
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    expected_token = os.getenv("WHATSAPP_VERIFY_TOKEN")
    if mode == "subscribe" and expected_token and token and hmac.compare_digest(token, expected_token):
        log.info("Webhook verificado por Meta.")
        return challenge, 200

    log.warning("Verificación de webhook fallida (token no coincide).")
    return "Forbidden", 403


@app.post("/webhook")
def receive_message():
    if not _verify_webhook_signature(request.get_data(), request.headers.get("X-Hub-Signature-256")):
        log.warning("Firma de webhook inválida o ausente — request rechazado (posible intento de suplantación).")
        return "Forbidden", 403

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
        phone_number_id = change_value["metadata"]["phone_number_id"]
    except (KeyError, IndexError):
        log.warning("Payload de webhook con formato inesperado: %s", payload)
        return "OK", 200

    if is_duplicate_message(message_id):
        log.info("Mensaje %s duplicado (reintento de Meta), ignorado.", message_id)
        return "OK", 200

    business = get_business_config(phone_number_id)
    if not business:
        # El número que recibió el mensaje no está dado de alta en la
        # pestaña Clientes (o está marcado inactivo) — no hay a quién
        # avisarle ni dónde guardar nada, así que solo se registra la alerta
        # y se ignora el mensaje, en vez de tronar.
        log.warning("Mensaje a phone_number_id %s sin negocio dado de alta.", phone_number_id)
        alert_daniel(None, f"Llegó un mensaje de {wa_id} a un número sin negocio dado de alta (phone_number_id={phone_number_id}).")
        return "OK", 200

    log.info("Mensaje de %s (tipo: %s) para %s", wa_id, msg_type, business["name"])

    owner = get_owner_number(business)
    if owner and wa_id == owner:
        handle_owner_reply(business, incoming.get("text", {}).get("body", ""))
        return "OK", 200

    stored_content = None  # si se define, es lo que se guarda en el historial en vez de user_content

    if msg_type == "text":
        user_content = incoming.get("text", {}).get("body", "")

    elif msg_type == "image":
        media = fetch_whatsapp_media(incoming["image"]["id"])
        if not media:
            send_whatsapp_message(business, wa_id, "No pude abrir tu foto, ¿me la puedes describir por texto?")
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
            business,
            wa_id,
            "Por ahora solo puedo leer mensajes de texto o fotos — ¿me lo escribes? 🙏",
        )
        log_event(business["name"], "mensaje_no_soportado")
        return "OK", 200

    reply, action, hallucination_detected = ask_agent(
        business, wa_id, user_content, stored_message=stored_content
    )
    send_whatsapp_message(business, wa_id, reply)
    log_event(business["name"], "mensaje_respondido")

    if hallucination_detected:
        alert_daniel(
            business,
            f"El bot casi confirma una acción falsa de cita a {wa_id} sin "
            f"usar la herramienta real — se bloqueó automáticamente, pero "
            f"revisa el prompt, esto no debería pasar."
        )
        log_event(business["name"], "alucinacion_detectada")

    if action and action["type"] == "crear":
        folio = add_pending_appointment(business["name"], wa_id, action)
        action["customer_wa_id"] = wa_id
        notify_owner_new_request(business, action, folio)
        log_event(business["name"], "cita_solicitada")

    elif action and action["type"] in ("cancelar", "modificar"):
        # Verificación de seguridad: el folio debe pertenecer a QUIEN
        # escribió, sin importar lo que haya dicho Claude — nunca se confía
        # ciegamente en que el modelo eligió el folio correcto.
        cita = get_appointment_by_folio(business["name"], action["folio"])
        if not cita or cita["customer_wa_id"] != wa_id:
            alert_daniel(
                business,
                f"{wa_id} intentó {action['type']} el folio #{action['folio']}, "
                f"que no existe o no le pertenece — bloqueado."
            )
        elif action["type"] == "cancelar":
            mark_appointment_resolved(business["name"], cita["row_number"], "cancelada_por_cliente")
            notify_owner_cancellation(business, cita)
            log_event(business["name"], "cita_cancelada")
        else:  # modificar
            update_appointment_horario(business["name"], cita["row_number"], action["nuevo_horario"])
            mark_appointment_resolved(business["name"], cita["row_number"], "pendiente")  # requiere reconfirmar
            notify_owner_modification(business, cita, action["nuevo_horario"])
            log_event(business["name"], "cita_modificada")

    return "OK", 200


@app.get("/")
def health():
    return "WhatsApp bot demo activo", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
