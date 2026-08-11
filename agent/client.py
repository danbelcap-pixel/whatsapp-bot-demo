import os
from anthropic import Anthropic

from services.memory import get_business_notice
from services.memory import get_history as _load_history
from services.memory import save_history as _save_history
from services.sheets import get_customer_active_appointments

MODEL = "claude-opus-4-7"
MAX_TOKENS = 1024
MAX_HISTORY_MESSAGES = 20

# Frases que suenan a "ya lo hice" (registré/cancelé/moví una cita) — si
# aparecen en el texto SIN que se haya usado ninguna herramienta en ese
# turno, es una alucinación del modelo (dice que hizo algo que no hizo) y
# hay que bloquearla, no confiar únicamente en que el prompt se respete.
_FAKE_CONFIRMATION_MARKERS = [
    "✅", "ya tengo tu solicitud", "ya registr", "queda registrad",
    "solicitud registrada", "cita registrada", "ya quedó registrad",
    "ya cancel", "cancelación registrada", "cancelacion registrada",
    "ya se movió", "ya quedó cambiad", "ya quedó modificad",
    "horario actualizado", "ya la movi", "ya lo movi",
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
        "Regístra una solicitud de cita NUEVA, SOLO cuando ya tengas los "
        "tres datos confirmados por el cliente: nombre, servicio y horario "
        "preferido. No la uses si todavía falta alguno, ni para modificar "
        "una cita que el cliente ya tiene (para eso usa modificar_cita)."
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

CANCEL_TOOL = {
    "name": "cancelar_cita",
    "description": (
        "Cancela una cita EXISTENTE del cliente. Úsala solo cuando el "
        "cliente pida cancelar y tenga esa cita en la lista de 'citas "
        "activas de este cliente' que se te da abajo. Usa el folio EXACTO "
        "de esa lista — nunca inventes un folio."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "folio": {
                "type": "integer",
                "description": "Folio de la cita a cancelar, tomado de la lista de citas activas",
            },
        },
        "required": ["folio"],
    },
}

RESCHEDULE_TOOL = {
    "name": "modificar_cita",
    "description": (
        "Cambia el horario de una cita EXISTENTE del cliente (no crea una "
        "cita nueva). Úsala solo si el cliente ya tiene esa cita en la "
        "lista de 'citas activas de este cliente' de abajo y pide moverla. "
        "Usa el folio EXACTO de esa lista — nunca inventes un folio."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "folio": {"type": "integer", "description": "Folio de la cita a modificar"},
            "nuevo_horario": {
                "type": "string",
                "description": "El nuevo horario que pide el cliente, tal cual lo dijo",
            },
        },
        "required": ["folio", "nuevo_horario"],
    },
}

ALL_TOOLS = [BOOKING_TOOL, CANCEL_TOOL, RESCHEDULE_TOOL]

ANNOUNCEMENT_TOOL = {
    "name": "actualizar_aviso_negocio",
    "description": (
        "Guarda un aviso temporal para mostrarles a los clientes (cierre "
        "especial, cambio de horario, etc.). Úsala cuando el dueño del "
        "negocio te informe de algo así en su propio mensaje."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "aviso": {
                "type": "string",
                "description": "El aviso redactado claro, tal cual se le debe decir a un cliente que pregunte",
            },
        },
        "required": ["aviso"],
    },
}


def interpret_owner_instruction(text: str) -> str | None:
    """Le pregunta a Claude si este mensaje del DUEÑO es un aviso de cierre
    especial / cambio de horario. Devuelve el aviso redactado a guardar, o
    None si el mensaje no es eso."""
    negocio = os.getenv("BUSINESS_NAME", "este negocio")
    message = get_client().messages.create(
        model=MODEL,
        max_tokens=300,
        system=[{
            "type": "text",
            "text": (
                f"Eres el asistente interno de {negocio}. Este mensaje viene "
                f"del DUEÑO del negocio (no de un cliente). Si te está "
                f"avisando de un cierre especial, cambio de horario "
                f"temporal, o algo que los clientes deberían saber antes de "
                f"preguntar, usa la herramienta actualizar_aviso_negocio con "
                f"el aviso redactado claro y breve. Si el mensaje no es eso, "
                f"no uses ninguna herramienta."
            ),
        }],
        tools=[ANNOUNCEMENT_TOOL],
        messages=[{"role": "user", "content": text}],
    )
    content = [block.model_dump() for block in message.content]
    tool_block = next((b for b in content if b.get("type") == "tool_use"), None)
    return tool_block["input"]["aviso"] if tool_block else None


