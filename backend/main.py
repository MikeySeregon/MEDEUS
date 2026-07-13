from fastapi import FastAPI
from fastapi import Request
from pathlib import Path
from pydantic import BaseModel
from google.cloud import bigquery
from google import genai
from google.oauth2 import service_account
from fastapi.middleware.cors import CORSMiddleware
from utils.domain_loader import load_domain
from utils.db import SessionLocal
from dotenv import load_dotenv
from typing import Dict, Any
from sqlalchemy import text

from utils.chat_storage import (
    get_or_create_conversation,
    save_message,
    get_topic_id,
    get_last_messages,
    format_history,
    save_ai_log,
    save_query_result,
    save_chart,
    get_last_query_result,
    summarize_history
)

import os
import re
import json
import shutil
import logging
import sqlglot
import markdown

load_dotenv()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
logging.basicConfig(level=logging.INFO)

app = FastAPI(
    redoc_url=None,
    openapi_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://datastudio.google.com", "https://lookerstudio.google.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/admin/deactivate-topic/{slug}")
async def deactivate_topic(slug: str, request: Request):
    if not validate_admin_key(request):
        return {"success": False}
    try:
        topic_dir = DOMAINS_DIR / slug
        if topic_dir.exists():
            (topic_dir / "disabled.flag").write_text("1")
        DOMAIN_CACHE.pop(slug, None)
        return {"success": True}
    except Exception as e:
        logging.exception(e)
        return {"success": False, "message": str(e)}

credentials = service_account.Credentials.from_service_account_file(
    "service-account.json",
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)

client = genai.Client(
    vertexai=True,
    project="admanagerapiaccess-382213",
    location="us-central1",
    credentials=credentials
)

bq = bigquery.Client(
    credentials=credentials,
    project="admanagerapiaccess-382213"
)

class Question(BaseModel):
    question: str
    session_id: str

# ==========================================
# ESQUEMA DE SALIDA ESTRUCTURADA PARA GRÁFICOS (Chart.js)
# ==========================================
# Gemini solo genera "type" y "data". Las "options" de Chart.js se fijan
# en código para evitar que el modelo devuelva configuraciones inválidas
# o inconsistentes con el frontend.
class ChartDataset(BaseModel):
    label: str
    data: list[float]

class ChartData(BaseModel):
    labels: list[str]
    datasets: list[ChartDataset]

class ChartConfig(BaseModel):
    type: str
    data: ChartData

ALLOWED_CHART_TYPES = {"bar", "line", "pie", "doughnut", "scatter"}

class TopicSync(BaseModel):
    slug: str
    active: bool
    config_json: dict
    analysis_prompt: str
    business_context: str
    dataset_context: str
    sql_base_prompt: str
    validation_prompt: str

BASE_DIR = Path(__file__).parent
DOMAINS_DIR = BASE_DIR / "temas"
SCHEMA_CACHE = {}
DOMAIN_CACHE = {}

@app.post("/chat/{domain}")
async def chat(domain: str, q: Question, request: Request):
    conversation_id = None

    # ==========================================
    # BUFFER DE TRAZABILIDAD (logs de IA / resultados de query)
    # ==========================================
    # Los logs de IA y el resultado de la query se generan en distintos
    # puntos del flujo, ANTES de que exista el message_id del mensaje
    # "assistant" que cierra el turno. Se acumulan aquí y se escriben
    # (flush) justo después de guardar ese mensaje final, sin importar
    # si el turno terminó en éxito o en cualquiera de las ramas de error.
    ai_logs_buffer = []
    pending_query_result = None
    pending_chart = None

    def buffer_ai_log(stage, prompt, response=None, success=True, error_type=None):
        ai_logs_buffer.append({
            "stage": stage,
            "prompt": prompt if prompt is not None else "PROMPT_NO_GENERADO",
            "response": response,
            "success": success,
            "error_type": error_type
        })

    def flush_ai_logs(message_id):
        while ai_logs_buffer:
            log = ai_logs_buffer.pop(0)
            try:
                save_ai_log(
                    conversation_id=conversation_id,
                    message_id=message_id,
                    stage=log["stage"],
                    prompt=log["prompt"],
                    response=log["response"],
                    success=log["success"],
                    error_type=log["error_type"]
                )
            except Exception:
                logging.exception(f"Error guardando ai_log diferido (stage={log['stage']})")

    def flush_query_result(message_id):
        nonlocal pending_query_result
        if pending_query_result:
            try:
                save_query_result(
                    conversation_id,
                    pending_query_result["sql"],
                    pending_query_result["rows"],
                    message_id
                )
            except Exception:
                logging.exception("Error guardando query_result diferido")
            pending_query_result = None

    def flush_chart(message_id):
        nonlocal pending_chart
        if pending_chart:
            try:
                save_chart(conversation_id, pending_chart, message_id)
            except Exception:
                logging.exception("Error guardando chart diferido")
            pending_chart = None

    try:
        if domain not in get_valid_domains():
            return {"answer": "Tema inválido."}
        try:
            DOMAIN = get_domain(domain)
            topic_id = get_topic_id(domain)
            if not topic_id:
                return {"answer": "Tema no registrado en base de datos."}

            conversation_id = get_or_create_conversation(topic_id, q.session_id)
            save_message(conversation_id, "user", q.question)
            history = get_last_messages(conversation_id, 10)
        except Exception as e:
            logging.exception(e)
            return {"answer": "Tema no configurado."}

        SETTINGS = DOMAIN["config"]
        ALLOWED_TABLE = SETTINGS["allowed_table"]
        BUSINESS_CONTEXT = DOMAIN["business_context"]
        DATASET_CONTEXT = DOMAIN["dataset_context"]
        VALIDATION_PROMPT = DOMAIN["validation_prompt"]
        SQL_BASE_PROMPT = DOMAIN["sql_base_prompt"]
        ANALYSIS_PROMPT = DOMAIN["analysis_prompt"]
        OUT_OF_SCOPE_ANSWER = SETTINGS["out_of_scope_answer"]

        history_text = format_history(history)

        # ==========================================
        # 1. VALIDAR TEMA (Usa validación JSON robusta)
        # ==========================================
        validation_result = await is_allowed_question_ai(
            question=q.question,
            validation_prompt=VALIDATION_PROMPT,
            history_text=history_text,
            buffer_ai_log=buffer_ai_log
        )
        if validation_result is None:
            msg = "Error al obtener la respuesta de IA para validar la pregunta."
            message_id = save_message(conversation_id, "assistant", msg, None, False, "validation_error")
            flush_ai_logs(message_id)
            return {"answer": msg}

        if not validation_result["permitido"]:
            message_id = save_message(conversation_id, "assistant", OUT_OF_SCOPE_ANSWER, None, False, "out_of_scope")
            flush_ai_logs(message_id)
            return {"answer": OUT_OF_SCOPE_ANSWER}

        # ==========================================
        # MEMORY ANALYSIS
        # ==========================================
        if not validation_result["requiere_sql"]:
            last_result = get_last_query_result(conversation_id)
            if not last_result:
                msg = "No existen resultados previos para analizar en esta conversación."
                message_id = save_message(conversation_id, "assistant", msg, None, False, "memory_analysis_without_context")
                flush_ai_logs(message_id)
                return {"answer": msg}

            if validation_result.get("requiere_grafico"):
                pending_chart = generate_chart_json(
                    question=q.question,
                    rows=last_result["result_json"],
                    history_text=history_text,
                    buffer_ai_log=buffer_ai_log
                )

            ANALYSIS_SYSTEM_PROMPT = """
                {user_rules}
                <formato_de_salida>
                - Utiliza formato Markdown limpio y estilizado.
                - Si presentas distribuciones, comparaciones geográficas por departamento/comunidad o cruces de variables, utiliza obligatoriamente tablas de Markdown para facilitar la lectura del usuario.
                </formato_de_salida>
                <historial_conversacion>
                {history}
                </historial_conversacion>
                <pregunta_usuario>
                {question}
                </pregunta_usuario>
                <sql_ejecutado>
                {sql}
                </sql_ejecutado>
                <datos_query>
                {rows}
                </datos_query>
                Genera la respuesta en Markdown siguiendo las reglas anteriores:
                """
            try:
                analysis_prompt = ANALYSIS_SYSTEM_PROMPT.format(
                    user_rules=ANALYSIS_PROMPT,
                    question=q.question,
                    sql=last_result["sql_query"],
                    rows=last_result["result_json"],
                    history=history_text
                )
            except Exception:
                logging.exception("Error construyendo prompt de análisis")
                return None
            try:
                analysis_response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=analysis_prompt
                )
                analysis_response_text = (analysis_response.text or "")
                buffer_ai_log(
                    stage="memory_analysis",
                    prompt=analysis_prompt,
                    response=analysis_response_text,
                    success=True
                )
                final_answer = (analysis_response_text.strip())
                final_answer = markdown.markdown(
                    final_answer,
                    extensions=["tables"]
                )
            except Exception as e:
                logging.exception(e)
                msg = "No fue posible generar el análisis solicitado."
                buffer_ai_log(
                    stage="memory_analysis",
                    prompt=analysis_prompt,
                    success=False,
                    error_type=str(type(e).__name__)
                )
                message_id = save_message(
                    conversation_id,
                    "assistant",
                    msg,
                    None,
                    False,
                    "memory_analysis_error",
                    str(type(e).__name__)
                )
                flush_ai_logs(message_id)
                flush_chart(message_id)
                return {"answer": msg}

            chart_to_return = pending_chart
            message_id = save_message(
                conversation_id,
                "assistant",
                final_answer,
                last_result["sql_query"]
            )
            flush_ai_logs(message_id)
            flush_chart(message_id)
            return {
                "answer": final_answer,
                "chart": chart_to_return
            }

        # ==========================================
        # 2. OBTENER SCHEMA
        # ==========================================
        schema_info = get_table_schema(ALLOWED_TABLE)

        # ==========================================
        # 3. GENERAR SQL
        # ==========================================
        SQL_SYSTEM_PROMPT = """
            {user_rules}
            <reglas_de_sintaxis>
            1. Genera EXCLUSIVAMENTE una sentencia SELECT de lectura.
            2. Devuelve SOLO el código SQL limpio. No incluyas explicaciones, introducciones ni bloques de texto adicionales. No uses bloques de código markdown (sin ```sql).
            3. Utiliza la sintaxis estándar y estricta de Google Cloud BigQuery.
            4. Para operaciones de fecha o diferencias de tiempo, utiliza obligatoriamente funciones de la familia DATE (como DATE_DIFF(..., ..., YEAR)). El año actual de operación del sistema es 2026.
            5. NUNCA agregar comentarios o anotaciones con "--"
            </reglas_de_sintaxis>

            <reglas_del_esquema>
            1. Usa ÚNICAMENTE la tabla especificada en la sección <tabla_permitida>. Está terminantemente prohibido inventar o consultar otras tablas.
            2. Utiliza SOLO los nombres exactos de los campos que aparecen en la sección <esquema_tabla>. Jamás inventes columnas.
            3. Para evitar errores de división por cero al calcular razones o tasas, envuelve siempre los denominadores usando la función SAFE_DIVIDE(numerador, denominador).
            </reglas_del_esquema>
            <funciones_disponibles>
            Al generar consultas SQL que filtren, agrupen o comparen por la columna `departamento_encuesta`, debes aplicar siempre la función de usuario (UDF) `admanagerapiaccess-382213.UsuariosOPSA.normalize_text` en el lado que tenga el texto, mientras que el campo de la tabla debe llevar los replace y lower necesarios para normalizar. Esto asegura que la búsqueda no falle por tildes o mayúsculas.
            Ejemplo de uso correcto:
            SELECT * 
            FROM `admanagerapiaccess-382213.UsuariosOPSA.View_SegExt_EncuestasTypeform`
            WHERE LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(departamento_encuesta,'á','a'),'é','e'),'í','i'),'ó','o'),'ú','u')) = `admanagerapiaccess-382213.UsuariosOPSA.normalize_text`('francisco morazán');
            </funciones_a_usar>
            <tabla_permitida>
            {allowed_table}
            </tabla_permitida>
            <esquema_tabla>
            {schema_info}
            </esquema_tabla>
            <contexto_de_negocio>
            {business_context}
            {dataset_context}
            </contexto_de_negocio>
            <historial_conversacion>
            {history_text}
            </historial_conversacion>
            <usuario_pregunta>
            Pregunta del usuario a procesar:
            "{question}"
            </usuario_pregunta>
            """
        try:
            sql_prompt = SQL_SYSTEM_PROMPT.format(
                user_rules=SQL_BASE_PROMPT,
                allowed_table=ALLOWED_TABLE,
                schema_info=schema_info,
                business_context=BUSINESS_CONTEXT,
                dataset_context=DATASET_CONTEXT,
                history_text=history_text,
                question=q.question
            )
        except Exception:
            logging.exception("Error construyendo prompt de SQL")
            return None
        try:
            # Ejecución con el SDK de GenAI
            sql_response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=sql_prompt
            )
            sql_response_text = sql_response.text or ""
            sql = clean_sql(sql_response_text)
            buffer_ai_log(
                stage="sql_generation",
                prompt=sql_prompt,
                response=sql_response_text,
                success=True
            )
        except Exception as e:
            buffer_ai_log(stage="sql_generation", prompt=sql_prompt, success=False, error_type=str(type(e).__name__))
            raise

        if not sql:
            msg = "No fue posible generar una consulta."
            message_id = save_message(conversation_id, "assistant", msg, None, False, "no_sql_generated")
            flush_ai_logs(message_id)
            return {"answer": msg}

        # ==========================================
        # 4. EVALUACIÓN Y SEGURIDAD DEL SQL
        # ==========================================
        if not is_safe_sql(sql, ALLOWED_TABLE) or not validate_sql(sql):
            msg = "La consulta generada no es segura."
            message_id = save_message(conversation_id, "assistant", msg, sql, False, "sql_validation_error", "unsafe_sql")
            flush_ai_logs(message_id)
            return {"answer": msg}

        # Ejecutar query en BigQuery
        try:
            query_job = bq.query(sql)
            rows = trim_rows([dict(row) for row in query_job])
            pending_query_result = {"sql": sql, "rows": rows}
        except Exception as e:
            logging.exception(e)
            msg = "No fue posible ejecutar la consulta."
            message_id = save_message(conversation_id, "assistant", msg, sql, False, "bq_execution_error", str(type(e).__name__))
            flush_ai_logs(message_id)
            return {"answer": msg}

        if validation_result.get("requiere_grafico"):
            pending_chart = generate_chart_json(
                question=q.question,
                rows=rows,
                history_text=history_text,
                buffer_ai_log=buffer_ai_log
            )

        # ==========================================
        # 5 y 6. INTERPRETAR RESULTADOS Y PRESENTAR
        # ==========================================
        ANALYSIS_SYSTEM_PROMPT = """
            {user_rules}
            <formato_de_salida>
            - Utiliza formato Markdown limpio y estilizado.
            - Si presentas distribuciones, comparaciones geográficas por departamento/comunidad o cruces de variables, utiliza obligatoriamente tablas de Markdown para facilitar la lectura del usuario.
            </formato_de_salida>
            <historial_conversacion>
            {history}
            </historial_conversacion>
            <pregunta_usuario>
            {question}
            </pregunta_usuario>
            <sql_ejecutado>
            {sql}
            </sql_ejecutado>
            <datos_query>
            {rows}
            </datos_query>
            Genera la respuesta en Markdown siguiendo las reglas anteriores:
            """
        analysis_prompt = ANALYSIS_SYSTEM_PROMPT.format(
            user_rules=ANALYSIS_PROMPT,
            question=q.question,
            sql=sql,
            rows=rows,
            history=history_text
        )
        try:
            analysis_response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=analysis_prompt
            )
            analysis_response_text = analysis_response.text or ""

            buffer_ai_log(
                stage="analysis",
                prompt=analysis_prompt,
                response=analysis_response_text,
                success=True
            )

            final_answer = analysis_response_text.strip()
            final_answer = markdown.markdown(final_answer, extensions=["tables"])

        except Exception as e:
            logging.exception(e)
            final_answer = "La consulta fue ejecutada correctamente, pero no fue posible generar el análisis."
            buffer_ai_log(stage="analysis", prompt=analysis_prompt, success=False, error_type=str(type(e).__name__))
            message_id = save_message(conversation_id, "assistant", final_answer, sql, False, "analysis_error", str(type(e).__name__))
            flush_ai_logs(message_id)
            flush_query_result(message_id)
            flush_chart(message_id)
            return {"answer": final_answer}

        chart_to_return = pending_chart
        message_id = save_message(conversation_id, "assistant", final_answer, sql)
        flush_ai_logs(message_id)
        flush_query_result(message_id)
        flush_chart(message_id)
        return {"answer": final_answer, "chart": chart_to_return}

    except Exception as e:
        logging.exception(e)
        if conversation_id:
            message_id = save_message(conversation_id, "assistant", "Ocurrió un error interno.", None, False, "system_error", str(type(e).__name__))
            flush_ai_logs(message_id)
            flush_query_result(message_id)
            flush_chart(message_id)
        return {"answer": "Ocurrió un error interno."}

