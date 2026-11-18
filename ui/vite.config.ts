import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// External, relative assets work at / and /admin/ under the Python CSP.
export default defineConfig({
    base: "./",
    plugins: [preact()],
    build: {
        outDir: "../src/graylog_mcp/admin/static",
        emptyOutDir: true,
        assetsDir: "assets",
        assetsInlineLimit: 0,
    },
    server: {
        proxy: {
            "/api": {
                target: process.env.ADMIN_URL ?? "http://127.0.0.1:8765",
                changeOrigin: true,
            },
        },
    },
});
