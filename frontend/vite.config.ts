import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev server: the React app rides on the FastAPI serve process (D-11) — every
// backend path is proxied through, WebSocket included (`ws: true`). Production
// build lands in dist/ and is served by FastAPI's StaticFiles mount instead.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/ws": { target: "http://localhost:8137", ws: true },
      "/webhook": { target: "http://localhost:8137" },
      "/api": { target: "http://localhost:8137" },
    },
  },
  build: { outDir: "dist" },
});