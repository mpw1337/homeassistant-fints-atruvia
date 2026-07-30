import { defineConfig } from "rollup";

// Two targets on purpose:
//  - custom_components/.../www/ is what ships via HACS and gets committed; the
//    integration serves it from there and registers the Lovelace resource.
//  - config/www/ keeps the local `scripts/develop` instance working.
// No source maps in either build (see SECURITY.md §9).
export default defineConfig({
  input: "src/fints-card.js",
  output: [
    {
      file: "../custom_components/fints_atruvia/www/fints-atruvia-card.js",
      format: "es",
      sourcemap: false,
    },
    {
      file: "../config/www/fints-atruvia-card.js",
      format: "es",
      sourcemap: false,
    },
  ],
});
