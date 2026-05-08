"""docfun-app — Bot Teams + REST API sobre el índice docfun-rag.

Forkeado de confubot-sanitas/confubot-app/app.py.
Cambios principales:
- Prompt base habla de "documentación funcional de proyectos internos".
- Intención adicional: 'inventario' (listar proyectos / dominios).
- Si DOCFUN_BASE_URL está definida, prefija las rutas relativas almacenadas en el índice.
- Campos extra del índice (project, domain, section, stack) se incluyen en el contexto y enlaces.
"""
import base64
import json
import logging
import os
import re
import time
from functools import wraps
from typing import Dict, List

import requests
from botbuilder.core import (
    BotFrameworkAdapter,
    BotFrameworkAdapterSettings,
    TurnContext,
)
from botbuilder.schema import Activity, ActivityTypes
from dotenv import load_dotenv
from openai import AzureOpenAI
from quart import Quart, Response, jsonify, request

load_dotenv()

logging.basicConfig(level=logging.INFO)

app = Quart(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AZURE_SEARCH_SERVICE = os.getenv("AZURE_SEARCH_SERVICE")
AZURE_SEARCH_API_KEY = os.getenv("AZURE_SEARCH_API_KEY")
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX")

AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
AZURE_OPENAI_DEPLOYMENT_INTENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_INTENT", "gpt-4o-mini")
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.getenv(
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"
)

USERNAME = os.getenv("BASIC_AUTH_USER", "admin")
PASSWORD = os.getenv("BASIC_AUTH_PASS", "password")

BOT_APP_ID = os.getenv("BOT_APP_ID")
BOT_APP_SECRET = os.getenv("BOT_APP_SECRET")
BOT_TENANT_ID = os.getenv("BOT_TENANT_ID")

DOCFUN_BASE_URL = os.getenv("DOCFUN_BASE_URL", "").rstrip("/")
RELEVANCE_THRESHOLD = float(os.getenv("RELEVANCE_THRESHOLD", "0.5"))

openai_client = AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
    api_version="2024-02-01",
)

settings = BotFrameworkAdapterSettings(
    app_id=BOT_APP_ID,
    app_password=BOT_APP_SECRET,
    channel_auth_tenant=BOT_TENANT_ID,
    oauth_endpoint=f"https://login.microsoftonline.com/{BOT_TENANT_ID}/oauth2/v2.0/token",
)
adapter = BotFrameworkAdapter(settings)
logging.info("🤖 docfun-app inicializado AppId=%s Tenant=%s", BOT_APP_ID, BOT_TENANT_ID)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PROMPT_BASE = (
    "Eres un asistente experto en la documentación funcional interna de proyectos de software de la compañía. "
    "El conocimiento cubre 281 proyectos (Spring Boot, Spring Batch, Ionic/Angular, PHP, PL/SQL, COBOL, Node.js, WCS) "
    "y 22 dominios de negocio. "
    "Tu alcance es EXCLUSIVAMENTE esa documentación funcional y técnica interna. "
    "Si la consulta no tiene relación, responde brevemente que solo puedes ayudar con esa documentación. "
    "Formatea SIEMPRE en Markdown conciso y profesional: encabezados (##), listas, bloques de código (```). "
    "Sé directo y estructurado, evita párrafos largos. "
    "Cuando cites contenido, indica de qué proyecto o dominio procede si está disponible."
)

INTENT_PROMPTS = {
    "resumen": (
        "Resume la información en 3-5 puntos clave. "
        "Usa viñetas y destaca en negrita los conceptos principales."
    ),
    "extraccion": (
        "Extrae y organiza los datos clave en listas o tablas markdown. "
        "Agrupa la información por categoría cuando haya varios temas."
    ),
    "consulta_directa": (
        "Responde de forma precisa y estructurada. "
        "Usa encabezados si la respuesta abarca varios aspectos."
    ),
    "procedimiento": (
        "Explica el procedimiento como una lista numerada de pasos. "
        "Incluye bloques de código para comandos o configuraciones."
    ),
    "inventario": (
        "Devuelve un listado tabular o por viñetas con nombre del proyecto/dominio, "
        "su propósito en una línea y su stack tecnológico si está disponible."
    ),
}

