# CLAUDE.md — docfun-app

This file provides guidance to Claude Code (claude.ai/code) when working with code in this directory.

## Project Overview

`docfun-app` is a Microsoft Teams chatbot **and** REST API answering questions over the documentation indexed by `docfun-ingest` (Azure Cognitive Search hybrid retrieval + Azure OpenAI generation).

Forked from `confubot-sanitas/confubot-app/app.py`. Differences:
- System prompt mentions "documentación funcional de proyectos internos" instead of "documentación de arquitectura Confluence".
- Extra intent: `inventario` (listing projects / domains, common from functional analysts).
- Renders source links by prefixing `DOCFUN_BASE_URL` if the indexed `url` field is a relative path. Set this to the Gitea browse URL (`.../src/branch/master`) so citations link to the source files.
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` parametrized — defaults to `text-embedding-3-large` but can be overridden if your Azure deployment uses a different alias.

## Commands

```bash
# Install
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run locally with hypercorn (recommended, supports reload)
hypercorn app:app --bind 0.0.0.0:8000 --reload

# Or via Quart's built-in dev server (deprecated, kept as fallback)
python app.py
```

## Architecture

Single-file Quart async server (`app.py`) with two endpoints:

- **`POST /api/messages`** — Microsoft Bot Framework endpoint for Teams. Requires `BOT_APP_ID`/`BOT_APP_SECRET`/`BOT_TENANT_ID` and a registered bot in Azure AD.
- **`POST /api/ask`** — REST endpoint with HTTP Basic Auth. Returns OpenAI-compatible JSON (Chat Completions format), so any tool that speaks OpenAI plug-and-plays.

### Request flow

1. `detect_intent()` classifies the query as `resumen` / `extraccion` / `procedimiento` / `consulta_directa` / `inventario` (Azure OpenAI with local regex fallback).
2. `search_azure_hybrid()` runs BM25 + vector search against Azure Cognitive Search (`vectorQueries` k=50, `top=15`, RRF automatic).
3. `build_context()` packs the top hits into a prompt context (cap 60k chars, 3k per doc).
4. `generate_openai_response()` calls Azure OpenAI with intent-specific system prompts. Uses `response_format: json_schema` (strict) → guaranteed `{answer, relevance_score}`.
5. If `relevance_score < RELEVANCE_THRESHOLD` (default 0.5), returns the "no encontrado" message.
6. Otherwise: answer (markdown) + deduplicated list of source links with relevance scores.

### Key components

- **Intent detection**: Azure OpenAI (`AZURE_OPENAI_DEPLOYMENT_INTENT`) with local fallback (`detect_intent_local`).
- **Hybrid search**: Azure Search with `vectorQueries` + classic BM25, RRF fusion automatic.
- **Bot adapter**: single-tenant Azure AD authentication (Bot Framework).
- **JSON Schema strict response**: prevents the LLM from going off-format.

## Environment Variables

Required:
- `AZURE_SEARCH_SERVICE`, `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_INDEX` — Azure Cognitive Search (must match `docfun-ingest`).
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT` — Azure OpenAI for chat (e.g. `gpt-4o-mini`).
- `BOT_APP_ID`, `BOT_APP_SECRET`, `BOT_TENANT_ID` — only required for the Teams endpoint (`/api/messages`). Can be empty for REST-only testing.

Optional:
- `AZURE_OPENAI_DEPLOYMENT_INTENT` — separate deployment for intent classification (default `gpt-4o-mini`).
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` — embeddings deployment (default `text-embedding-3-large`).
- `BASIC_AUTH_USER`, `BASIC_AUTH_PASS` — credentials for `/api/ask`.
- `DOCFUN_BASE_URL` — prefix applied to relative URLs at render time. Recommended:
  `https://ic.sanitas.dom/git/entrega-continua-ia/docfun-docs/src/branch/master`
- `MIN_SCORE_THRESHOLD_HYBRID` (default `0.01`) — min hybrid score (RRF scores are in the 0.01-0.05 range).
- `MIN_SCORE_THRESHOLD` (default `10`) — min classic BM25 score.
- `RELEVANCE_THRESHOLD` (default `0.5`) — below this the bot returns "no encontrado".
- `PORT` — default `8000`.

## Testing the REST endpoint

```bash
curl -s -u admin:changeme http://localhost:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"qué tools usan los agentes en ApiTask"}]}' \
  | python3 -m json.tool
```

The response is OpenAI-compatible (`choices[0].message.content` contains markdown answer + citation block).

## Known limitations

- No automated tests yet. Manual smoke testing via `/api/ask` and `docfun-ingest/query_test.py`.
- `relevance_score` is self-reported by the LLM that wrote the answer — convenient but not strictly reliable.
- No multi-turn conversation memory: each request is stateless.
- No prompt caching enabled (the ~700-token system prompt is sent on every request).

## Deployment

TODO: define Azure Web App targets (`docfun-rag-dev`, `docfun-rag-pro`) and GitHub Actions workflow analogous to confubot-sanitas.