@app.get("/chat/{domain}/history")
async def get_history(domain: str, session_id: str):
    topic_id = get_topic_id(domain)
    if not topic_id:
        return {"messages": []}
    conversation_id = get_or_create_conversation(topic_id, session_id)
    db = SessionLocal()
    rows = db.execute(
        text("""
            SELECT
                m.role,
                m.content,
                m.created_at,
                c.chart_json
            FROM chatbot_messages m
            LEFT JOIN chatbot_charts c ON c.message_id = m.id
            WHERE m.conversation_id = :conversation_id
            ORDER BY m.created_at DESC
            LIMIT 10
        """),
        {"conversation_id": conversation_id}
    ).fetchall()
    db.close()
    rows = list(reversed(rows))

    def parse_chart(chart_json):
        if not chart_json:
            return None
        try:
            # El driver puede devolver la columna JSON ya como dict o como texto,
            # según el motor; se soportan ambos casos.
            return chart_json if isinstance(chart_json, dict) else json.loads(chart_json)
        except Exception:
            logging.exception("Error parseando chart_json del historial")
            return None

    return {
        "messages": [
            {
                "role": r.role,
                "content": r.content,
                "created_at": r.created_at,
                "chart": parse_chart(r.chart_json)
            }
            for r in rows
        ]
    }

