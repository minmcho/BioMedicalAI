# Agentic ADK layer (agents service)

A separate deployable service (`agents/`, port **8100**) built on the real
**Google ADK** (Agent Development Kit, Apache-2.0). It is intentionally not
part of the core backend: ADK's dependency pins live in `agents/.venv` /
the agents image only, and the core EHR suite never imports ADK.

## Shape

```
caller ──token──> agents service (FastAPI, :8100)
                    ├─ ADK roster (LlmAgent tree, LiteLlm models)
                    ├─ FHIR tools ──httpx, caller's token──> core FHIR API (:8000)
                    ├─ guardrail callback (allowlist + patient scope + audit)
                    ├─ proposals DB (agents-local, hash-chained tool audit)
                    └─ intake uploads (agents data dir, content-hash names)
```

The agents service holds **no clinical credential of its own**. Every read
goes through the core FHIR REST API with the human caller's bearer token,
so the core's RBAC and patient scoping apply unchanged. Its own database
holds only proposals, tool-call audit, ADK sessions, and upload metadata.

## Roster

| Agent | Job | Tools | Inputs |
|---|---|---|---|
| `root_orchestrator` | Routes each request to one sub-agent | none (delegation only) | text |
| `clinical_summary_agent` | Factual one-patient summary | `get_patient_summary`, `search_patient_resources` (read-only) | text |
| `results_explainer_agent` | Plain-language, hedged explanation of results | `get_patient_summary`, `get_results_inbox` (read-only) | text |
| `document_intake_agent` | Extracts candidate FHIR drafts from an uploaded document | none | text, **image, PDF** |
| `voice_note_agent` | Structured encounter-note draft from dictation | none | text, **audio** (when the model supports it) |

Multimodal inputs travel as ADK content parts: intake stores the artifact
and passes its bytes in as an image/PDF part; voice notes accept a
transcript today and audio parts when the configured model supports audio.

## Model configuration — `AGENT_MODEL`

All agents are constructed with ADK's **LiteLlm** wrapper, so one env var
chooses the model (LiteLLM model string):

- **Local-first (Min's preference, no API key):** run Ollama locally and set
  `AGENT_MODEL="ollama_chat/llama3.2:3b"` (or any local model LiteLLM can
  reach, e.g. a local OpenAI-compatible endpoint via `openai/<model>` with
  `OPENAI_API_BASE` pointed at localhost).
- **Hosted (opt-in only):** `AGENT_MODEL="gemini/gemini-2.0-flash"` or
  `"anthropic/claude-sonnet-4-5"` with that provider's own API key in the
  deployment environment. Nothing in the code requires or embeds a key.
- **Unset:** roster/proposals/intake-upload work; `POST /agents/run`
  returns **503 "model not configured"** — an honest state, not a fake run.

## Safety model (proposal → human confirm → core write)

1. **No agent has a write tool.** The tool module is read-only by
   construction; drafting agents (intake, voice) have no FHIR tools at all.
2. **Guardrail callback** (`agents/app/guardrails.py`, ADK
   `before_tool_callback`) enforces, in code: (a) per-agent tool allowlist,
   (b) the run's authorised patient scope — a tool call naming another
   patient is blocked, (c) a hash-chained audit row for EVERY tool call,
   allowed or blocked.
3. **Drafts are proposals** (`agents/app/proposals.py`). A proposal changes
   nothing. `POST /agents/proposals/{id}/confirm` (admin/clinician only)
   POSTs the payload resources to the core with the *confirming human's*
   token and records the created ids; a core rejection leaves the proposal
   pending and returns the core error. Reject is a terminal, audited-by-row
   state on the proposal.
4. **Claim safety** (system design §7): agent instructions require hedged,
   plain language from the explainer, forbid diagnosis/prescribing in every
   agent's instructions, and require clinician-review advice. This is
   prompt-level *in addition to* the code-level fences above, never instead
   of them.

## Endpoints

- `GET /agents/health` — status + whether a model is configured
- `GET /agents/roster` — agent names, descriptions, tools, multimodal inputs
- `POST /agents/run` — `{prompt, patient_id?}` via ADK Runner +
  DatabaseSessionService; 503 when no model is configured
- `GET /agents/proposals`, `GET /agents/proposals/{id}`
- `POST /agents/proposals/{id}/confirm` / `/reject` (admin/clinician)
- `POST /agents/intake/upload` — multipart PNG/JPEG/PDF only (magic-byte
  checked, 10 MB cap). Without a model the proposal parks at
  `extraction_status="awaiting_model"` with an empty resource list — no
  extraction is fabricated.

Auth mirrors the core: dev tokens by default, `AUTH_MODE=oidc` JWT/JWKS
verification (Keycloak-shaped claims) — re-implemented in this service;
services never import each other's code.

## Evals

`agents/evals/` holds an ADK evalset (routing + claim-safety) and run
instructions. Evals require a configured model and are **NOT RUN** in this
repo's SQA (no model on the build VM) — see the phase SQA report.
