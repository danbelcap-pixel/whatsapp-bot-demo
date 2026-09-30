import base64
import hashlib
import hmac
import logging
import os
import re

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

from agent.client import ask_agent, interpret_owner_citas_reply, interpret_owner_instruction
from services.business import (
    get_business_config,
    get_business_config_by_page,
    get_business_config_by_telegram,
    get_business_config_by_widget,
)
from services.memory import (
    check_ip_rate_limit,
    check_widget_rate_limit,
    es_primera_vez_hoy,
    get_history,
    is_duplicate_message,
    save_business_notice,
    save_history,
)
from services.sheets import (
    add_lead,
    add_pending_appointment,
    dar_de_alta_cliente,
    desactivar_cliente,
    eliminar_datos_finales,
    get_appointment_by_folio,
    get_daily_report,
    get_pending_appointment_by_folio,
    intentar_vincular_telegram,
    list_all_appointments,
    list_leads,
    list_pending_appointments,
    log_conversacion_nueva,
    log_event,
    marcar_lead_contactado,
    mark_appointment_resolved,
    negocio_existe,
    search_citas_by_query,
    update_appointment_calendar_event_id,
    update_appointment_horario,
)
from services.calendar import create_event as create_calendar_event
from services.calendar import delete_event as delete_calendar_event

WEB_VISITOR_PREFIX = "web:"
WIDGET_MAX_MESSAGE_LENGTH = 2000
MESSENGER_PREFIX = "messenger:"

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("whatsapp-bot")

app = Flask(__name__)

GRAPH_API_VERSION = "v21.0"


def _registrar_conversacion(business_name: str, wa_id: str) -> None:
    """Suma 1 a "personas distintas hoy" de este negocio, solo la primera vez que ESTE
    cliente/visitante escribe en el día — es la métrica real que decide su plan de
    mensualidad (ver lib/planes.ts en la plataforma)."""
    if es_primera_vez_hoy(business_name, wa_id):
        log_conversacion_nueva(business_name)


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


def _has_owner_contact(business: dict) -> bool:
    """True si hay alguna forma de avisarle al dueño — por WhatsApp o por
    Telegram (negocios que solo usan el widget de página web, sin WhatsApp
    propio, avisan por Telegram)."""
    return bool(get_owner_number(business)) or bool(business.get("telegram_chat_id"))


def send_telegram_message(chat_id: str, text: str) -> bool:
    """Manda un mensaje de Telegram usando el mismo bot de alertas de
    Daniel — el dueño de un negocio solo tiene que iniciar una conversación
    con ese bot una vez para poder recibir y contestar avisos de citas por
    ahí. Devuelve True si se mandó bien."""
    bot_token = os.getenv("TELEGRAM_ALERT_BOT_TOKEN")
    if not bot_token or not chat_id:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException:
        log.exception("No se pudo mandar el mensaje de Telegram a %s", chat_id)
        return False


def _reply_to_owner(business: dict, text: str) -> None:
    """Le contesta directo al DUEÑO (confirmaciones tipo "anotado", o que no
    se encontró un folio) — por el mismo canal donde recibe los avisos de
    citas, Telegram o WhatsApp, sin importar por cuál llegó su mensaje."""
    telegram_chat_id = business.get("telegram_chat_id")
    if telegram_chat_id:
        send_telegram_message(telegram_chat_id, text)
        return
    owner = get_owner_number(business)
    if owner:
        send_whatsapp_message(business, owner, text)


def notify_staff(
    business: dict,
    owner_template: str, owner_params: list[str], owner_text: str,
    notify_template: str, notify_params: list[str],
) -> None:
    """Manda el aviso de acción al dueño (plantilla de WhatsApp, o texto
    libre por Telegram si el negocio no usa WhatsApp propio), y una
    plantilla informativa (sin instrucciones, para no confundirlos con
    acciones que no pueden tomar) a cualquier encargado adicional
    configurado — esos siempre son números de WhatsApp, no Telegram."""
    telegram_chat_id = business.get("telegram_chat_id")
    if telegram_chat_id:
        send_telegram_message(telegram_chat_id, owner_text)
    else:
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
    except (requests.RequestException, KeyError):
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

    try:
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
    except requests.RequestException as exc:
        log.exception("Fallo de red enviando mensaje a WhatsApp")
        alert_daniel(business, f"Fallo de red al enviar mensaje a {to}: {exc}")
        if business:
            log_event(business["name"], "error_envio")
        return

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

    try:
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
    except requests.RequestException as exc:
        log.exception("Fallo de red enviando plantilla '%s'", template_name)
        alert_daniel(business, f"Fallo de red al enviar plantilla '{template_name}' a {to}: {exc}")
        log_event(business["name"], "error_envio")
        return

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


EMAIL_FROM_DOMAIN = "beltranserviciosdigitales.com"


