# docfun-app

Bot de Microsoft Teams + REST API que responde preguntas sobre la documentación indexada por `docfun-ingest`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# editar .env (mínimo: AZURE_SEARCH_*, AZURE_OPENAI_*)
# DOCFUN_BASE_URL=https://ic.sanitas.dom/git/entrega-continua-ia/docfun-docs/src/branch/master

# arrancar
hypercorn app:app --bind 0.0.0.0:8000 --reload
```

## Endpoints

- `POST /api/messages` — Microsoft Bot Framework (Teams). Requiere `BOT_APP_*`.
- `POST /api/ask` — REST con HTTP Basic Auth, formato OpenAI Chat Completions. Solo requiere `AZURE_*`.

## Probar el endpoint REST

```bash
curl -s -u admin:changeme http://localhost:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"qué tools usan los agentes en ApiTask"}]}' \
  | python3 -m json.tool
```

La respuesta incluye `choices[0].message.content` con la respuesta en markdown + un bloque de citas con enlaces a los `.md` originales en Gitea (si `DOCFUN_BASE_URL` está configurada).

Ver `CLAUDE.md` para arquitectura, intents disponibles y variables de entorno.