LOW_RELEVANCE_MESSAGE = (
    "No he encontrado documentación relevante para tu consulta. "
    "Recuerda que solo puedo ayudarte con documentación funcional de proyectos internos. "
    "Intenta reformular tu pregunta con más detalle (nombre de proyecto, dominio, stack…)."
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_basic_auth(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Basic "):
            return Response(
                "Unauthorized",
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="Login Required"'},
            )
        try:
            decoded = base64.b64decode(auth.split(" ")[1]).decode("utf-8")
            user, pwd = decoded.split(":", 1)
        except Exception:
            return Response("Unauthorized", status=401)
        if user != USERNAME or pwd != PASSWORD:
            return Response("Unauthorized", status=401)
        return await func(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_mentions(text: str) -> str:
    return re.sub(r"<at>[^<]*</at>", "", text).strip()


def render_url(stored_url: str) -> str:
    """Prefija DOCFUN_BASE_URL si el url almacenado es relativo."""
    if not stored_url:
        return stored_url
    if stored_url.startswith(("http://", "https://")):
        return stored_url
    if DOCFUN_BASE_URL:
        return f"{DOCFUN_BASE_URL}/{stored_url.lstrip('/')}"
    return stored_url


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def generate_embedding(text: str) -> List[float]:
    cleaned = text.strip()
    if not cleaned:
        return [0.0] * 1536
    if len(cleaned) > 8000:
        cleaned = cleaned[:8000]
    try:
        result = openai_client.embeddings.create(
            model=AZURE_OPENAI_EMBEDDING_DEPLOYMENT,
            input=cleaned,
            dimensions=1536,
        )
        return result.data[0].embedding
    except Exception as exc:
        logging.error("Error generando embedding: %s", exc)
        return [0.0] * 1536


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

VALID_INTENTS = {"resumen", "extraccion", "procedimiento", "consulta_directa", "inventario"}


def detect_intent(query: str) -> str:
    try:
        return detect_intent_openai(query)
    except Exception as exc:
        logging.warning("⚠️ Fallback a intent local: %s", exc)
        return detect_intent_local(query)


def detect_intent_local(query: str) -> str:
    q = query.lower().strip()
    if any(w in q for w in ["lista", "listado", "qué proyectos", "que proyectos", "inventario", "qué dominios", "que dominios"]):
        return "inventario"
    if any(w in q for w in ["cómo", "como", "pasos", "configurar", "instalar", "setup", "crear", "hacer"]):
        return "procedimiento"
    if any(w in q for w in ["resume", "resumen", "qué es", "que es", "explica"]):
        return "resumen"
    if any(w in q for w in ["extrae", "puntos", "datos", "tabla"]):
        return "extraccion"
    return "consulta_directa"


def detect_intent_openai(query: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "Clasifica esta consulta como 'resumen', 'extraccion', 'procedimiento', "
                "'inventario' o 'consulta_directa'. "
                "'inventario' = el usuario quiere un listado de proyectos o dominios. "
                "Responde SOLO con una de esas palabras exactas."
            ),
        },
        {"role": "user", "content": query},
    ]
    result = openai_client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT_INTENT,
        messages=messages,
        temperature=0,
        max_tokens=10,
    )
    intent = result.choices[0].message.content.strip().lower()
    if intent not in VALID_INTENTS:
        logging.warning("⚠️ Intent inesperado: '%s', fallback local", intent)
        return detect_intent_local(query)
    return intent


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_azure(query: str) -> List[Dict]:
    return search_azure_hybrid(query)


def search_azure_hybrid(query: str) -> List[Dict]:
    logging.info("🔍 Búsqueda híbrida: '%s'", query)
    embedding = generate_embedding(query)

    url = f"https://{AZURE_SEARCH_SERVICE}.search.windows.net/indexes/{INDEX_NAME}/docs/search?api-version=2024-07-01"
    headers = {"Content-Type": "application/json", "api-key": AZURE_SEARCH_API_KEY}

    if all(v == 0.0 for v in embedding):
        logging.warning("⚠️ Embedding cero, fallback keyword")
        return search_azure_classic(query)

    payload = {
        "search": query,
        "searchMode": "all",
        "vectorQueries": [
            {"kind": "vector", "vector": embedding, "fields": "content_vector", "k": 50}
        ],
        "top": 15,
        "select": "title,content,url,type,project,domain,section,stack",
        "highlight": "content",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        results = response.json().get("value", [])
        threshold = float(os.getenv("MIN_SCORE_THRESHOLD_HYBRID", "0.01"))
        filtered = [d for d in results if d.get("@search.score", 0) >= threshold]
        logging.info("🔍 %d encontrados, %d relevantes", len(results), len(filtered))
        return filtered
    except requests.RequestException as exc:
        logging.error("❌ Error búsqueda híbrida: %s", exc)
        return []


def search_azure_classic(query: str) -> List[Dict]:
    url = f"https://{AZURE_SEARCH_SERVICE}.search.windows.net/indexes/{INDEX_NAME}/docs/search?api-version=2024-07-01"
    headers = {"Content-Type": "application/json", "api-key": AZURE_SEARCH_API_KEY}
    payload = {
        "search": query,
        "top": 15,
        "select": "title,content,url,type,project,domain,section,stack",
    }
    response = requests.post(url, headers=headers, json=payload)
    if not response.ok:
        return []
    threshold = float(os.getenv("MIN_SCORE_THRESHOLD", "10"))
    return [d for d in response.json().get("value", []) if d.get("@search.score", 0) >= threshold]


# ---------------------------------------------------------------------------
# Context + generation
# ---------------------------------------------------------------------------

def build_context(search_results: List[Dict], max_total_chars: int = 60000) -> str:
    parts: List[str] = []
    total = 0
    for doc in search_results:
        title = doc.get("title", "Documento sin título")
        content = (doc.get("content") or "")[:3000]
        project = doc.get("project") or ""
        domain = doc.get("domain") or ""
        section = doc.get("section") or ""

        # Cabecera con metadatos para que el LLM sepa el origen del fragmento
        meta_bits = []
        if project:
            meta_bits.append(f"proyecto={project}")
        if domain:
            meta_bits.append(f"dominio={domain}")
        if section:
            meta_bits.append(f"sección={section}")
        meta = f" [{', '.join(meta_bits)}]" if meta_bits else ""

        entry = f"- **{title}**{meta}: {content}"
        if total + len(entry) > max_total_chars:
            break
        parts.append(entry)
        total += len(entry)

    return "\n".join(parts)


def generate_openai_response(query: str, context: str, intent: str):
    instruction = (
        f"{PROMPT_BASE} {INTENT_PROMPTS.get(intent, INTENT_PROMPTS['consulta_directa'])} "
        "Responde únicamente usando el contenido proporcionado. "
        "Si la pregunta es ambigua o demasiado corta, pide al usuario que concrete en 1-2 frases. "
        "Si no encuentras información relevante, indica que no hay suficiente información. "
        'Responde SIEMPRE en JSON: {"answer": "<markdown>", "relevance_score": <0.0-1.0>}.'
    )
    messages = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": f"### DOCUMENTOS:\n{context}\n\n### PREGUNTA:\n{query}"},
    ]
    result = openai_client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=messages,
        max_tokens=2048,
        temperature=0.3,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "bot_response",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string"},
                        "relevance_score": {"type": "number"},
                    },
                    "required": ["answer", "relevance_score"],
                    "additionalProperties": False,
                },
            },
        },
    )
    raw = result.choices[0].message.content
    try:
        parsed = json.loads(raw)
        return parsed.get("answer", raw), parsed.get("relevance_score", 1.0)
    except (json.JSONDecodeError, AttributeError):
        return raw, 1.0


