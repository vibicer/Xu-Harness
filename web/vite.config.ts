import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Xu webui host / port. The brain WS talks on ws://127.0.0.1:9876.
export default defineConfig({
  plugins: [svelte()],
  server: {
    host: "127.0.0.1",
    port: 1421,
    strictPort: true,
  },
  build: {
    target: "es2020",
  },

});
