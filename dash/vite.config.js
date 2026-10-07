import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
import { viteSingleFile } from "vite-plugin-singlefile";

export default defineConfig({
  plugins: [vue(), tailwindcss(), viteSingleFile()],
  base: "./",
  server: {
    host: "127.0.0.1",
    port: 5173,
    origin: "http://127.0.0.1:5173",
    cors: true,
  },
  build: {
    outDir: "../pages/test",
    emptyOutDir: false,
    assetsInlineLimit: 100000000,
    cssCodeSplit: false,
  },
});
