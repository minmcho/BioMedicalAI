import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// One config for dev/build (Vite) and tests (Vitest reads the `test` key).
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  preview: { port: 4173 },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
