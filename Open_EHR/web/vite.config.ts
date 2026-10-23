import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Minimal ambient declaration so tsc passes without @types/node (this
// config is bundled by Vite at runtime; the declaration is type-only).
declare const process: { env: Record<string, string | undefined> };

// Dev/preview proxy: the browser only ever talks to the Vite origin and
// Vite forwards the API prefixes to the backend server-side, so local
// development involves NO cross-origin browser calls at all (a missing
// CORS setup on the backend cost a live debugging round on 2026-10-15).
// Point it at a non-default backend port with VITE_API_PROXY_TARGET,
// e.g. VITE_API_PROXY_TARGET=http://127.0.0.1:8010 npm run dev
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";
const apiPrefixes = [
  "/health",
  "/fhir",
  "/portal",
  "/studio",
  "/lab",
  "/pophealth",
  "/warehouse",
  "/billing",
  "/pharmacy",
  "/ed",
  "/radiology",
  "/dicomweb",
  "/surgery",
  "/orders",
  "/notes",
  "/flowsheets",
  "/inpatient",
  "/access",
  "/interop",
  // Phase 9 route groups (engagement): were missing from this list, so
  // portal messaging, telehealth and the quality page silently failed
  // in proxy mode while direct mode worked (found 2026-10-17).
  "/messages",
  "/engagement",
  "/quality",
  // Phase 10 route groups: agents (Track A), rules (Track B),
  // compliance (Track J), privacy / break-glass (Track K).
  "/agents",
  "/rules",
  "/compliance",
  "/privacy",
  // Phase 12 route group: care-continuum operations (Track D).
  "/continuum",
  // Phase 13 route groups: workforce + analytics (/people) and the
  // report catalogue / documents / export / import centre (/reports).
  "/people",
  "/reports",
  // Phase 14 route groups: device registry / lake / quarantine
  // (/devices) and the pipeline runner (/pipelines).
  "/devices",
  "/pipelines",
  // Front Door route groups: user accounts (/accounts) and patient
  // registration + the front-desk day view (/registration).
  "/accounts",
  "/registration",
  // Platform pass route groups: SOP/protocol framework (/protocols)
  // and hot/cold tiering (/tiering).
  "/protocols",
  "/tiering",
  // Platform pass route groups: report engine (/report-engine) and
  // the indicator library (/indicators).
  "/report-engine",
  "/indicators",
  // Home & Module UX pass route group: the role dashboard's card
  // indicators + effective-modules resolution (/home). Missing here,
  // /home/cards silently 404'd in proxy mode while direct mode
  // worked — the Phase 9 lesson repeated; pinned by review.
  "/home",
  // Platform pass route group: Studio DB platform (/dbplatform).
  "/dbplatform",
  // Platform pass WS10 route groups (Studio full parity): AI
  // provider registry (/ai-providers) and Studio ops (/studio-ops).
  "/ai-providers",
  "/studio-ops",
  // Phase 15 route groups: support ops + services (/support), care
  // coordination (/coord), payer care management (/care), governed
  // list management (/lists), the clinician results inbox (/inbox),
  // RFID (/rfid), barcodes (/barcodes), infection prevention
  // (/infection), utilisation review (/ur), immunisations
  // (/immunisations), platform depth (/platform-depth, /costing)
  // and the Studio report designer data surface (/designer).
  "/support",
  "/coord",
  "/care",
  "/lists",
  "/inbox",
  "/rfid",
  "/barcodes",
  "/infection",
  "/ur",
  "/immunisations",
  "/platform-depth",
  "/costing",
  "/designer",
  // Phase 16 route groups: directories (/directories), federation
  // (/federation), the gateway legs (/gateway), supervised agent
  // workflows (/agent-workflows), the validation harness
  // (/agent-validation), innovation pilots (/innovation), RAG
  // (/rag), MCP + A2A (/mcp, /a2a, /.well-known), imaging assist
  // (/imaging-assist), genomics (/genomics), e-signature (/esign),
  // research ops (/research), EMPI (/empi), document intake
  // (/intake), analytics (/analytics) and maintenance (/maintenance).
  "/directories",
  "/federation",
  "/gateway",
  "/agent-workflows",
  "/agent-validation",
  "/innovation",
  "/rag",
  "/mcp",
  "/a2a",
  "/.well-known",
  "/imaging-assist",
  "/genomics",
  "/esign",
  "/research",
  "/empi",
  "/intake",
  "/analytics",
  "/maintenance",
];
const apiProxy = Object.fromEntries(
  apiPrefixes.map((prefix) => [prefix, { target: apiTarget, changeOrigin: true }]),
);

// One config for dev/build (Vite) and tests (Vitest reads the `test` key).
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: apiProxy },
  preview: { port: 4173, proxy: apiProxy },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