def send_confirmation_email(business: dict, to_email: str, subject: str, message_text: str) -> None:
    """Manda un correo de confirmación al cliente final — solo aplica a
    clientes que llegaron por el chat de página web y aceptaron dar su
    correo al agendar. Se manda ADEMÁS de guardarlo en el historial del
    chat (ver _notify_customer), no en su lugar: si el cliente no vuelve a
    abrir el chat, el correo es la única forma en que se entera."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key or not to_email:
        return
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": f"{business['name']} <citas@{EMAIL_FROM_DOMAIN}>",
                "to": [to_email],
                "subject": subject,
                "html": f"<p>{message_text}</p>",
            },
            timeout=15,
        )
        if resp.status_code >= 400:
            log.error("Error enviando correo a %s: %s %s", to_email, resp.status_code, resp.text)
    except requests.RequestException:
        log.exception("No se pudo mandar el correo de confirmación a %s", to_email)


def notify_owner_new_request(business: dict, req: dict, folio: int | None) -> None:
    if not _has_owner_contact(business):
        alert_daniel(business, f"Se pidió una cita pero este negocio no tiene forma de avisarle al dueño configurada: {req}")
        return

    folio_texto = str(folio) if folio else "?"
    params = [folio_texto, req["nombre"], req["servicio"], req["horario"]]
    negocio_cliente = req.get("negocio_cliente", "")
    negocio_texto = f"\nNegocio: {negocio_cliente}" if negocio_cliente else ""
    owner_text = (
        f"📅 Nueva solicitud de cita, folio {folio_texto}.\n"
        f"Nombre: {req['nombre']}{negocio_texto}\nServicio: {req['servicio']}\nHorario pedido: {req['horario']}\n\n"
        f"Responde SI para confirmar, NO para rechazar, o propone otro horario."
    )
    notify_staff(business, "cita_nueva_solicitud", params, owner_text, "cita_nueva_informativa", params)


def notify_owner_cancellation(business: dict, cita: dict) -> None:
    if not _has_owner_contact(business):
        return
    params = [str(cita["folio"]), cita["nombre"], cita["servicio"], cita["horario"]]
    owner_text = (
        f"❌ Folio {cita['folio']} cancelada por el cliente.\n"
        f"Nombre: {cita['nombre']}\nServicio: {cita['servicio']}\nHorario: {cita['horario']}.\n\n"
        f"Esto es solo informativo."
    )
    notify_staff(business, "cita_cancelada", params, owner_text, "cita_cancelada", params)


def notify_owner_modification(business: dict, cita: dict, nuevo_horario: str) -> None:
    if not _has_owner_contact(business):
        alert_daniel(business, f"Se pidió mover el folio #{cita['folio']} pero este negocio no tiene forma de avisarle al dueño configurada.")
        return

    params = [str(cita["folio"]), cita["nombre"], cita["servicio"], cita["horario"], nuevo_horario]
    owner_text = (
        f"🔄 Folio {cita['folio']}: cambio de horario solicitado.\n"
        f"Nombre: {cita['nombre']}\nServicio: {cita['servicio']}\n"
        f"Horario anterior: {cita['horario']}\nHorario nuevo pedido: {nuevo_horario}\n\n"
        f"Responde SI para confirmar, NO para rechazar, o propone otro horario."
    )
    notify_staff(business, "cita_cambio_solicitado", params, owner_text, "cita_cambio_informativa", params)


def _notify_customer(
    business: dict, customer_id: str, message_text: str,
    template_name: str, template_params: list[str],
    correo: str = "",
) -> None:
    """Manda el resultado de una cita al CLIENTE final, sin importar por
    qué canal llegó. Por WhatsApp usa la plantilla pre-aprobada (funciona
    aunque hayan pasado más de 24h). Un visitante del chat de la página web
    no tiene forma de recibir un mensaje "empujado" a un navegador que ya
    cerró — en vez de eso, se guarda en su historial de conversación, y lo
    ve tal cual la próxima vez que abra el chat, como si el bot se lo
    hubiera dicho en ese momento. Si además dejó un correo al agendar, se le
    manda también por ahí — no en lugar del historial, sino además, por si
    no vuelve a abrir el chat."""
    if customer_id.startswith(WEB_VISITOR_PREFIX):
        history = get_history(business["business_id"], customer_id)
        history.append({"role": "assistant", "content": [{"type": "text", "text": message_text}]})
        save_history(business["business_id"], customer_id, history)
        if correo:
            send_confirmation_email(business, correo, f"Actualización de tu cita — {business['name']}", message_text)
    else:
        send_whatsapp_template(business, customer_id, template_name, template_params)


def _add_to_calendar_if_configured(business: dict, req: dict, pendiente: bool = False) -> None:
    """Si el negocio compartió su Google Calendar (columna configurada en
    Sheets), agrega la cita como evento — nunca bloquea ni falla la acción
    en sí si algo sale mal aquí.

    "pendiente=True" se usa desde que el cliente PIDE la cita (todavía sin
    aprobar) — así el dueño ve la solicitud con solo mirar su calendario,
    sin depender de que no se le pase ningún aviso de WhatsApp/Telegram.
    Cuando el dueño confirma, se borra ese evento pendiente y se crea uno
    nuevo sin la marca (ver _resolve_citas_reply)."""
    calendar_id = business.get("google_calendar_id")
    if not calendar_id:
        return
    negocio_texto = f"\nNegocio del cliente: {req['negocio_cliente']}" if req.get("negocio_cliente") else ""
    event_id = create_calendar_event(
        calendar_id,
        resumen=f"{req['servicio']} — {req['nombre']}",
        descripcion=f"Folio #{req['folio']}\nCliente: {req['nombre']}{negocio_texto}",
        horario_texto=req["horario"],
        zona=business.get("zona_horaria", "America/Mexico_City"),
        pendiente=pendiente,
    )
    if event_id:
        update_appointment_calendar_event_id(business["name"], req["row_number"], event_id)


def _remove_from_calendar_if_any(business: dict, cita: dict) -> None:
    """Borra el evento de Calendar de una cita que ya estaba confirmada, si
    es que llegó a crearse uno — al cancelarla o moverla de horario."""
    calendar_id = business.get("google_calendar_id")
    event_id = cita.get("google_event_id")
    if calendar_id and event_id:
        delete_calendar_event(calendar_id, event_id)


def _resolve_citas_reply(business: dict, req: dict, reply_text: str) -> None:
    """Aplica la respuesta del dueño (SI/NO/horario alternativo) a una
    solicitud de cita específica ya identificada sin ambigüedad."""
    negocio = business["name"]
    reply_normalized = reply_text.strip().lower()

    # Estos avisos van al CLIENTE, y el bot los inicia por su cuenta — el
    # dueño pudo haber tardado horas en contestar, así que no se puede
    # asumir que la ventana de 24h del cliente siga abierta. Por eso usan
    # plantilla, no texto libre (o el equivalente guardado, si es del widget).
    if reply_normalized in ("si", "sí", "yes", "ok", "dale"):
        _notify_customer(
            business, req["customer_wa_id"],
            f"¡Confirmado! Tu cita para {req['servicio']} quedó agendada el {req['horario']}. Te esperamos.",
            "cita_confirmada_cliente", [req["servicio"], req["horario"]],
            correo=req.get("correo", ""),
        )
        mark_appointment_resolved(negocio, req["row_number"], "confirmada")
        log_event(negocio, "cita_confirmada")
        # Se borra el evento "⏳ Pendiente" (si se creó desde que se pidió
        # la cita) y se crea uno nuevo sin la marca — así no quedan dos
        # eventos del mismo folio en el calendario.
        _remove_from_calendar_if_any(business, req)
        _add_to_calendar_if_configured(business, req, pendiente=False)
    elif reply_normalized == "no":
        _notify_customer(
            business, req["customer_wa_id"],
            "Ese horario no está disponible. ¿Tienes otro horario que te funcione? Escríbenos de nuevo para revisar otra opción.",
            "cita_rechazada_cliente", [],
            correo=req.get("correo", ""),
        )
        mark_appointment_resolved(negocio, req["row_number"], "rechazada")
        log_event(negocio, "cita_rechazada")
        _remove_from_calendar_if_any(business, req)
    else:
        # El dueño escribió un horario alternativo en texto libre
        _notify_customer(
            business, req["customer_wa_id"],
            f"Tu horario solicitado no estaba disponible, pero te proponemos: {reply_text}. ¿Te funciona?",
            "cita_horario_alternativo_cliente", [reply_text],
            correo=req.get("correo", ""),
        )
        mark_appointment_resolved(negocio, req["row_number"], f"horario_alternativo: {reply_text[:100]}")


def _send_pending_list_prompt(business: dict, pending: list[dict]) -> None:
    lista = "\n".join(
        f"#{p['folio']} — {p['nombre']} ({p['servicio']}, {p['horario']})" for p in pending
    )
    _reply_to_owner(
        business,
        f"Tienes {len(pending)} solicitudes de cita pendientes — dime a cuál te "
        f"refieres empezando tu respuesta con su folio, ej. '#{pending[0]['folio']} SI':\n\n{lista}",
    )


def _wa_id_legible(wa_id: str) -> str:
    """Versión corta y segura de mostrar un wa_id en un mensaje al dueño —
    nunca el número completo en la lista de candidatos (para desambiguar
    alcanza con los últimos dígitos), y una etiqueta clara si es un
    visitante del chat de la página web en vez de un número real."""
    if wa_id.startswith(WEB_VISITOR_PREFIX):
        return "chat de la página web"
    return f"WhatsApp ...{wa_id[-4:]}" if len(wa_id) >= 4 else f"WhatsApp {wa_id}"


def _send_historial_conversacion(business: dict, cliente: dict) -> None:
    history = get_history(business["business_id"], cliente["customer_wa_id"])
    mensajes = _extract_visible_messages(history)
    if not mensajes:
        _reply_to_owner(
            business,
            f"No tengo conversación guardada de {cliente['nombre']} — el "
            f"historial se guarda solo por 3 días, así que si fue hace más "
            f"tiempo ya no está disponible.",
        )
        return

    lineas = [f"Historial con {cliente['nombre']} ({_wa_id_legible(cliente['customer_wa_id'])}):\n"]
    for m in mensajes:
        quien = "Cliente" if m["role"] == "user" else "Bot"
        lineas.append(f"{quien}: {m['text']}")
    transcripcion = "\n\n".join(lineas)

    # Telegram/WhatsApp tienen límite de longitud por mensaje — mejor un
    # historial recortado a lo más reciente que un envío que falle entero.
    if len(transcripcion) > 3500:
        transcripcion = (
            "(...recortado, se muestra solo lo más reciente...)\n\n"
            + transcripcion[-3500:]
        )
    _reply_to_owner(business, transcripcion)


def _handle_historial_request(business: dict, query: str) -> None:
    negocio = business["name"]

    # "historial #12" — folio exacto, típicamente al desempatar una lista
    # de homónimos. Siempre resuelve a un cliente único, sin ambigüedad.
    folio_match = re.match(r"^#?(\d+)$", query.strip())
    if folio_match:
        cita = get_appointment_by_folio(negocio, int(folio_match.group(1)))
        if not cita:
            _reply_to_owner(
                business,
                f"No encontré ninguna cita con el folio #{folio_match.group(1)}.",
            )
            return
        _send_historial_conversacion(business, cita)
        return

    candidatos = search_citas_by_query(negocio, query)

    if not candidatos:
        _reply_to_owner(
            business,
            f'No encontré ningún cliente que coincida con "{query}" en los '
            f"registros de citas. Prueba con el nombre completo o el "
            f"número de WhatsApp.\n\n(Ojo: solo encuentro clientes que ya "
            f"pidieron una cita alguna vez — si solo platicó con el bot sin "
            f"agendar nada, no lo puedo buscar por nombre.)",
        )
        return

    if len(candidatos) > 1:
        # Varios clientes distintos coinciden (posibles homónimos) — nunca
        # se adivina cuál. Se desempata por folio (siempre existe y es
        # único, a diferencia del número de WhatsApp: un cliente del chat
        # de la página web no tiene uno real que pueda dar).
        lista = "\n".join(
            f"- #{c['folio']} {c['nombre']} ({_wa_id_legible(c['customer_wa_id'])}) — "
            f"última cita: {c['servicio']}, {c['horario']}"
            for c in candidatos
        )
        _reply_to_owner(
            business,
            f'Encontré {len(candidatos)} clientes que coinciden con "{query}" '
            f"— contesta \"historial #<folio>\" del que te interese "
            f"(ej. \"historial #{candidatos[0]['folio']}\"):\n\n{lista}",
        )
        return

    _send_historial_conversacion(business, candidatos[0])


def handle_owner_reply(business: dict, text: str) -> None:
    negocio = business["name"]
    text = text.strip()

    # Comando para que el dueño pida ver una conversación pasada — antes de
    # cualquier otra interpretación, porque "historial ..." no se parece a
    # un folio ni a un SI/NO.
    historial_match = re.match(r"^historial[:\s]+(.+)$", text, re.IGNORECASE)
    if historial_match:
        _handle_historial_request(business, historial_match.group(1).strip())
        return

    # Formato estricto y explícito para referenciar un folio: '#3 SI'.
    # A propósito NO se busca cualquier número suelto en el texto — una
    # respuesta como "a las 17:00" contiene un número que NO es un folio,
    # y confundirlo con uno resolvería la cita equivocada. Cero margen para
    # eso: o el folio viene marcado sin ambigüedad, o no se adivina nada.
    folio_match = re.match(r"^#(\d+)\s+(.*)$", text)
    if folio_match:
        req = get_pending_appointment_by_folio(negocio, int(folio_match.group(1)))
        if not req:
            _reply_to_owner(
                business,
                f"No encontré una solicitud pendiente con el folio #{folio_match.group(1)}. "
                f"Revisa el número e intenta de nuevo.",
            )
            return
        _resolve_citas_reply(business, req, folio_match.group(2).strip())
        return

    pending = list_pending_appointments(negocio)
    is_exact_reply = text.lower() in ("si", "sí", "yes", "ok", "dale", "no")
    zona = business.get("zona_horaria", "America/Mexico_City")

    if pending and len(pending) == 1 and is_exact_reply:
        # Único caso simple e inequívoco sin necesitar folio ni Claude.
        _resolve_citas_reply(business, pending[0], text)
        return

    if pending and len(pending) > 1:
        if is_exact_reply:
            # Un "si"/"no" puro con varias pendientes no dice a cuál se
            # refiere — no hay nada que Claude pueda relacionar tampoco.
            _send_pending_list_prompt(business, pending)
            return
        # Respuesta en lenguaje natural con varias solicitudes pendientes
        # (ej. "si confirmo la cita de las 6 para Daniel", "cita 6 si").
        # Además del formato explícito '#N SI', se le da a Claude la lista
        # REAL de pendientes para que identifique sin ambigüedad a cuál se
        # refiere — si no está seguro, no adivina y se cae al mismo aviso
        # de siempre pidiendo el folio.
        match = interpret_owner_citas_reply(negocio, text, pending, zona)
        if match:
            req = get_pending_appointment_by_folio(negocio, match["folio"])
            if req:
                reply_for_resolve = {
                    "confirmar": "si",
                    "rechazar": "no",
                }.get(match["decision"], match.get("nuevo_horario") or text)
                _resolve_citas_reply(business, req, reply_for_resolve)
                return
        _send_pending_list_prompt(business, pending)
        return

    # Aquí no hay más de una pendiente (0 o 1) y el texto no es un SI/NO
    # limpio — es ambiguo entre "propuesta de horario para la única
    # pendiente" y "aviso de negocio" (cierre, cambio de horario). No se
    # asume, se le pregunta a Claude qué es en realidad.
    aviso = interpret_owner_instruction(negocio, text, zona)
    if aviso:
        save_business_notice(business["business_id"], aviso)
        _reply_to_owner(business, f'Anotado — les voy a avisar a los clientes: "{aviso}"')
        log_event(negocio, "aviso_negocio_actualizado")
        return

    if not pending:
        _reply_to_owner(
            business,
            "No tengo ninguna solicitud de cita pendiente ahora mismo. "
            "Si quieres avisar de un cierre especial o cambio de horario, "
            "dime algo como 'mañana cerraremos' o 'el jueves cerramos a las 2pm'.",
        )
    else:
        _resolve_citas_reply(business, pending[0], text)


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

    try:
        reply, action, hallucination_detected = ask_agent(
            business, wa_id, user_content, stored_message=stored_content
        )
    except Exception as exc:
        log.exception("Fallo llamando a Claude para %s", wa_id)
        alert_daniel(business, f"Falló la llamada a Claude respondiéndole a {wa_id}: {exc}")
        send_whatsapp_message(
            business, wa_id,
            "Ando teniendo un problema técnico ahora mismo — dame un momento e intenta de nuevo, por favor 🙏",
        )
        log_event(business["name"], "error_envio")
        return "OK", 200

    send_whatsapp_message(business, wa_id, reply)
    log_event(business["name"], "mensaje_respondido")
    _registrar_conversacion(business["name"], wa_id)

    if hallucination_detected:
        alert_daniel(
            business,
            f"El bot casi confirma una acción falsa de cita a {wa_id} sin "
            f"usar la herramienta real — se bloqueó automáticamente, pero "
            f"revisa el prompt, esto no debería pasar."
        )
        log_event(business["name"], "alucinacion_detectada")

    if action:
        _handle_agent_action(business, wa_id, action)

    return "OK", 200


def send_messenger_message(page_id: str, recipient_psid: str, body: str) -> None:
    """Manda un mensaje de texto por Messenger, a nombre de la página que recibió el mensaje
    original. Usa un solo token de acceso de página por ahora (MESSENGER_PAGE_TOKEN) — sirve
    mientras solo haya una página conectada; si más adelante cada negocio conecta la suya
    propia, esto necesitará guardar un token por negocio, igual que el resto del sistema
    multi-negocio ya hace por Sheets."""
    token = os.getenv("MESSENGER_PAGE_TOKEN")
    if not token:
        log.error("MESSENGER_PAGE_TOKEN no configurado: no se pudo contestar por Messenger.")
        return
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
    try:
        resp = requests.post(
            url,
            params={"access_token": token},
            json={
                "messaging_product": "messenger",  # ignorado por Graph, pero documenta la intención
                "recipient": {"id": recipient_psid},
                "message": {"text": body},
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        log.exception("Fallo de red enviando mensaje a Messenger")
        alert_daniel(None, f"Fallo de red al enviar mensaje de Messenger a {recipient_psid} (página {page_id}): {exc}")
        return
    if resp.status_code >= 400:
        log.error("Error enviando mensaje a Messenger: %s %s", resp.status_code, resp.text)
        alert_daniel(None, f"Fallo al enviar mensaje de Messenger a {recipient_psid}: HTTP {resp.status_code} — {resp.text[:200]}")


@app.get("/messenger-webhook")
def verify_messenger_webhook():
    """Mismo protocolo de verificación que Meta exige para cualquier webhook (WhatsApp,
    Messenger, etc.) — token propio para no mezclarlo con el de WhatsApp."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    expected_token = os.getenv("MESSENGER_VERIFY_TOKEN")
    if mode == "subscribe" and expected_token and token and hmac.compare_digest(token, expected_token):
        log.info("Webhook de Messenger verificado por Meta.")
        return challenge, 200
    log.warning("Verificación del webhook de Messenger fallida (token no coincide).")
    return "Forbidden", 403


