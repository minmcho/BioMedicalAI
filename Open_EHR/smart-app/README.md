# OpenCare — SMART on FHIR companion app

A SMART on FHIR (SMART App Launch) patient app built on the open-source
**`fhirclient`** library (v3, Apache-2.0) with Vite + TypeScript. It renders
one patient's **summary, conditions, medications, and lab trends**
(inline SVG sparklines, grouped by LOINC code) from any FHIR R4 server it
is launched against — including the public sandbox operated by Epic for
developers, which is this app's original Phase 0 target.

Two entry points:

- **`launch.html`** — the EHR-launch URL. The launcher opens it with
  `iss` + `launch` parameters; the page starts OAuth via
  `fhirclient`'s `oauth2.authorize()`.
- **`index.html`** — the app itself, and the OAuth **redirect URI**
  (fhirclient's default redirect is the directory of the launch page,
  `"."`, so register the app **root URL** as the redirect URI and
  `<root>launch.html` as the launch URI — see registration below).
  With no SMART session it falls back to a clearly labelled local demo
  mode against an OpenCare backend.

## Run it locally (no EHR registration needed)

```bash
# 1. Core backend with synthetic seed data (from the repo root):
cd backend && ../.venv/bin/uvicorn app.main:app --port 8000 &
BASE_URL=http://127.0.0.1:8000 TOKEN=admin-token ../.venv/bin/python seed/seed.py

# 2. This app:
cd ../smart-app && npm install && npm run dev   # http://localhost:5173
```

Open the page, use **"Connect to OpenCare backend (dev)"** (defaults:
`http://127.0.0.1:8000`, token `clinician-token`), pick a seeded patient.
This mode uses the backend's dev-token auth and fabricated seed data —
local development only.

## Register the app with the Epic sandbox (fhir.epic.com)

A live launch against the sandbox needs a **client_id registered under
your own developer account** — only you can create it (~5 minutes):

1. Create/sign in to a free developer account at <https://fhir.epic.com>.
2. **Build Apps → Create**: name it (e.g. "OpenCare Patient Summary"),
   application type for browser use.
3. **Audience: choose "Patients"** for this app as shipped — it launches
   in patient context and requests `launch/patient` + `patient/*.rs`
   scopes. (For a clinician-workflow variant, choose "Clinicians or
   Administrative Users" and change the scope in the app settings to a
   `launch` + `user/*.rs` set.)
4. **Redirect URI:** the app root URL, e.g. `http://localhost:5173/` for
   local development or `https://<your-host>/` when deployed. **Launch
   URI:** the same root + `launch.html`, e.g.
   `http://localhost:5173/launch.html`.
5. **Incoming APIs (FHIR R4):** select read/search for **Patient,
   Condition, MedicationRequest, Observation**.
6. Save — the portal issues a **client_id** (non-production/sandbox).
   Put it in the app: either paste it into the **SMART settings** form on
   the index page (stored in this browser's localStorage) or set
   `VITE_SMART_CLIENT_ID` in `smart-app/.env.local` and restart the dev
   server / rebuild.
7. Launch: from the developer portal's app page use its sandbox test
   launch (pick a sandbox test patient), or open
   `http://localhost:5173/launch.html` via the SMART Health IT launcher
   below. After authorization you land back on the index page in SMART
   mode with that patient's summary.

Note: if the sandbox rejects a `localhost` redirect URI in your account,
serve the built app (`npm run build`, `dist/`) from any HTTPS URL you
control and register that URL instead — the app is static files.

### Alternative public launcher (no registration issued there)

<https://launch.smarthealthit.org> simulates a provider/patient EHR
launch: set its **App Launch URI** to `http://localhost:5173/launch.html`,
launch, and sign in with the sandbox's demo credentials it offers. That
sandbox does not issue a real client_id — put any value (e.g.
`opencare-dev`) in the app settings; the flow still exercises the real
authorize → redirect → `oauth2.ready()` path against its FHIR server.

## What is NOT verified yet

- **Live Epic-sandbox launch: NOT RUN.** It requires the client_id from
  the registration above (your account, your step). Everything up to that
  step — build, unit tests, local mode end-to-end against the seeded
  OpenCare backend, and the SMART Health IT-style flow shape — is covered
  in `docs/sqa/PHASE_SMART_SYNTHEA_SQA_REPORT.md`.
- A production launch inside a real hospital's EHR instance additionally
  needs that organisation's own app approval; nothing here claims it.

## Development

```bash
npm test        # Vitest: data-shaping (bundles, summary, trend grouping, sparklines)
npm run build   # tsc --noEmit && vite build -> dist/ (index.html + launch.html)
```