# =====================================================
# FUNCIONES AUXILIARES CORREGIDAS
# =====================================================
def clean_sql(sql):
    if not sql:
        return ""
    sql = sql.replace("```sql", "")
    sql = sql.replace("```", "")
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1]
    return sql

def is_safe_sql(sql, allowed_table):
    sql_upper = sql.upper()
    forbidden = ["DELETE", "UPDATE", "INSERT", "DROP", "ALTER", "TRUNCATE", "CREATE"]
    for word in forbidden:
        if re.search(rf"\b{word}\b", sql_upper):
            return False
    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
        return False
    try:
        parsed = sqlglot.parse_one(sql, read="bigquery")
        # 1. Limpiamos la tabla original que viene por parámetro
        original_table_clean = (
            allowed_table
            .replace("`", "")
            .upper()
            .strip()
        )
        # 2. Creamos la lista con la tabla original y añadimos el nuevo elemento (ya limpio en mayúsculas)
        allowed_tables_clean = [
            original_table_clean,
            "ADMANAGERAPIACCESS-382213.USUARIOSOPSA.NORMALIZE_TEXT"
        ]
        for table_node in parsed.find_all(sqlglot.expressions.Table):
            table_name = table_node.this.sql(dialect="bigquery").replace("`", "").upper().strip()
            if table_node.alias:
                pass  # no incluir alias en comparación
            # CTE o alias interno
            if "." not in table_name:
                continue
            # 3. Validamos si la tabla del SQL NO está en nuestra lista de permitidas
            if table_name not in allowed_tables_clean:
                return False
        return True
    except Exception as e:
        logging.error(f"Error en parseo sintáctico de seguridad: {e}")
        return False

