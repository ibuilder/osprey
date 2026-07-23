// Rasterize the Osprey brand SVG into the icon set Tauri needs.
// Usage: node scripts/gen-icons.mjs
import { existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const here = dirname(fileURLToPath(import.meta.url));
const svgPath = resolve(here, "../../../osprey-brand/logo/osprey-icon.svg");
const iconsDir = resolve(here, "../src-tauri/icons");
const srcDir = resolve(here, "../src-tauri/icons-src");
for (const d of [iconsDir, srcDir]) if (!existsSync(d)) mkdirSync(d, { recursive: true });

async function png(size, out) {
  await sharp(svgPath, { density: 384 })
    .resize(size, size, { fit: "contain", background: { r: 0, g: 0, b: 0, alpha: 0 } })
    .png()
    .toFile(out);
  console.log("wrote", out);
}

// 1024 source (feed to `tauri icon` for the full platform set) + a few direct sizes.
await png(1024, resolve(srcDir, "icon.png"));
await png(32, resolve(iconsDir, "32x32.png"));
await png(128, resolve(iconsDir, "128x128.png"));
await png(256, resolve(iconsDir, "128x128@2x.png"));
await png(128, resolve(iconsDir, "tray.png"));
console.log("Done. Run `npx tauri icon src-tauri/icons-src/icon.png` for .ico/.icns + Windows Store tiles.");
