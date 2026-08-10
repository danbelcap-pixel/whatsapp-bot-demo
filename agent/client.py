import os
from anthropic import Anthropic

from services.memory import get_history as _load_history
from services.memory import save_history as _save_history

MODEL = "claude-opus-4-7"
MAX_TOKENS = 1024
MAX_HISTORY_MESSAGES = 20

# Frases que suenan a "ya registré tu cita" — si aparecen en el texto SIN que
# se haya usado la herramienta solicitar_cita en ese turno, es una alucinación
# del modelo (dice que hizo algo que no hizo) y hay que bloquearla, no confiar
# únicamente en que el prompt se respete siempre.
_FAKE_CONFIRMATION_MARKERS = [
    "✅", "ya tengo tu solicitud", "ya registr", "queda registrad",
    "solicitud registrada", "cita registrada", "ya quedó registrad",
]

_client: Anthropic | None = None


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
  servicio y horario preferido. En cuanto tengas los tres datos, tu ÚNICA
  acción posible es usar la herramienta "solicitar_cita" — no existe otra
  forma de registrar una solicitud.

REGLA CRÍTICA, sin excepción: tener los tres datos y NO usar la herramienta
"solicitar_cita" en ese mismo turno es un error grave — nunca digas frases
como "ya tengo tu solicitud registrada", "✅", o cualquier variante que
suene a que ya se guardó algo, a menos que hayas usado la herramienta
realmente en este turno. Si no la usas, no digas que registraste nada.
Usar la herramienta NO confirma la cita todavía — el negocio tiene que
aprobar el horario primero, así que el mensaje correcto después de usarla
es que estás confirmando disponibilidad y en breve avisas (nunca digas
"confirmado" hasta que el sistema te lo indique más adelante).

LO QUE NO PUEDES HACER, aunque te lo pregunten — sé honesto, nunca digas que
sí puedes: no puedes tomar pedidos de productos ni registrarlos en ningún
sistema, no puedes guardar datos de contacto en ningún CRM, no puedes
derivar la conversación a una persona real, y no tienes integración con
hojas de cálculo o sistemas de ventas. Si preguntan por alguna de estas
funciones, responde que todavía no está disponible en este chat.

Esto es una demostración: si te preguntan qué eres, explica que eres un
agente de IA conectado a WhatsApp que puede automatizar respuestas y toma de
citas 24/7 para negocios reales."""


def _trim(history: list[dict]) -> list[dict]:
    if len(history) <= MAX_HISTORY_MESSAGES:
        return history
    trimmed = history[-MAX_HISTORY_MESSAGES:]
    # Evita dejar un tool_result huérfano al inicio si el corte cayó
    # justo a la mitad de un par tool_use/tool_result
    first_content = trimmed[0].get("content") if trimmed else None
    if isinstance(first_content, list) and any(
        isinstance(c, dict) and c.get("type") == "tool_result" for c in first_content
    ):
        trimmed = trimmed[1:]
    return trimmed


def ask_agent(
    wa_id: str,
    user_message: str | list[dict],
    stored_message: str | list[dict] | None = None,
) -> tuple[str, dict | None, bool]:
    """Devuelve (texto_para_el_cliente, solicitud_de_cita, alucinacion_detectada).
    solicitud_de_cita es None salvo que Claude haya usado la herramienta
    solicitar_cita, en cuyo caso trae {"nombre", "servicio", "horario"}.
    alucinacion_detectada es True si se bloqueó un mensaje que sonaba a
    confirmación falsa (ver _FAKE_CONFIRMATION_MARKERS) — señal de que el
    prompt no se respetó y vale la pena que alguien lo revise.

    stored_message: si se da, esto es lo que se GUARDA en el historial en
    vez de user_message — para no arrastrar contenido pesado (fotos en
    base64) en cada turno futuro de la conversación, que infla memoria y
    costo de API cada vez que se reenvía el historial completo."""
    history = _load_history(wa_id)
    user_index = len(history)
    history.append({"role": "user", "content": user_message})

    content, text, tool_use_block = _call_claude(history)
    history.append({"role": "assistant", "content": content})

    hallucination_detected = False
    if not tool_use_block and _looks_like_fake_confirmation(text):
        # Claude actuó como si ya tuviera todo listo pero no usó la
        # herramienta — en vez de hacer que el cliente repita datos que ya
        # dio, se lo forzamos en un segundo intento antes de rendirnos.
        hallucination_detected = True
        retry_content, _, retry_tool_block = _call_claude(
            history[:-1], force_booking_tool=True
        )
        if retry_tool_block:
            content, tool_use_block = retry_content, retry_tool_block
            history[-1] = {"role": "assistant", "content": content}

    booking_request = None
    if tool_use_block:
        booking_request = dict(tool_use_block["input"])
        # La API exige un tool_result antes del siguiente turno; lo simulamos
        # aquí mismo, sin otra llamada, porque la confirmación real depende
        # de que el negocio apruebe el horario (eso lo maneja main.py).
        history.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use_block["id"],
                "content": "Solicitud registrada. Se está confirmando disponibilidad con el negocio.",
            }],
        })
        reply = (
            f"¡Perfecto, {booking_request['nombre']}! Voy a confirmar tu cita para "
            f"{booking_request['servicio']} el {booking_request['horario']}. "
            f"En un momento te aviso si el horario está disponible 🙌"
        )
    elif hallucination_detected:
        # Ni el intento normal ni el forzado lograron una herramienta real —
        # no había suficiente info de verdad. No repetimos el texto
        # alucinado del primer intento, pedimos los datos de forma segura.
        reply = (
            "Para tomar bien tu solicitud, ¿me confirmas tu nombre, el "
            "servicio que necesitas y el horario que buscas?"
        )
        history[-1] = {"role": "assistant", "content": [{"type": "text", "text": reply}]}
    else:
        # Caso normal: Claude pidió info faltante (o contestó cualquier otra
        # cosa) con su propio texto, sin alucinar nada — se usa tal cual.
        reply = text

    if stored_message is not None:
        history[user_index] = {"role": "user", "content": stored_message}

    _save_history(wa_id, _trim(history))
    return reply, booking_request, hallucination_detected


def _call_claude(
    history: list[dict], force_booking_tool: bool = False
) -> tuple[list[dict], str, dict | None]:
    """Llama a Claude y devuelve (content_serializable, texto, tool_use_block)."""
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
        tool_choice=(
            {"type": "tool", "name": "solicitar_cita"} if force_booking_tool else {"type": "auto"}
        ),
        messages=history,
    )
    # .model_dump() convierte los bloques del SDK a dicts planos, para poder
    # guardarlos como JSON en Redis (los objetos del SDK no son serializables).
    content = [block.model_dump() for block in message.content]
    text = "".join(b["text"] for b in content if b.get("type") == "text").strip()
    tool_use_block = next(
        (b for b in content if b.get("type") == "tool_use" and b.get("name") == "solicitar_cita"),
        None,
    )
    return content, text, tool_use_block


def _looks_like_fake_confirmation(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _FAKE_CONFIRMATION_MARKERS)