@app.post("/messenger-webhook")
def receive_messenger_message():
    """Recibe mensajes de Messenger de cualquier página conectada. Misma firma
    X-Hub-Signature-256 y mismo App Secret que el webhook de WhatsApp — es la misma app de Meta,
    solo un producto distinto."""
    if not _verify_webhook_signature(request.get_data(), request.headers.get("X-Hub-Signature-256")):
        log.warning("Firma de webhook de Messenger inválida o ausente — request rechazado.")
        return "Forbidden", 403

    payload = request.get_json(silent=True) or {}
    if payload.get("object") != "page":
        return "OK", 200

    for entry in payload.get("entry", []):
        page_id = str(entry.get("id", ""))
        for evento in entry.get("messaging", []):
            texto = (evento.get("message") or {}).get("text")
            psid = (evento.get("sender") or {}).get("id")
            # Sin texto (ej. un "like" al último mensaje, o un mensaje de solo imagen/sticker) o
            # sin remitente: no hay nada que contestar, se ignora sin tronar.
            if not texto or not psid:
                continue

            business = get_business_config_by_page(page_id)
            if not business:
                log.warning("Mensaje de Messenger a la página %s sin negocio dado de alta.", page_id)
                alert_daniel(None, f"Llegó un mensaje de Messenger a la página {page_id}, sin negocio dado de alta con ese Page ID en Clientes.")
                continue

            wa_id = f"{MESSENGER_PREFIX}{psid}"
            try:
                reply, action, hallucination_detected = ask_agent(business, wa_id, texto)
            except Exception as exc:
                log.exception("Fallo llamando a Claude para Messenger (%s)", business["name"])
                alert_daniel(business, f"Falló la llamada a Claude respondiéndole por Messenger a {psid}: {exc}")
                send_messenger_message(page_id, psid, "Ando teniendo un problema técnico ahora mismo — dame un momento e intenta de nuevo, por favor 🙏")
                continue

            send_messenger_message(page_id, psid, reply)
            log_event(business["name"], "mensaje_respondido")
            _registrar_conversacion(business["name"], wa_id)

            if hallucination_detected:
                alert_daniel(
                    business,
                    f"El bot (Messenger) casi confirma una acción falsa de cita a {psid} sin "
                    f"usar la herramienta real — se bloqueó automáticamente, pero revisa el "
                    f"prompt, esto no debería pasar."
                )
                log_event(business["name"], "alucinacion_detectada")

            if action:
                _handle_agent_action(business, wa_id, action)

    return "OK", 200


