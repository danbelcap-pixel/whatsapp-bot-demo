import logging

from services.memory import get_cached_business_config, save_cached_business_config
from services.sheets import get_business_config_row

log = logging.getLogger("whatsapp-bot")


def get_business_config(phone_number_id: str) -> dict | None:
    """Resuelve a qué negocio pertenece un phone_number_id (el número de
    WhatsApp que recibió el mensaje), primero por cache (Redis, 5 min) y si
    no está ahí, leyendo la pestaña 'Clientes' de Sheets.

    Devuelve None si no hay ningún negocio dado de alta con ese número, o si
    está marcado inactivo — en ambos casos el mensaje se debe ignorar (no
    hay a quién avisarle ni dónde guardar nada).

    Dar de alta un negocio nuevo es agregar una fila en la pestaña
    'Clientes' — no requiere redeploy ni tocar código. El cambio puede
    tardar hasta 5 minutos en reflejarse por el cache."""
    cached = get_cached_business_config(phone_number_id)
    if cached is not None:
        return cached

    config = get_business_config_row(phone_number_id)
    if config is None:
        return None

    save_cached_business_config(phone_number_id, config)
    return config