def generate_response_by_intent(query: str, search_results: List[Dict], intent: str) -> str:
    context = build_context(search_results)
    response, score = generate_openai_response(query, context, intent)
    logging.info("📊 Relevance score: %s", score)
    if score < RELEVANCE_THRESHOLD:
        return LOW_RELEVANCE_MESSAGE

    seen = set()
    enlaces = []
    for doc in search_results:
        url = doc.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        rendered = render_url(url)
        title = doc.get("title", "Documento")
        score_str = f"{doc.get('@search.score', 0.0):.3f}"
        enlaces.append(f"- 🔗 [{title}]({rendered}) (score: {score_str})")

    if enlaces:
        response += "\n\n---\n" + "\n".join(enlaces)
    return response


# ---------------------------------------------------------------------------
# Teams handler
# ---------------------------------------------------------------------------

async def on_message_activity(turn_context: TurnContext):
    raw = turn_context.activity.text or ""
    user_query = strip_mentions(raw)
    logging.info("🔍 Query: '%s'", user_query)

    if not user_query:
        await turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                text="Hola, soy docfun-app. ¿Sobre qué proyecto o dominio quieres consultar?",
            )
        )
        return

    await turn_context.send_activity(Activity(type=ActivityTypes.typing))

    intent = detect_intent(user_query)
    logging.info("🎯 Intent: %s", intent)

    search_results = search_azure(user_query)
    response_text = generate_response_by_intent(user_query, search_results, intent)

    await turn_context.send_activity(Activity(type=ActivityTypes.message, text=response_text))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/messages", methods=["POST"])
async def messages():
    try:
        body = await request.get_json()
        auth_header = request.headers.get("Authorization", "")
        activity = Activity().deserialize(body)

        async def aux_func(turn_context: TurnContext):
            try:
                if turn_context.activity.type == ActivityTypes.message and turn_context.activity.text:
                    await on_message_activity(turn_context)
            except Exception as exc:
                logging.error("❌ Error procesando mensaje: %s", exc, exc_info=True)
                await turn_context.send_activity(
                    Activity(type=ActivityTypes.message, text="Se ha producido un error procesando tu mensaje.")
                )

        await adapter.process_activity(activity, auth_header, aux_func)
        return Response(status=201)
    except PermissionError:
        return Response("Unauthorized", status=401)
    except Exception as exc:
        logging.error("❌ Error /api/messages: %s", exc, exc_info=True)
        return Response("Internal Server Error", status=500)


@app.route("/api/ask", methods=["POST"])
@require_basic_auth
async def ask():
    try:
        data = await request.get_json()
        messages_in = data.get("messages", [])
        if not messages_in or not isinstance(messages_in, list):
            return jsonify({"error": "Missing or invalid 'messages' field"}), 400

        user_message = next(
            (m["content"] for m in reversed(messages_in) if m.get("role") == "user"),
            None,
        )
        if not user_message:
            return jsonify({"error": "No user message found"}), 400

        intent = detect_intent(user_message)
        results = search_azure(user_message)
        response_text = generate_response_by_intent(user_message, results, intent)

        return jsonify({
            "id": "chatcmpl-docfun",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "docfun-rag",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": response_text},
                    "finish_reason": "stop",
                }
            ],
        })
    except Exception as exc:
        logging.error("❌ Error /api/ask: %s", exc, exc_info=True)
        return jsonify({"error": "Internal Server Error"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=True)