def _handle_agent_action(business: dict, wa_id: str, action: dict) -> None:
    """Aplica la acción que Claude decidió tomar (crear/cancelar/modificar
    una cita) — compartido entre el canal de WhatsApp y el del chat de
    página web, la lógica (y sobre todo la validación de seguridad) es
    exactamente la misma sin importar de dónde vino el mensaje."""
    if action["type"] == "crear":
        folio = add_pending_appointment(business["name"], wa_id, action)
        action["customer_wa_id"] = wa_id
        notify_owner_new_request(business, action, folio)
        log_event(business["name"], "cita_solicitada")
        if folio:
            # add_pending_appointment siempre agrega la fila justo después
            # de las ya existentes, así que su número de fila es el folio
            # + 1 (la fila 2 es el folio 1, la fila 3 el folio 2, etc.).
            action["folio"] = folio
            action["row_number"] = folio + 1
            _add_to_calendar_if_configured(business, action, pendiente=True)

    elif action["type"] in ("cancelar", "modificar"):
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
            _remove_from_calendar_if_any(business, cita)
            notify_owner_cancellation(business, cita)
            log_event(business["name"], "cita_cancelada")
        else:  # modificar
            update_appointment_horario(business["name"], cita["row_number"], action["nuevo_horario"])
            mark_appointment_resolved(business["name"], cita["row_number"], "pendiente")  # requiere reconfirmar
            # Ojo: notify_owner_modification necesita el horario ANTERIOR
            # (todavía en cita["horario"]) — se manda antes de tocar ese
            # campo, para no pisarlo.
            notify_owner_modification(business, cita, action["nuevo_horario"])
            # Se borra el evento viejo (con el horario anterior) y se crea
            # uno nuevo "⏳ Pendiente" con el horario nuevo — así el
            # calendario nunca se queda sin reflejar la solicitud mientras
            # se espera la reconfirmación del dueño.
            _remove_from_calendar_if_any(business, cita)
            cita["horario"] = action["nuevo_horario"]
            _add_to_calendar_if_configured(business, cita, pendiente=True)
            log_event(business["name"], "cita_modificada")

    elif action["type"] == "lead":
        _notify_new_lead(business, action)
        add_lead(business["name"], action["nombre"], action["contacto"], action.get("negocio_cliente", ""), action["detalle"])
        log_event(business["name"], "lead_registrado")


