# OpenCare ADK runtime service (Phase 10, Track A)

A thin FastAPI wrapper around Google ADK + LiteLLM — the agent-run
service beside the FastAPI EHR core. Agent definitions are OpenCare
Studio configuration (`kind=agent`); the EHR backend passes the full
published config with each run request.

## Run it

```bash
cd backend/adk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# local-first: run Ollama locally, then point the runtime at it
AGENT_MODEL=ollama_chat/llama3.2:3b .venv/bin/uvicorn app.main:app --port 8100
```

The EHR backend finds it via `ADK_RUNTIME_URL` (default
`http://127.0.0.1:8100`) and sends tool callbacks to `OPENCARE_API_BASE`
(default `http://127.0.0.1:8000`).

## Contract

- `GET /health` — `{ok, service, modelConfigured, model, agentsSynced}`.
  `modelConfigured` reflects AGENT_MODEL; per-agent config models also
  count at run time.
- `POST /agents/sync` `{agents: [...]}` — validate + roster configs.
- `GET /agents` — the synced roster.
- `POST /run` `{agent, prompt, imageBase64?, audioBase64?, apiBaseUrl,
  apiToken}` — `{output, model, agent}`. **Honest-off:** with no model
  on the config and no AGENT_MODEL, `/run` answers **503** with an
  explanation — it never fabricates an answer. A multimodal part the
  config did not enable is a 422. Model-call failures and timeouts are
  503s with the reason, again with no fabricated output.

Agents only ever produce drafts; the EHR backend records every run in
the hash-chained audit and a human records the disposition.
