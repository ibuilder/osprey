/// <reference types="vite/client" />

// Declares Vite's ambient module types — `*.css`, `*.svg`, `?raw`/`?url` imports,
// and `import.meta.env`. tsconfig.json sets an explicit `types` array (for vitest
// globals and jest-dom matchers), which suppresses automatic inclusion of
// vite/client, so it has to be referenced here.
//
// TypeScript 5 tolerated the side-effect `import "./styles.css"` in main.tsx
// without any declaration. TypeScript 7 does not:
//
//   error TS2882: Cannot find module or type declarations for side-effect import
//   of './styles.css'.