def _notify_new_lead(business: dict, lead: dict) -> None:
    """Avisa de inmediato al dueño de alguien interesado en contratar — sin
    aprobación de por medio, a diferencia de una cita: aquí no hay nada que
    "resolver", solo se le avisa para que le hable lo antes posible."""
    if not _has_owner_contact(business):
        alert_daniel(business, f"Nuevo interesado pero este negocio no tiene forma de avisarle al dueño configurada: {lead}")
        return

    negocio_texto = f"\nNegocio: {lead['negocio_cliente']}" if lead.get("negocio_cliente") else ""
    text = (
        f"🟢 Nuevo interesado en contratar.\n"
        f"Nombre: {lead['nombre']}{negocio_texto}\n"
        f"Contacto: {lead['contacto']}\n"
        f"Detalle: {lead['detalle']}\n\n"
        f"Contáctalo lo antes posible."
    )
    telegram_chat_id = business.get("telegram_chat_id")
    if telegram_chat_id:
        send_telegram_message(telegram_chat_id, text)
    else:
        owner = get_owner_number(business)
        if owner:
            send_whatsapp_message(business, owner, text)


def _client_ip() -> str:
    """IP real del visitante detrás del proxy de Render — X-Forwarded-For trae una lista
    'cliente, proxy1, proxy2'; el primero es el visitante real."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "")


def _widget_cors(response):
    """El widget se embebe en dominios de terceros (la página del cliente),
    así que el navegador exige CORS para que el fetch() no se bloquee."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/widget/message", methods=["POST", "OPTIONS"])
