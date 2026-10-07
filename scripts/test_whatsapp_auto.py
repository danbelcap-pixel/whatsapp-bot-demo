"""Verifica y LIMPIA (borra de verdad) la fila de prueba que deja scripts/test-whatsapp-auto.mjs
(del lado de la plataforma) en la hoja "Clientes" real — necesita GOOGLE_SERVICE_ACCOUNT_JSON y
GOOGLE_SHEET_ID en el entorno (ya están en .env).

Uso: python scripts/test_whatsapp_auto.py --verificar "TEST WHATSAPP AUTO 169..." 999169...
"""
import argparse
import os
import sys

# La consola de Windows (cp1252) no puede imprimir "→" — forzar UTF-8 en stdout evita que la
# prueba truene a medias (antes de limpiar) solo por un caracter en un mensaje.
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

from services.sheets import CLIENTES_TAB, _batch_update, _get_metadata, _values_get  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--verificar", required=True, help="Nombre exacto del negocio de prueba (columna B)")
parser.add_argument("phone_number_id", help="Phone Number ID que se debería haber escrito en columna A")
args = parser.parse_args()

sheet_id = os.getenv("GOOGLE_SHEET_ID")
if not sheet_id:
    sys.exit("Falta GOOGLE_SHEET_ID en el entorno.")

fallos = 0


def ok(nombre, cond, detalle=""):
    global fallos
    if not cond:
        fallos += 1
    print(f"{'OK   ' if cond else 'FALLA'} {nombre}" + (f" → {detalle}" if detalle else ""))


rows = _values_get(sheet_id, f"'{CLIENTES_TAB}'!A2:P")
idx = next((i for i, r in enumerate(rows) if len(r) > 1 and r[1].strip() == args.verificar.strip()), None)
ok("se encontró la fila de prueba en la hoja Clientes", idx is not None)

if idx is not None:
    row = rows[idx]
    phone_id = row[0].strip() if len(row) > 0 else ""
    info = row[5].strip() if len(row) > 5 else ""
    activo = row[4].strip() if len(row) > 4 else ""
    ok("columna A (Phone Number ID) tiene el valor de prueba escrito por vincular-numero", phone_id == args.phone_number_id, phone_id)
    ok("columna E (Activo) = SI", activo.upper() == "SI", activo)
    ok("columna F (Información) sí trae datos reales del cuestionario", "Clínica dental" in info or "prueba automatizada" in info, info[:80])

    fila_real = idx + 2
    metadata = _get_metadata(sheet_id)
    sheet_tab_id = next((s["properties"]["sheetId"] for s in metadata.get("sheets", []) if s["properties"]["title"] == CLIENTES_TAB), None)
    if sheet_tab_id is not None:
        _batch_update(sheet_id, [{
            "deleteDimension": {
                "range": {"sheetId": sheet_tab_id, "dimension": "ROWS", "startIndex": fila_real - 1, "endIndex": fila_real},
            }
        }])
        print(f"\nFila {fila_real} borrada de verdad de la hoja Clientes (no solo vaciada).")
    else:
        print("\n⚠️ No se pudo borrar: no se encontró el sheetId de la pestaña Clientes.")

print(f"\n{fallos} falla(s)." if fallos else "\nTodo OK — verificado y limpiado.")
sys.exit(1 if fallos else 0)