def generate_chart_json(question, rows, history_text, buffer_ai_log=None):
    prompt = None
    try:
        CHART_SYSTEM_PROMPT = """
            Con base en los siguientes datos obtenidos de una consulta SQL, genera la
            configuración de un gráfico de Chart.js que represente mejor la respuesta
            a la pregunta del usuario.

            Reglas:
            - Elige el "type" más adecuado entre: "bar", "line", "pie", "doughnut", "scatter".
            - "labels" debe contener las categorías del eje X (o los segmentos si es pie/doughnut).
            - Cada elemento de "datasets" debe tener un "label" descriptivo y un array "data"
              numérico del mismo tamaño que "labels".
            - No inventes datos que no estén presentes en <datos_query>.
            - No agregues texto, explicaciones ni bloques markdown, solo el JSON del esquema.

            <historial_conversacion>
            {history_text}
            </historial_conversacion>
            <pregunta_usuario>
            {question}
            </pregunta_usuario>
            <datos_query>
            {rows}
            </datos_query>
            """
        prompt = CHART_SYSTEM_PROMPT.format(
            history_text=history_text,
            question=question,
            rows=rows
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": ChartConfig
            }
        )
        response_text = (response.text or "").strip()

        chart_config = ChartConfig.model_validate_json(response_text)

        if chart_config.type not in ALLOWED_CHART_TYPES:
            raise ValueError(f"Tipo de gráfico no soportado: {chart_config.type}")

        for dataset in chart_config.data.datasets:
            if len(dataset.data) != len(chart_config.data.labels):
                raise ValueError("Un dataset no coincide en longitud con 'labels'.")

        chart_json = chart_config.model_dump()
        chart_json["options"] = {"responsive": True}

        if buffer_ai_log:
            buffer_ai_log(
                stage="chart_generation",
                prompt=prompt,
                response=response_text,
                success=True
            )

        return chart_json

    except Exception as e:
        logging.exception(e)
        if buffer_ai_log:
            buffer_ai_log(
                stage="chart_generation",
                prompt=prompt or "PROMPT_NO_GENERADO",
                success=False,
                error_type=str(type(e).__name__)
            )
        # Fallo al generar el gráfico NO debe romper la respuesta textual.
        return None

