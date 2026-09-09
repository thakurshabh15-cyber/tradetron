import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Frontend unit test configuration (Vitest).
// environment: jsdom — apiClient / stores / hooks use window/localStorage.
// globals: true — `describe`/`it`/`expect` available without explicit imports.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
  },
});
