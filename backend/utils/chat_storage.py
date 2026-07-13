from sqlalchemy import text
from utils.db import SessionLocal
import json
import logging

def get_or_create_conversation(topic_id: int, session_id: str):
    db = SessionLocal()

    try:
        row = db.execute(
            text("""
                SELECT id
                FROM chatbot_conversations
                WHERE topic_id = :topic_id
                AND session_id = :session_id
                LIMIT 1
            """),
            {
                "topic_id": topic_id,
                "session_id": session_id
            }
        ).fetchone()

        if row:
            return row.id

        db.execute(
            text("""
                INSERT INTO chatbot_conversations
                (topic_id, session_id, created_at, updated_at)
                VALUES
                (:topic_id, :session_id, NOW(), NOW())
            """),
            {
                "topic_id": topic_id,
                "session_id": session_id
            }
        )

        db.commit()

        row = db.execute(text("SELECT LAST_INSERT_ID() AS id")).fetchone()
        return row.id

    finally:
        db.close()

def save_message(conversation_id: int,
    role: str,
    content: str,
    sql_query: str = None,
    include_in_context: bool = True,
    message_status: str = "success",
    error_type: str = None):
    db = SessionLocal()

    try:

        if (
            role == "assistant"
            and not include_in_context
        ):
            db.execute(
                text("""
                    UPDATE chatbot_messages
                    SET include_in_context = 0
                    WHERE id = (
                        SELECT id
                        FROM (
                            SELECT id
                            FROM chatbot_messages
                            WHERE conversation_id = :conversation_id
                            AND role = 'user'
                            AND include_in_context = 1
                            ORDER BY created_at DESC
                            LIMIT 1
                        ) t
                    )
                """),
                {
                    "conversation_id": conversation_id
                }
            )

        db.execute(
            text("""
                INSERT INTO chatbot_messages
                (
                    conversation_id,
                    role,
                    content,
                    sql_query,
                    include_in_context,
                    message_status,
                    error_type,
                    created_at
                )
                VALUES
                (
                    :conversation_id,
                    :role,
                    :content,
                    :sql_query,
                    :include_in_context,
                    :message_status,
                    :error_type,
                    NOW()
                )
            """),
            {
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
                "sql_query": sql_query,
                "include_in_context": include_in_context,
                "message_status": message_status,
                "error_type": error_type
            }
        )

        message_id = db.execute(text("SELECT LAST_INSERT_ID()")).scalar()

        db.commit()

        return message_id

    except:
        db.rollback()
        raise

    finally:
        db.close()

def get_topic_id(domain: str):
    db = SessionLocal()

    try:
        row = db.execute(
            text("""
                SELECT id
                FROM chatbot_topics
                WHERE slug = :slug
                LIMIT 1
            """),
            {"slug": domain}
        ).fetchone()

        return row.id if row else None

    finally:
        db.close()

def get_last_messages(conversation_id: int, limit: int = 10):
    db = SessionLocal()

    try:
        rows = db.execute(
            text("""
                SELECT role, content, sql_query, created_at
                FROM chatbot_messages
                WHERE conversation_id = :conversation_id
                AND include_in_context = 1
                ORDER BY created_at DESC
                LIMIT :limit
            """),
            {
                "conversation_id": conversation_id,
                "limit": limit
            }
        ).fetchall()

        return list(reversed(rows))

    finally:
        db.close()

def format_history(history):
    return "\n---\n".join(
        f"ROLE: {h.role.upper()}\nMESSAGE: {h.content}"
        for h in history
    )

def save_ai_log(
    conversation_id: int,
    stage: str,
    prompt: str,
    response: str = None,
    success: bool = True,
    error_type: str = None,
    message_id: int = None
):
    prompt = prompt or ""
    response = response or ""
    error_type = error_type or ""
    db = SessionLocal()

    try:

        db.execute(
            text("""
                INSERT INTO chatbot_ai_logs
                (
                    conversation_id,
                    message_id,
                    stage,
                    prompt,
                    response,
                    success,
                    error_type,
                    created_at,
                    updated_at
                )
                VALUES
                (
                    :conversation_id,
                    :message_id,
                    :stage,
                    :prompt,
                    :response,
                    :success,
                    :error_type,
                    NOW(),
                    NOW()
                )
            """),
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "stage": stage,
                "prompt": prompt,
                "response": response,
                "success": success,
                "error_type": error_type
            }
        )

        db.commit()

    except:
        db.rollback()
        raise

    finally:
        db.close()

def save_query_result(
    conversation_id,
    sql,
    rows,
    message_id
):
    db = SessionLocal()

    try:

        db.execute(
            text("""
                INSERT INTO chatbot_query_results
                (
                    conversation_id,
                    message_id,
                    sql_query,
                    result_json,
                    created_at,
                    updated_at
                )
                VALUES
                (
                    :conversation_id,
                    :message_id,
                    :sql_query,
                    :result_json,
                    NOW(),
                    NOW()
                )
            """),
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "sql_query": sql,
                "result_json": json.dumps(rows, ensure_ascii=False)
            }
        )

        db.commit()

    except Exception as e:
        logging.exception(e)
        db.rollback()
        raise

    finally:
        db.close()

def save_chart(
    conversation_id,
    chart_json,
    message_id
):
    db = SessionLocal()

    try:

        db.execute(
            text("""
                INSERT INTO chatbot_charts
                (
                    conversation_id,
                    message_id,
                    chart_json,
                    created_at,
                    updated_at
                )
                VALUES
                (
                    :conversation_id,
                    :message_id,
                    :chart_json,
                    NOW(),
                    NOW()
                )
            """),
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "chart_json": json.dumps(chart_json, ensure_ascii=False)
            }
        )

        db.commit()

    except Exception as e:
        logging.exception(e)
        db.rollback()
        raise

    finally:
        db.close()

def get_last_query_result(conversation_id):
    db = SessionLocal()

    try:
        row = db.execute(
            text("""
                SELECT sql_query, result_json
                FROM chatbot_query_results
                WHERE conversation_id = :conversation_id
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {
                "conversation_id": conversation_id
            }
        ).fetchone()

        if not row:
            return None

        try:
            result_json = json.loads(row.result_json)
        except:
            result_json = []

        return {
            "sql_query": row.sql_query,
            "result_json": result_json
        }

    finally:
        db.close()

def summarize_history(history):
    text = "\n---\n".join(
        f"ROLE: {h.role.upper()}\nMESSAGE: {h.content}"
        for h in history
    )

    prompt = f"""
Resume la siguiente conversación manteniendo solo:
- temas principales
- datos relevantes
- decisiones o resultados importantes

No incluyas texto innecesario.

CONVERSACIÓN:
{text}

Devuelve un resumen corto y técnico.
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    return (response.text or "").strip()