async def is_allowed_question_ai(question, validation_prompt, history_text, buffer_ai_log=None):
    prompt = None
    try:
        VALIDATION_SYSTEM_PROMPT = """
            Determina si la siguiente pregunta es VÁLIDA (SI) o INVÁLIDA (NO) para ser respondida mediante un dataset de encuestas institucionales.
            Crea una respuesta en formato JSON estrictamente válido, sin markdown, bloques de código ni texto adicional.
            Formato:
            {{"permitido": true, "requiere_sql": true, "requiere_grafico": true}}
            o
            {{"permitido": true, "requiere_sql": false, "requiere_grafico": true}}
            o
            {{"permitido": false, "requiere_sql": true, "requiere_grafico": true}}
            Definición de requiere_sql:
            requiere_sql = false cuando la pregunta puede responderse únicamente utilizando los resultados obtenidos en la consulta inmediatamente anterior.
            requiere_sql = true cuando la pregunta solicita información nueva que requiere consultar nuevamente el dataset.
            Ejemplos requiere_sql=false:
            - ¿Cuál fue el segundo?
            - ¿Y en Cortés?
            - Explícalo mejor
            - Resume los resultados
            - Haz un análisis más profundo
            - ¿Qué conclusión se obtiene?
            - ¿Qué tendencias observas?
            - ¿Por qué crees que sucede?
            - Interpreta la tabla
            - Compara los primeros lugares
            - ¿Qué recomiendas hacer con estos resultados?
            - Dame recomendaciones basadas en lo anterior.
            - ¿Qué sugieres para mejorar estos indicadores?
            Ejemplos requiere_sql=true:
            - ¿Cómo está el empleo?
            - Compara empleo y seguridad.
            - Muéstrame los resultados por departamento.
            - ¿Cuál es la percepción sobre infraestructura?
            - Analiza seguridad por rango de edad.
            - ¿Qué opinan los jóvenes sobre el gobierno?
            Definición de requiere_grafico:
            - true: Si el usuario pide explícitamente "graficar", "mostrar un gráfico", "hacer una gráfica de barras", "ver visualmente", etc.
            - false: Si solo pide datos, resúmenes textuales o respuestas conversacionales normales.
            Historial:
            {history_text}
            Pregunta:
            {question}
            Reglas de validación:
            {user_rules}
            """
        try:
            prompt = VALIDATION_SYSTEM_PROMPT.format(
                question=question,
                history_text=history_text,
                user_rules=validation_prompt
            )
        except Exception:
            logging.exception("Error construyendo prompt de validación")
            return None

        # Forzar respuesta en formato JSON estructurado nativo
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        response_text = (response.text or "").strip()

        # Sanitizar si contiene marcas de bloque markdown por error
        if response_text.startswith("```"):
            response_text = response_text.replace("```json", "").replace("```", "").strip()
        # Parsear JSON de forma segura
        validation_data = json.loads(response_text)
        is_allowed = validation_data.get("permitido", False)

        if buffer_ai_log:
            buffer_ai_log(
                stage="validation",
                prompt=prompt or "PROMPT_NO_GENERADO",
                response=response_text,
                success=True,
                error_type=f"allowed={is_allowed}"
            )

        return {
            "permitido": validation_data.get(
                "permitido",
                False
            ),
            "requiere_sql": validation_data.get(
                "requiere_sql",
                True
            ),
            "requiere_grafico": validation_data.get(
                "requiere_grafico",
                False
            )
        }
    except Exception as e:
        if buffer_ai_log:
            buffer_ai_log(
                stage="validation",
                prompt=prompt or "PROMPT_NO_GENERADO",
                success=False,
                error_type=str(type(e).__name__)
            )
        logging.exception(e)
        return None