def widget_message():
    if request.method == "OPTIONS":
        return _widget_cors(app.make_default_options_response())

    payload = request.get_json(silent=True) or {}
    widget_id = str(payload.get("widget_id", "")).strip()
    visitor_id = str(payload.get("visitor_id", "")).strip()
    message = str(payload.get("message", "")).strip()

    if not widget_id or not visitor_id or not message:
        return _widget_cors(jsonify({"error": "Faltan datos (widget_id, visitor_id o message)."})), 400
    if len(message) > WIDGET_MAX_MESSAGE_LENGTH:
        return _widget_cors(jsonify({"error": "Mensaje demasiado largo."})), 400

    business = get_business_config_by_widget(widget_id)
    if not business:
        return _widget_cors(jsonify({"error": "Widget no reconocido."})), 404

    wa_id = f"{WEB_VISITOR_PREFIX}{visitor_id}"

    # Dos límites independientes: por visitor_id (lo manda el navegador, cualquiera lo puede
    # cambiar) y por IP real (más difícil de rotar) — así uno cubre lo que el otro no.
    if not check_widget_rate_limit(visitor_id) or not check_ip_rate_limit(_client_ip()):
        return _widget_cors(jsonify({
            "reply": "Has mandado muchos mensajes en poco tiempo — intenta de nuevo en un rato, por favor.",
        }))

    try:
        reply, action, hallucination_detected = ask_agent(business, wa_id, message)
    except Exception as exc:
        log.exception("Fallo llamando a Claude para el widget de %s", business["name"])
        alert_daniel(business, f"Falló la llamada a Claude respondiéndole a un visitante del widget: {exc}")
        return _widget_cors(jsonify({
            "reply": "Ando teniendo un problema técnico ahora mismo — dame un momento e intenta de nuevo, por favor 🙏",
        }))

    log_event(business["name"], "mensaje_respondido")
    _registrar_conversacion(business["name"], wa_id)

    if hallucination_detected:
        alert_daniel(
            business,
            f"El bot (widget web) casi confirma una acción falsa de cita a "
            f"{wa_id} sin usar la herramienta real — se bloqueó "
            f"automáticamente, pero revisa el prompt, esto no debería pasar."
        )
        log_event(business["name"], "alucinacion_detectada")

    if action:
        _handle_agent_action(business, wa_id, action)

    return _widget_cors(jsonify({"reply": reply}))


