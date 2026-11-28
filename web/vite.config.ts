import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/** Gateway that the dev server proxies to; the default matches `mertina gateway`. */
const gateway = process.env.MERTINA_WEB_GATEWAY_URL ?? "http://127.0.0.1:8765";

// The Gateway accepts only requests whose Host and Origin name the Gateway itself.
// `changeOrigin` rewrites Host; Origin has to be rewritten here. The Gateway's
// checks stay as strict as in production.
const proxy = {
  target: gateway,
  changeOrigin: true,
  configure(server: { on(event: "proxyReq", listener: ProxyRequestListener): void }) {
    server.on("proxyReq", (request) => {
      if (request.getHeader("origin") !== undefined) {
        request.setHeader("origin", new URL(gateway).origin);
      }
    });
  },
};

type ProxyRequestListener = (request: {
  getHeader(name: string): unknown;
  setHeader(name: string, value: string): void;
}) => void;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    proxy: { "/api": proxy, "/v1": proxy },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // The Gateway's CSP allows same-origin files only, so nothing may be inlined
    // as a data: URI (fonts in particular).
    assetsInlineLimit: 0,
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.{ts,tsx}"],
    setupFiles: ["tests/setup.ts"],
    restoreMocks: true,
  },
});
