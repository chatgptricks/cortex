import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Relative base so the build works on GitHub Pages project sites.
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
    headers: {
      "Cache-Control": "no-store"
    }
  }
});
