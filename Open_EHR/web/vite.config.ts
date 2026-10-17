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
