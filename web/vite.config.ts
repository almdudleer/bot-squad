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

// Alias for the canonical install bundle. Production builds run through
// Rollup which won't resolve a `../../../scripts/install/...` path from
// web/ (T-0050) — the alias turns it into a stable virtual prefix that
// resolves to the same physical directory in both local and docker
// contexts (docker stages it at /scripts/install/ via api/Dockerfile).
const installBundleDir = fileURLToPath(
  new URL("../scripts/install", import.meta.url),
);

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@install": installBundleDir,
    },
  },
  server: {
    fs: {
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
