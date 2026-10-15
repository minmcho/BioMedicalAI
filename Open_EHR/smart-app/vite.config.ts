import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// Two entry pages: index.html (the app; also the OAuth redirect target) and
// launch.html (the SMART EHR-launch entry point).
export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
        launch: fileURLToPath(new URL("./launch.html", import.meta.url)),
      },
    },
  },
});
