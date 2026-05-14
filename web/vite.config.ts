import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// Repo root sits one level up from web/. We allow Vite's dev fs to read
// it so the mothership add-server wizard can ?raw-import the canonical
// chat-agent-prompt template that lives under scripts/install/ alongside
// install.sh and bootstrap-claude-instructions.md (single source of
// truth, per the T-0044 F-7 resolution — the wizard renders the prompt
// inline; the mothership BE does not serve a /prompt.txt endpoint).
const repoRoot = fileURLToPath(new URL("..", import.meta.url));

export default defineConfig({
  plugins: [react()],
  server: {
    fs: {
      // Dev-server only — production builds resolve via Rollup, which
      // is not bound by this allowlist.
      allow: [".", repoRoot],
    },
    proxy: {
      "/api": "http://localhost:8000"
    }
  },
  build: {
    outDir: "dist"
  }
});
