from fastapi import FastAPI
from fastapi import Request
from pathlib import Path
from pydantic import BaseModel
from google.cloud import bigquery
from google import genai
from google.oauth2 import service_account
from fastapi.middleware.cors import CORSMiddleware
from utils.domain_loader import load_domain
from dotenv import load_dotenv
import os
import re
import json
import shutil
import logging

load_dotenv()

ADMIN_API_KEY = os.getenv(
    "ADMIN_API_KEY"
)

logging.basicConfig(
    level=logging.INFO
)

app = FastAPI(
    #docs_url=None,
    redoc_url=None,
    openapi_url=None
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://datastudio.google.com","https://lookerstudio.google.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {
        "status": "ok"
    }

@app.post(
    "/admin/deactivate-topic/{slug}"
)
async def deactivate_topic(
    slug: str,
    request: Request
):

    if not validate_admin_key(request):
        return {
            "success": False
        }

    try:

        topic_dir = DOMAINS_DIR / slug

        if topic_dir.exists():
            shutil.rmtree(topic_dir)

        DOMAIN_CACHE.pop(
            slug,
            None
        )

        return {
            "success": True
        }

    except Exception as e:

        logging.exception(e)

        return {
            "success": False,
            "message": str(e)
        }

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
async def chat( domain: str, q: Question, request: Request):

    if domain not in get_valid_domains():
        return {
            "answer":"Tema inválido."
        }

    referer = request.headers.get(
        "referer",
        ""
    )

    allowed = [
        "https://lookerstudio.google.com",
        "https://datastudio.google.com"
    ]

    origin = request.headers.get(
        "origin",
        ""
    )

    #logging.info(f"Origin=[{request.headers.get('origin', '')}]")
    #if origin and origin not in allowed:
    #    return {
    #        "answer": "Acceso denegado."
    #    }

    #logging.info(f"Referer=[{request.headers.get('referer', '')}]")
    #if not any(
    #    referer.startswith(x)
    #    for x in allowed
    #):
    #    return {
    #        "answer":"Acceso denegado."
    #    }

    try:
        DOMAIN = get_domain(domain)
    except Exception as e:
        logging.exception(e)
        return {
            "answer":"Tema no configurado."
        }

    SETTINGS = DOMAIN["config"]
    ALLOWED_TABLE = SETTINGS["allowed_table"]
    BUSINESS_CONTEXT = DOMAIN["business_context"]
    DATASET_CONTEXT = DOMAIN["dataset_context"]
    VALIDATION_PROMPT = DOMAIN["validation_prompt"]
    SQL_BASE_PROMPT = DOMAIN["sql_base_prompt"]
    ANALYSIS_PROMPT = DOMAIN["analysis_prompt"]
    OUT_OF_SCOPE_ANSWER = SETTINGS["out_of_scope_answer"]

    # ==========================================
    # 1. VALIDAR TEMA
    # ==========================================

    if not await is_allowed_question_ai(q.question, VALIDATION_PROMPT):
        return {
            "answer": OUT_OF_SCOPE_ANSWER
        }

    # ==========================================
    # 2. OBTENER SCHEMA
    # ==========================================

    schema_info = get_table_schema(ALLOWED_TABLE)
    
    # ==========================================
    # 3. GENERAR SQL
    # ==========================================

    sql_prompt = f"""
        {SQL_BASE_PROMPT}

        Solo usar:
        {ALLOWED_TABLE}

        SCHEMA:
        {schema_info}

        CONTEXTO DE NEGOCIO:
        {BUSINESS_CONTEXT}

        {DATASET_CONTEXT}

        Pregunta:
        {q.question}
        """

    sql_response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=sql_prompt
    )

    sql = clean_sql(sql_response.text or "")

    if not sql:
        return {
            "answer": "No fue posible generar una consulta."
        }

    # Validar fuera de dominio
    if "FUERA_DE_DOMINIO" in sql:
        return {
            "answer": OUT_OF_SCOPE_ANSWER
        }

    # Validar seguridad SQL
    if not is_safe_sql(sql, ALLOWED_TABLE):
        return {
            "answer": "La consulta generada no es segura."
        }

    # Ejecutar query
    try:
        query_job = bq.query(sql)
        rows = [dict(row) for row in query_job]

    except Exception as e:
        logging.exception(e)
        return {
            "answer": "No fue posible ejecutar la consulta.",
            "sql": sql
        }

    # ==========================================
    # 6. INTERPRETAR RESULTADOS
    # ==========================================

    analysis_prompt = ANALYSIS_PROMPT.format(
        question=q.question,
        sql=sql,
        rows=rows
    )

    try:
        analysis_response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=analysis_prompt
        )
        final_answer = (analysis_response.text or "").strip()

    except Exception as e:
        logging.exception(e)
        final_answer = (
            "La consulta fue ejecutada correctamente, "
            "pero no fue posible generar el análisis."
        )

    return {
        "answer": final_answer,
        "sql": sql
    }


