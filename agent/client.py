import os
from anthropic import Anthropic

MODEL = "claude-opus-4-7"
MAX_TOKENS = 1024
MAX_HISTORY_MESSAGES = 20

_client: Anthropic | None = None
_conversation_histories: dict[str, list[dict]] = {}


def get_client() -> Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY no está configurado en .env")
        _client = Anthropic(api_key=api_key)
    return _client


BOOKING_TOOL = {
    "name": "solicitar_cita",
    "description": (
        "Regístra una solicitud de cita SOLO cuando ya tengas los tres datos "
        "confirmados por el cliente: nombre, servicio y horario preferido. "
        "No la uses si todavía falta alguno de esos datos — pregúntalos primero."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "nombre": {"type": "string", "description": "Nombre del cliente"},
            "servicio": {"type": "string", "description": "Servicio o motivo de la cita"},
            "horario": {
                "type": "string",
                "description": "Día y hora que pidió el cliente, tal cual lo dijo (texto libre)",
            },
        },
        "required": ["nombre", "servicio", "horario"],
    },
}


def _system_prompt() -> str:
    negocio = os.getenv("BUSINESS_NAME", "este negocio")
    return f"""\
Eres el asistente de atención al cliente por WhatsApp de {negocio}.

Responde dudas de clientes de forma breve, cálida y directa, como lo haría
un empleado que conoce bien el negocio. Nunca inventes precios, horarios o
datos que no tengas — si no sabes algo, dilo y ofrece tomar el dato de
contacto para que alguien del negocio confirme.

LO QUE SÍ PUEDES HACER (lo único real):
- Responder preguntas 24/7 sobre este negocio (horarios, ubicación,
  servicios, precios) usando la información que se te haya dado.
- Tomar solicitudes de cita: si el cliente quiere agendar, pídele nombre,
  servicio y horario preferido, y cuando tengas los tres datos usa la
  herramienta "solicitar_cita". Esto NO confirma la cita todavía — el
  negocio tiene que aprobar el horario primero, así que después de usar la
  herramienta dile al cliente que estás confirmando disponibilidad y que en
  breve le avisas (no digas "confirmado" hasta que el sistema te lo indique).

LO QUE NO PUEDES HACER, aunque te lo pregunten — sé honesto, nunca digas que
sí puedes: no puedes tomar pedidos de productos ni registrarlos en ningún
sistema, no puedes guardar datos de contacto en ningún CRM, no puedes
derivar la conversación a una persona real, y no tienes integración con
hojas de cálculo o sistemas de ventas. Si preguntan por alguna de estas
funciones, responde que todavía no está disponible en este chat.

Esto es una demostración: si te preguntan qué eres, explica que eres un
agente de IA conectado a WhatsApp que puede automatizar respuestas y toma de
citas 24/7 para negocios reales."""


def get_history(wa_id: str) -> list[dict]:
    return _conversation_histories.setdefault(wa_id, [])


def _trim_history(wa_id: str) -> None:
    history = _conversation_histories.get(wa_id, [])
    if len(history) > MAX_HISTORY_MESSAGES:
        trimmed = history[-MAX_HISTORY_MESSAGES:]
        # Evita dejar un tool_result huérfano al inicio si el corte cayó
        # justo a la mitad de un par tool_use/tool_result
        first_content = trimmed[0].get("content") if trimmed else None
        if isinstance(first_content, list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in first_content
        ):
            trimmed = trimmed[1:]
        _conversation_histories[wa_id] = trimmed


def ask_agent(wa_id: str, user_message: str) -> tuple[str, dict | None]:
    """Devuelve (texto_para_el_cliente, solicitud_de_cita).
    solicitud_de_cita es None salvo que Claude haya usado la herramienta
    solicitar_cita, en cuyo caso trae {"nombre", "servicio", "horario"}."""
    history = get_history(wa_id)
    history.append({"role": "user", "content": user_message})

    message = get_client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": _system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[BOOKING_TOOL],
        messages=history,
    )
    history.append({"role": "assistant", "content": message.content})

    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ).strip()

    booking_request = None
    tool_use_block = next(
        (b for b in message.content if getattr(b, "type", None) == "tool_use"
         and b.name == "solicitar_cita"),
        None,
    )
    if tool_use_block:
        booking_request = dict(tool_use_block.input)
        # La API exige un tool_result antes del siguiente turno; lo simulamos
        # aquí mismo, sin otra llamada, porque la confirmación real depende
        # de que el negocio apruebe el horario (eso lo maneja main.py).
        history.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_block.id,
                "content": "Solicitud registrada. Se está confirmando disponibilidad con el negocio.",
            }],
        })
        reply = (
            f"¡Perfecto, {booking_request['nombre']}! Voy a confirmar tu cita para "
            f"{booking_request['servicio']} el {booking_request['horario']}. "
            f"En un momento te aviso si el horario está disponible 🙌"
        )
    else:
        reply = text

    _trim_history(wa_id)
    return reply, booking_request