def get_domain(domain):
    if domain in DOMAIN_CACHE:
        return DOMAIN_CACHE[domain]
    data = load_domain(domain)
    DOMAIN_CACHE[domain] = data
    return data

def get_valid_domains():
    domains = []
    for d in DOMAINS_DIR.iterdir():
        if not d.is_dir():
            continue
        if not (d / "config.json").exists():
            continue
        if (d / "disabled.flag").exists():
            continue
        domains.append(d.name)
    return set(domains)

def validate_admin_key(request: Request):
    api_key = request.headers.get("x-api-key")
    return api_key == ADMIN_API_KEY

@app.post("/admin/sync-topic")
async def sync_topic(request: Request):
    data = await request.json()
    if not validate_admin_key(request):
        return {"success": False, "message": "Unauthorized"}
    try:
        topic_dir = DOMAINS_DIR / data["slug"]
        topic_dir.mkdir(parents=True, exist_ok=True)
        with open(topic_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    **data.get("config_json", {}),
                    "active": data.get("active", True)
                },
                f,
                ensure_ascii=False,
                indent=4
            )

        disabled = topic_dir / "disabled.flag"
        if disabled.exists():
            disabled.unlink()
        (topic_dir / "analysis_prompt.txt").write_text(data.get("analysis_prompt", ""), encoding="utf-8")
        (topic_dir / "business_context.txt").write_text(data.get("business_context", ""), encoding="utf-8")
        (topic_dir / "dataset_context.txt").write_text(data.get("dataset_context", ""), encoding="utf-8")
        (topic_dir / "sql_base_prompt.txt").write_text(data.get("sql_base_prompt", ""), encoding="utf-8")
        (topic_dir / "validation_prompt.txt").write_text(data.get("validation_prompt", ""), encoding="utf-8")
        (topic_dir / "disabled.flag").unlink(missing_ok=True)
        DOMAIN_CACHE.pop(data["slug"], None)
        return {"success": True}
    except Exception as e:
        logging.exception(e)
        return {"success": False, "message": str(e)}

def trim_rows(rows, max_rows=50):
    return rows[:max_rows]

def validate_sql(sql: str) -> bool:
    try:
        parsed = sqlglot.parse_one(sql, read="bigquery")
        return parsed is not None and parsed.find(sqlglot.exp.Insert) is None
    except Exception:
        return False

def get_table_schema(table_name):
    if table_name in SCHEMA_CACHE:
        return SCHEMA_CACHE[table_name]
    table = bq.get_table(table_name)
    schema_text = ""
    for field in table.schema:
        schema_text += f"\nCampo: {field.name}\nTipo: {field.field_type}\nDescripción: {field.description}\n"
    SCHEMA_CACHE[table_name] = schema_text
    return schema_text