# =====================================================
# FUNCIONES AUXILIARES
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

    forbidden = [
        "DELETE",
        "UPDATE",
        "INSERT",
        "DROP",
        "ALTER",
        "TRUNCATE",
        "CREATE"
    ]

    for word in forbidden:
        if word in sql_upper:
            return False

    # Solo SELECT
    if not (
        sql_upper.startswith("SELECT")
        or sql_upper.startswith("WITH")
    ):
        return False
    
    tables = re.findall(
        r'(?:FROM|JOIN)\s+`?([\w\.\-]+)`?',
        sql_upper
    )
    
    allowed_table = allowed_table.upper().replace("`", "")
    sql_upper = sql_upper.replace("`", "")

    for table in tables:
        table = table.replace("`", "")
        if table != allowed_table:
            return False

    return True

async def is_allowed_question_ai(question, validation_prompt):

    try:
        prompt = validation_prompt.format(
            question=question
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )

        result = (response.text or "").strip().upper()

        return "SI" in result
    except Exception as e:
        logging.exception(e)
        return False

def get_domain(domain):

    if domain in DOMAIN_CACHE:
        return DOMAIN_CACHE[domain]

    data = load_domain(domain)

    DOMAIN_CACHE[domain] = data

    return data

def get_valid_domains():
    return {
        d.name
        for d in DOMAINS_DIR.iterdir()
        if d.is_dir()
    }

def validate_admin_key(request: Request):

    api_key = request.headers.get(
        "X-API-KEY"
    )

    return api_key == ADMIN_API_KEY

@app.post("/admin/sync-topic")
async def sync_topic(
    data: TopicSync,
    request: Request
):

    if not validate_admin_key(request):
        return {
            "success": False,
            "message": "Unauthorized"
        }

    try:

        topic_dir = (
            DOMAINS_DIR / data.slug
        )

        topic_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        (
            topic_dir / "config.json"
        ).write_text(
            json.dumps(
                data.config_json,
                ensure_ascii=False,
                indent=4
            ),
            encoding="utf-8"
        )

        (
            topic_dir / "analysis_prompt.txt"
        ).write_text(
            data.analysis_prompt or "",
            encoding="utf-8"
        )

        (
            topic_dir / "business_context.txt"
        ).write_text(
            data.business_context or "",
            encoding="utf-8"
        )

        (
            topic_dir / "dataset_context.txt"
        ).write_text(
            data.dataset_context or "",
            encoding="utf-8"
        )

        (
            topic_dir / "sql_base_prompt.txt"
        ).write_text(
            data.sql_base_prompt or "",
            encoding="utf-8"
        )

        (
            topic_dir / "validation_prompt.txt"
        ).write_text(
            data.validation_prompt or "",
            encoding="utf-8"
        )

        DOMAIN_CACHE.pop(
            data.slug,
            None
        )

        return {
            "success": True
        }

    except Exception as e:

        logging.exception(e)

        return {
            "success": False,
            "message": str(e)
        }

def get_table_schema(table_name):

    if table_name in SCHEMA_CACHE:
        return SCHEMA_CACHE[table_name]

    table = bq.get_table(table_name)

    schema_text = ""

    for field in table.schema:
        schema_text += f"""
Campo: {field.name}
Tipo: {field.field_type}
Descripción: {field.description}
"""

    SCHEMA_CACHE[table_name] = schema_text

    return schema_text