@app.route("/widget/history", methods=["GET", "OPTIONS"])
def widget_history():
    if request.method == "OPTIONS":
        return _widget_cors(app.make_default_options_response())

    widget_id = request.args.get("widget_id", "").strip()
    visitor_id = request.args.get("visitor_id", "").strip()
    if not widget_id or not visitor_id:
        return _widget_cors(jsonify({"error": "Faltan datos."})), 400

    business = get_business_config_by_widget(widget_id)
    if not business:
        return _widget_cors(jsonify({"error": "Widget no reconocido."})), 404

    if not check_ip_rate_limit(_client_ip()):
        return _widget_cors(jsonify({"error": "Demasiadas solicitudes — intenta de nuevo en un rato."})), 429

    wa_id = f"{WEB_VISITOR_PREFIX}{visitor_id}"
    history = get_history(business["business_id"], wa_id)
    mensajes = _extract_visible_messages(history)

    return _widget_cors(jsonify({"messages": mensajes}))


@app.get("/api/reporte")
def reporte_mensual():
    """Datos diarios de un negocio para la plataforma web (reportes mensuales
    del cliente). Solo lectura y solo para la plataforma: exige la clave
    compartida REPORT_SECRET en el encabezado X-Report-Secret."""
    secreto = os.getenv("REPORT_SECRET", "")
    enviado = request.headers.get("X-Report-Secret", "")
    if not secreto:
        return jsonify({"error": "No configurado."}), 503
    if not hmac.compare_digest(secreto.encode(), enviado.encode()):
        return jsonify({"error": "No autorizado."}), 403

    dias = get_daily_report(request.args.get("negocio", ""), request.args.get("mes", ""))
    if dias is None:
        return jsonify({"error": "Negocio o mes no válido."}), 404
    return jsonify({"dias": dias})


def _chequear_report_secret() -> bool:
    secreto = os.getenv("REPORT_SECRET", "")
    enviado = request.headers.get("X-Report-Secret", "")
    return bool(secreto) and hmac.compare_digest(secreto.encode(), enviado.encode())


@app.get("/api/contactos")
def contactos():
    """Interesados y citas de un negocio, para su pestaña 'Contactos' en la plataforma web.
    Misma clave compartida que /api/reporte (X-Report-Secret)."""
    if not _chequear_report_secret():
        return jsonify({"error": "No autorizado."}), 403
    negocio = request.args.get("negocio", "")
    if not negocio_existe(negocio):
        return jsonify({"error": "Negocio no válido."}), 404
    return jsonify({"leads": list_leads(negocio), "citas": list_all_appointments(negocio)})


@app.post("/api/contactos/marcar-lead")
def marcar_lead():
    """El negocio marcó un interesado como ya contactado, desde la plataforma web."""
    if not _chequear_report_secret():
        return jsonify({"error": "No autorizado."}), 403
    datos = request.get_json(silent=True) or {}
    negocio = str(datos.get("negocio", ""))
    if not negocio_existe(negocio):
        return jsonify({"error": "Negocio no válido."}), 404
    try:
        folio = int(datos.get("folio"))
    except (TypeError, ValueError):
        return jsonify({"error": "Folio inválido."}), 400
    encontrado = marcar_lead_contactado(negocio, folio)
    return jsonify({"ok": True, "encontrado": encontrado})


