# -*- coding: utf-8 -*-
"""
Script de diagnostico. Ejecutalo en TU entorno (mismo venv que usa FastAPI/Uvicorn)
para ver exactamente por que se rechaza la consulta.

Uso:
    python debug_is_safe_sql.py
"""
import re
import logging
import sqlglot
from sqlglot import expressions as exp

logging.basicConfig(level=logging.DEBUG)


def is_safe_sql_debug(sql, allowed_table):
    sql_upper = sql.upper()
    forbidden = ["DELETE", "UPDATE", "INSERT", "DROP", "ALTER", "TRUNCATE", "CREATE"]

    for word in forbidden:
        if re.search(rf"\b{word}\b", sql_upper):
            print(f"[RECHAZO] Palabra prohibida detectada: {word!r}")
            return False

    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
        print("[RECHAZO] La consulta no inicia con SELECT/WITH")
        return False

    try:
        parsed = sqlglot.parse_one(sql, read="bigquery")

        original_table_clean = (
            allowed_table
            .replace("`", "")
            .upper()
            .strip()
        )
        allowed_tables_clean = [
            original_table_clean,
            "ADMANAGERAPIACCESS-382213.USUARIOSOPSA.NORMALIZE_TEXT",
        ]

        print(f"[INFO] sqlglot version: {sqlglot.__version__}")
        print(f"[INFO] allowed_table (param crudo): {allowed_table!r}")
        print(f"[INFO] allowed_tables_clean: {allowed_tables_clean!r}")

        for table_node in parsed.find_all(exp.Table):
            # Replicamos EXACTAMENTE la logica original
            table_name = table_node.this.sql(dialect="bigquery").replace("`", "").upper().strip()

            # Info adicional para diagnostico (no usada por la logica original)
            full_qualified = table_node.sql(dialect="bigquery").replace("`", "").upper().strip()

            print(f"[NODO Table] this.sql()={table_name!r} | full.sql()={full_qualified!r} "
                  f"| catalog={table_node.catalog!r} | db={table_node.db!r} | alias={table_node.alias!r}")

            if table_node.alias:
                pass

            if "." not in table_name:
                print(f"    -> '{table_name}' no tiene punto, se asume alias/CTE -> continue")
                continue

            if table_name not in allowed_tables_clean:
                print(f"    -> [RECHAZO] '{table_name}' NO esta en la whitelist")
                return False
            else:
                print(f"    -> '{table_name}' SI esta en whitelist, OK")

        print("[ACEPTADO] La consulta pasa todas las validaciones")
        return True

    except Exception as e:
        print(f"[RECHAZO] Excepcion durante el parseo: {e!r}")
        logging.error(f"Error en parseo sintactico de seguridad: {e}")
        return False


def is_safe_sql_from_capture(sql, allowed_table):
    """Util para pegar un repr() capturado en produccion y depurarlo aqui."""
    print("=" * 70)
    print("DIAGNOSTICO con datos capturados en produccion")
    print("=" * 70)
    print("sql repr():", repr(sql))
    print("allowed_table repr():", repr(allowed_table))
    print("-" * 70)
    resultado = is_safe_sql_debug(sql, allowed_table)
    print("=" * 70)
    print(f"RESULTADO FINAL: {resultado}")
    return resultado


if __name__ == "__main__":
    sql = """SELECT
    Principal_problema_seg,
    SAFE_DIVIDE(COUNT(*), SUM(COUNT(*)) OVER ()) AS proporcion
FROM
    `admanagerapiaccess-382213.UsuariosOPSA.View_SegExt_EncuestasTypeform`
WHERE
    titulo = 'Seguridad - Extorsion (03/23/2026)'
    AND `admanagerapiaccess-382213.UsuariosOPSA.normalize_text`(departamento_encuesta) = `admanagerapiaccess-382213.UsuariosOPSA.normalize_text`('francisco morazan')
GROUP BY
    Principal_problema_seg
ORDER BY
    proporcion DESC"""

    # IMPORTANTE: reemplaza esto por EXACTAMENTE el valor que le llega
    # a la funcion en produccion (revisa con un print/log justo antes
    # de llamar a is_safe_sql en tu endpoint real).
    allowed_table = "`admanagerapiaccess-382213.UsuariosOPSA.View_SegExt_EncuestasTypeform`"

    print("=" * 70)
    print("DIAGNOSTICO is_safe_sql")
    print("=" * 70)
    resultado = is_safe_sql_debug(sql, allowed_table)
    print("=" * 70)
    print(f"RESULTADO FINAL: {resultado}")