def _system_prompt(citas_activas: list[dict], aviso_negocio: str | None) -> str:
    negocio = os.getenv("BUSINESS_NAME", "este negocio")

    if citas_activas:
        lineas = "\n".join(
            f"- Folio #{c['folio']}: {c['servicio']}, {c['horario']} (estado: {c['estado']})"
            for c in citas_activas
        )
        citas_texto = f"CITAS ACTIVAS DE ESTE CLIENTE AHORA MISMO:\n{lineas}"
    else:
        citas_texto = "Este cliente no tiene ninguna cita activa registrada todavía."

    aviso_texto = (
        f"\nAVISO ESPECIAL VIGENTE — menciónalo si el cliente pregunta por "
        f"horarios, o de entrada si aplica a lo que está pidiendo:\n{aviso_negocio}\n"
        if aviso_negocio else ""
    )

    return f"""\
Eres el asistente de atención al cliente por WhatsApp de {negocio}.

Responde dudas de clientes de forma breve, cálida y directa, como lo haría
un empleado que conoce bien el negocio. Nunca inventes precios, horarios o
datos que no tengas — si no sabes algo, dilo y ofrece tomar el dato de
contacto para que alguien del negocio confirme.
{aviso_texto}
{citas_texto}

LO QUE SÍ PUEDES HACER (lo único real):
- Responder preguntas 24/7 sobre este negocio (horarios, ubicación,
  servicios, precios) usando la información que se te haya dado.
- Tomar solicitudes de cita NUEVA: si el cliente quiere agendar, pídele
  nombre, servicio y horario preferido. En cuanto tengas los tres datos, tu
  ÚNICA acción posible es usar la herramienta "solicitar_cita".
- Cancelar una cita EXISTENTE del cliente (ver la lista de arriba) con la
  herramienta "cancelar_cita", usando el folio exacto de la lista.
- Mover el horario de una cita EXISTENTE del cliente (ver la lista de
  arriba) con la herramienta "modificar_cita", usando el folio exacto.
- Un cliente puede tener varias citas activas a la vez (por ejemplo, una
  cita nueva no tiene nada que ver con una que ya tenía antes) — no asumas
  que solo puede haber una. Usa la lista de arriba para saber cuáles tiene.

REGLA CRÍTICA, sin excepción: si vas a registrar, cancelar o modificar algo,
tu ÚNICA forma de hacerlo es usando la herramienta correspondiente en ese
mismo turno — nunca digas frases como "ya tengo tu solicitud registrada",
"ya cancelé tu cita", "✅", o cualquier variante que suene a que ya hiciste
algo, a menos que hayas usado la herramienta de verdad en este turno. Si no
usas ninguna herramienta, no digas que hiciste ninguna acción.
Usar una herramienta NO confirma nada todavía — el negocio tiene que
aprobar el cambio primero, así que el mensaje correcto después de usar una
herramienta es que estás confirmando con el negocio y en breve avisas
(nunca digas "confirmado" hasta que el sistema te lo indique más adelante).

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


def _call_claude(
    history: list[dict],
    citas_activas: list[dict],
    aviso_negocio: str | None,
    force_any_tool: bool = False,
) -> tuple[list[dict], str, dict | None]:
    """Llama a Claude y devuelve (content_serializable, texto, tool_use_block)."""
    message = get_client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": _system_prompt(citas_activas, aviso_negocio),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=ALL_TOOLS,
        tool_choice=({"type": "any"} if force_any_tool else {"type": "auto"}),
        messages=history,
    )
    # .model_dump() convierte los bloques del SDK a dicts planos, para poder
    # guardarlos como JSON en Redis (los objetos del SDK no son serializables).
    content = [block.model_dump() for block in message.content]
    text = "".join(b["text"] for b in content if b.get("type") == "text").strip()
    tool_use_block = next((b for b in content if b.get("type") == "tool_use"), None)
    return content, text, tool_use_block


def _looks_like_fake_confirmation(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _FAKE_CONFIRMATION_MARKERS)


def ask_agent(
    wa_id: str,
    user_message: str | list[dict],
    stored_message: str | list[dict] | None = None,
) -> tuple[str, dict | None, bool]:
    """Devuelve (texto_para_el_cliente, accion, alucinacion_detectada).

    accion es None, o un dict con "type" en {"crear", "cancelar", "modificar"}:
    - crear: {"type": "crear", "nombre", "servicio", "horario"}
    - cancelar: {"type": "cancelar", "folio"}
    - modificar: {"type": "modificar", "folio", "nuevo_horario"}

    alucinacion_detectada es True si se bloqueó un mensaje que sonaba a una
    acción falsa — señal de que el prompt no se respetó, vale la pena que
    alguien lo revise aunque el sistema ya se haya autocorregido.

    stored_message: si se da, esto es lo que se GUARDA en el historial en
    vez de user_message — para no arrastrar contenido pesado (fotos en
    base64) en cada turno futuro de la conversación, que infla memoria y
    costo de API cada vez que se reenvía el historial completo."""
    citas_activas = get_customer_active_appointments(wa_id)
    aviso_negocio = get_business_notice()

    history = _load_history(wa_id)
    user_index = len(history)
    history.append({"role": "user", "content": user_message})

    content, text, tool_block = _call_claude(history, citas_activas, aviso_negocio)
    history.append({"role": "assistant", "content": content})

    hallucination_detected = False
    if not tool_block and _looks_like_fake_confirmation(text):
        # Claude actuó como si ya hubiera hecho algo pero no usó ninguna
        # herramienta — en vez de hacer que el cliente repita todo, se lo
        # forzamos en un segundo intento antes de rendirnos.
        hallucination_detected = True
        retry_content, _, retry_tool_block = _call_claude(
            history[:-1], citas_activas, aviso_negocio, force_any_tool=True
        )
        if retry_tool_block:
            content, tool_block = retry_content, retry_tool_block
            history[-1] = {"role": "assistant", "content": content}

    action = None
    if tool_block:
        name = tool_block["name"]
        inp = tool_block["input"]
        # La API exige un tool_result antes del siguiente turno; lo simulamos
        # aquí mismo, sin otra llamada, porque la confirmación real depende
        # de que el negocio apruebe el cambio (eso lo maneja main.py).
        history.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_block["id"],
                "content": "Solicitud recibida. Se está confirmando con el negocio.",
            }],
        })

        if name == "solicitar_cita":
            action = {"type": "crear", "nombre": inp["nombre"], "servicio": inp["servicio"], "horario": inp["horario"]}
            reply = (
                f"¡Perfecto, {inp['nombre']}! Voy a confirmar tu cita para "
                f"{inp['servicio']} el {inp['horario']}. En un momento te "
                f"aviso si el horario está disponible 🙌"
            )
        elif name == "cancelar_cita":
            action = {"type": "cancelar", "folio": inp["folio"]}
            reply = (
                f"Entendido, voy a tramitar la cancelación de tu cita "
                f"(folio #{inp['folio']}) con el negocio. Te confirmo en breve."
            )
        else:  # modificar_cita
            action = {"type": "modificar", "folio": inp["folio"], "nuevo_horario": inp["nuevo_horario"]}
            reply = (
                f"Entendido, voy a pedirle al negocio que mueva tu cita "
                f"(folio #{inp['folio']}) a {inp['nuevo_horario']}. Te aviso "
                f"en cuanto lo confirmen."
            )
    elif hallucination_detected:
        # Ni el intento normal ni el forzado lograron una herramienta real.
        reply = "¿Me repites exactamente qué necesitas? Quiero asegurarme de tomarlo bien."
        history[-1] = {"role": "assistant", "content": [{"type": "text", "text": reply}]}
    else:
        # Caso normal: Claude respondió con su propio texto, sin alucinar nada.
        reply = text

    if stored_message is not None:
        history[user_index] = {"role": "user", "content": stored_message}

    _save_history(wa_id, _trim(history))
    return reply, action, hallucination_detected
