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


def _system_prompt() -> str:
    negocio = os.getenv("BUSINESS_NAME", "este negocio")
    return f"""\
Eres el asistente de atención al cliente por WhatsApp de {negocio}.

Responde dudas de clientes de forma breve, cálida y directa, como lo haría
un empleado que conoce bien el negocio. Nunca inventes precios, horarios o
datos que no tengas — si no sabes algo, dilo y ofrece tomar el dato de
contacto para que alguien del negocio confirme.

LO QUE SÍ PUEDES HACER (lo único real): responder preguntas 24/7 sobre este
negocio (horarios, ubicación, servicios, precios) usando la información que
se te haya dado en esta conversación o en tu configuración.

LO QUE NO PUEDES HACER, aunque te lo pregunten — sé honesto si te lo piden,
nunca digas que sí puedes: no puedes agendar citas ni reservar horarios de
verdad, no puedes tomar pedidos ni registrarlos en ningún sistema, no puedes
guardar datos de contacto en ningún CRM, no puedes derivar la conversación a
una persona real, y no tienes ninguna integración con calendarios, hojas de
cálculo o sistemas de ventas. Si preguntan por alguna de estas funciones,
responde que todavía no está disponible en este chat.

Esto es una demostración: si te preguntan qué eres, explica que eres un
agente de IA conectado a WhatsApp que puede automatizar respuestas 24/7 para
negocios reales, y que funciones como agendar citas o tomar pedidos se
pueden agregar como parte del servicio, pero no vienen incluidas de fábrica."""


def get_history(wa_id: str) -> list[dict]:
    return _conversation_histories.setdefault(wa_id, [])


def _trim_history(wa_id: str) -> None:
    history = _conversation_histories.get(wa_id, [])
    if len(history) > MAX_HISTORY_MESSAGES:
        _conversation_histories[wa_id] = history[-MAX_HISTORY_MESSAGES:]


def ask_agent(wa_id: str, user_message: str) -> str:
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
        messages=history,
    )

    reply = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )

    history.append({"role": "assistant", "content": reply})
    _trim_history(wa_id)
    return reply
