/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * "1" on the botsquad.dev mothership build — mounts the centralization
   * layer (/m/* routes, cross-server picker swap). Unset or "0" on a
   * single-install build. Vite inlines this at build time, so the guarded
   * dynamic imports are tree-shaken when off. Frozen by
   * docs/architecture/D-0017-mothership-seam.md.
   */
  readonly VITE_MOTHERSHIP?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// `?raw` imports for plain-text assets (e.g. the chat-agent install prompt
// the mothership wizard renders inline). Vite ships the loader; this
// declaration just teaches TS that the suffixed path resolves to a string.
declare module "*.txt?raw" {
  const content: string;
  export default content;
}
