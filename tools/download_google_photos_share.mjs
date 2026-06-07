import fs from "node:fs";
import path from "node:path";

const [albumUrl, outputDir] = process.argv.slice(2);
if (!albumUrl || !outputDir) {
  console.error("Usage: node tools/download_google_photos_share.mjs <album-url> <output-dir>");
  process.exit(2);
}

const USER_AGENT = "Mozilla/5.0";
const pageResponse = await fetch(albumUrl, {
  redirect: "follow",
  headers: { "User-Agent": USER_AGENT }
});
if (!pageResponse.ok) {
  throw new Error(`Album page failed: HTTP ${pageResponse.status}`);
}

const html = await pageResponse.text();
const re = /\["(AF1Qip[^"\\]+)",\["(https:\/\/lh3\.googleusercontent\.com\/pw\/[^"\\=]+)",(\d+),(\d+)[\s\S]*?\],(\d{13})/g;
const byId = new Map();
let match;
while ((match = re.exec(html))) {
  const [, id, url, width, height, timestamp] = match;
  if (!byId.has(id)) {
    byId.set(id, {
      id,
      url,
      width: Number(width),
      height: Number(height),
      timestamp: Number(timestamp)
    });
  }
}

const items = [...byId.values()].sort((a, b) => a.timestamp - b.timestamp || a.id.localeCompare(b.id));
if (items.length === 0) throw new Error("No Google Photos image entries found.");

fs.rmSync(outputDir, { recursive: true, force: true });
fs.mkdirSync(outputDir, { recursive: true });

function extensionFromType(type) {
  if (type?.includes("png")) return ".png";
  if (type?.includes("webp")) return ".webp";
  if (type?.includes("heic")) return ".heic";
  return ".jpg";
}

function sanitizeName(value) {
  return value.replace(/[/\\?%*:|"<>]/g, "_").replace(/\s+/g, " ").trim();
}

function contentDispositionFilename(value) {
  const match = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(value ?? "");
  return match ? decodeURIComponent(match[1].replace(/^"|"$/g, "")) : null;
}

async function download(item, index) {
  const response = await fetch(`${item.url}=d`, {
    redirect: "follow",
    headers: { "User-Agent": USER_AGENT }
  });
  if (!response.ok) throw new Error(`Download ${item.id} failed: HTTP ${response.status}`);
  const buffer = Buffer.from(await response.arrayBuffer());
  const dispositionName = contentDispositionFilename(response.headers.get("content-disposition"));
  const ext = path.extname(dispositionName ?? "") || extensionFromType(response.headers.get("content-type"));
  const baseName = dispositionName ? path.basename(dispositionName, path.extname(dispositionName)) : item.id;
  const stamp = new Date(item.timestamp).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  const fileName = `${String(index + 1).padStart(3, "0")}_${stamp}_${sanitizeName(baseName)}${ext.toLowerCase()}`;
  const filePath = path.join(outputDir, fileName);
  fs.writeFileSync(filePath, buffer);
  const seconds = item.timestamp / 1000;
  fs.utimesSync(filePath, seconds, seconds);
  return {
    ...item,
    fileName,
    bytes: buffer.length,
    contentType: response.headers.get("content-type") ?? null
  };
}

const results = [];
let nextIndex = 0;
const workers = Array.from({ length: 8 }, async () => {
  while (nextIndex < items.length) {
    const index = nextIndex++;
    const item = items[index];
    const result = await download(item, index);
    results[index] = result;
    console.log(`${String(index + 1).padStart(3, "0")}/${items.length} ${result.fileName} ${result.bytes}`);
  }
});
await Promise.all(workers);

fs.writeFileSync(
  path.join(outputDir, "manifest.json"),
  JSON.stringify({
    albumUrl,
    downloadedAt: new Date().toISOString(),
    count: results.length,
    photos: results
  }, null, 2)
);

console.log(`Downloaded ${results.length} photos to ${outputDir}`);
