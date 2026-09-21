import { defineConfig } from "astro/config";

export default defineConfig({
  output: "static",
  publicDir: "../design2/assets",
  server: {
    host: "127.0.0.1",
    port: 3000,
  },
  preview: {
    host: "127.0.0.1",
    port: 3000,
  },
  vite: {
    build: {
      target: "es2022",
    },
  },
});