@app.post("/api/aprovisionar")
def aprovisionar():
    """Da de alta automáticamente, en la pestaña 'Clientes', un negocio del canal web/app que acaba de pagar.
    Lo llama la plataforma (app.beltranserviciosdigitales.com) justo después del pago. Exige la clave compartida
    PROVISION_SECRET en el encabezado X-Provision-Secret. Nunca toca nada de WhatsApp."""
    secreto = os.getenv("PROVISION_SECRET", "")
    enviado = request.headers.get("X-Provision-Secret", "")
    if not secreto:
        return jsonify({"error": "No configurado."}), 503
    if not hmac.compare_digest(secreto.encode(), enviado.encode()):
        return jsonify({"error": "No autorizado."}), 403

    datos = request.get_json(silent=True) or {}
    business_name = str(datos.get("business_name", "")).strip()
    info = str(datos.get("info", "")).strip()
    if not business_name or not info:
        return jsonify({"error": "Faltan business_name o info."}), 400

    try:
        resultado = dar_de_alta_cliente(
            business_name=business_name,
            owner_phone=str(datos.get("owner_phone", "")),
            info=info,
            tono=str(datos.get("tono", "")),
            objetivo=str(datos.get("objetivo", "")),
            agenda_citas=bool(datos.get("agenda_citas", True)),
            telegram_username=str(datos.get("telegram_username", "")),
            zona_horaria=str(datos.get("zona_horaria", "")),
            calendar_id=str(datos.get("calendar_id", "")),
        )
    except Exception as exc:
        log.exception("Falló el alta automática de un cliente")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, **resultado})


@app.post("/api/desactivar")
def desactivar():
    """Apaga un negocio en la pestaña 'Clientes' (columna Activo = NO) cuando cancela su suscripción. Lo llama
    la plataforma; misma clave compartida PROVISION_SECRET que /api/aprovisionar."""
    secreto = os.getenv("PROVISION_SECRET", "")
    enviado = request.headers.get("X-Provision-Secret", "")
    if not secreto:
        return jsonify({"error": "No configurado."}), 503
    if not hmac.compare_digest(secreto.encode(), enviado.encode()):
        return jsonify({"error": "No autorizado."}), 403

    bot_name = str((request.get_json(silent=True) or {}).get("bot_name", "")).strip()
    if not bot_name:
        return jsonify({"error": "Falta bot_name."}), 400
    try:
        encontrado = desactivar_cliente(bot_name)
    except Exception as exc:
        log.exception("Falló la desactivación automática de un cliente")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "encontrado": encontrado})


@app.post("/api/eliminar-datos-finales")
def eliminar_datos_finales_endpoint():
    """Borra las pestañas de Citas e Interesados de un negocio (datos personales de SUS clientes
    finales) — 90 días después de que canceló, para cumplir el aviso de privacidad de la
    plataforma. Misma clave compartida PROVISION_SECRET que /api/aprovisionar y /api/desactivar."""
    secreto = os.getenv("PROVISION_SECRET", "")
    enviado = request.headers.get("X-Provision-Secret", "")
    if not secreto:
        return jsonify({"error": "No configurado."}), 503
    if not hmac.compare_digest(secreto.encode(), enviado.encode()):
        return jsonify({"error": "No autorizado."}), 403

    bot_name = str((request.get_json(silent=True) or {}).get("bot_name", "")).strip()
    if not bot_name:
        return jsonify({"error": "Falta bot_name."}), 400
    ok_borrado = eliminar_datos_finales(bot_name)
    if not ok_borrado:
        return jsonify({"error": "No se pudo completar el borrado."}), 500
    return jsonify({"ok": True})


def _extract_visible_messages(history: list[dict]) -> list[dict]:
    """El historial guardado incluye bloques internos (tool_use/tool_result)
    que no le sirven a un humano leyendo la conversación — solo el texto
    visible. Usado tanto por el widget de la página web como por el
    historial que se le manda al dueño por Telegram/WhatsApp."""
    mensajes = []
    for turno in history:
        content = turno.get("content")
        if isinstance(content, str):
            texto = content
        elif isinstance(content, list):
            texto = "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            texto = ""
        if texto.strip():
            mensajes.append({"role": turno.get("role"), "text": texto})
    return mensajes


@app.post("/telegram-webhook")
def telegram_webhook():
    """Recibe las respuestas de dueños de negocios que usan Telegram en vez
    de WhatsApp para aprobar/rechazar citas (típicamente negocios que solo
    usan el widget de página web, sin número de WhatsApp propio)."""
    expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected_secret or not hmac.compare_digest(received_secret, expected_secret):
        log.warning("Firma de webhook de Telegram inválida o ausente — request rechazado.")
        return "Forbidden", 403

    payload = request.get_json(silent=True) or {}
    message = payload.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if not chat_id or not text:
        # Otros tipos de update (stickers, ediciones, etc.) — no hay nada
        # que procesar, pero se contesta OK para que Telegram no reintente.
        return "OK", 200

    business = get_business_config_by_telegram(str(chat_id))
    if not business:
        # Puede ser un dueño nuevo escribiéndole al bot por primera vez, para conectar su aviso — se
        # intenta vincular solo por su @usuario de Telegram, tal como lo puso en su cuestionario.
        username = (message.get("from") or {}).get("username", "")
        negocio = intentar_vincular_telegram(username, str(chat_id)) if username else None
        if negocio:
            send_telegram_message(str(chat_id), f"¡Listo! Quedaste conectado para recibir los avisos de {negocio}. En cuanto tu agente esté activo, aquí te llegará cada solicitud de cita.")
            log.info("Telegram vinculado automáticamente: %s -> %s", username, negocio)
        else:
            log.warning("Mensaje de Telegram de chat_id %s (@%s) sin negocio dado de alta.", chat_id, username)
        return "OK", 200

    handle_owner_reply(business, text)
    return "OK", 200


@app.get("/")
def health():
    return "WhatsApp bot demo activo", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
