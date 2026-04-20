import { defineConfig } from "rollup";

export default defineConfig({
  input: "src/fints-card.js",
  output: {
    file: "../config/www/fints-atruvia-card.js",
    format: "es",
    sourcemap: true,
  },
});
