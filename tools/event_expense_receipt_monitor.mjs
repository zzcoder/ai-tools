#!/usr/bin/env node
import { execFile, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  closeSync,
  mkdtempSync,
  mkdirSync,
  openSync,
  readFileSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { appendFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

function envValue(name, legacyName, fallback = "") {
  return process.env[name] ?? (legacyName ? process.env[legacyName] : undefined) ?? fallback;
}

function envFlag(name, legacyName, fallback = true) {
  const raw = process.env[name] ?? (legacyName ? process.env[legacyName] : undefined);
  if (raw === undefined) return fallback;
  return raw !== "0" && !/^false$/i.test(raw);
}

function slugify(value) {
  return (
    String(value || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 80) || "event-expense"
  );
}

function parseCsv(value) {
  return String(value || "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function gmailQueryString(value) {
  return `"${String(value || "").replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

function escapeRegExp(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function spreadsheetUrlForId(spreadsheetId) {
  return spreadsheetId ? `https://docs.google.com/spreadsheets/d/${spreadsheetId}/edit` : "";
}

function workflowDirForSlug(eventSlug) {
  return `/home/zhihongz/.openclaw/workflows/${eventSlug}-receipts`;
}

const DEFAULT_EVENT_NAME = envValue("EVENT_EXPENSE_EVENT_NAME", "LAKE_ANNA_EVENT_NAME", "Lake Anna");
const DEFAULT_EVENT_SLUG = envValue(
  "EVENT_EXPENSE_EVENT_SLUG",
  "LAKE_ANNA_EVENT_SLUG",
  slugify(DEFAULT_EVENT_NAME),
);
const DEFAULT_GMAIL_SUBJECT = envValue(
  "EVENT_EXPENSE_GMAIL_SUBJECT",
  "LAKE_ANNA_GMAIL_SUBJECT",
  DEFAULT_EVENT_NAME,
);
const DEFAULT_IGNORED_SENDERS = parseCsv(
  envValue(
    "EVENT_EXPENSE_IGNORED_SENDERS",
    "LAKE_ANNA_IGNORED_SENDERS",
    "zhihong@gmail.com",
  ),
);
const DEFAULT_WORKFLOW_DIR = workflowDirForSlug(DEFAULT_EVENT_SLUG);
const DEFAULT_SPREADSHEET_ID =
  envValue("EVENT_EXPENSE_SPREADSHEET_ID", "LAKE_ANNA_SPREADSHEET_ID") ||
  "1yq0sOkqPjkfajg6ByzT7phifX4SxLhI4i5GVtEIbXds";

function defaultGmailQuery(subject, ignoredSenders) {
  return [
    subject ? `subject:${gmailQueryString(subject)}` : "",
    ...ignoredSenders.map((sender) => `-from:${sender}`),
    "-in:sent",
  ]
    .filter(Boolean)
    .join(" ");
}

const CONFIG = {
  workflowName: envValue("EVENT_EXPENSE_WORKFLOW_NAME", "LAKE_ANNA_WORKFLOW_NAME", "Event Expense"),
  eventName: DEFAULT_EVENT_NAME,
  eventSlug: DEFAULT_EVENT_SLUG,
  gmailSubject: DEFAULT_GMAIL_SUBJECT,
  gmailSubjectRegex: envValue("EVENT_EXPENSE_GMAIL_SUBJECT_REGEX", "LAKE_ANNA_GMAIL_SUBJECT_REGEX", ""),
  ignoredSenderEmails: DEFAULT_IGNORED_SENDERS,
  spreadsheetTitle: envValue(
    "EVENT_EXPENSE_SPREADSHEET_TITLE",
    "LAKE_ANNA_SPREADSHEET_TITLE",
    DEFAULT_EVENT_NAME,
  ),
  account: envValue("EVENT_EXPENSE_GMAIL_ACCOUNT", "LAKE_ANNA_GMAIL_ACCOUNT", "zhihong@gmail.com"),
  query:
    envValue("EVENT_EXPENSE_GMAIL_QUERY", "LAKE_ANNA_GMAIL_QUERY") ||
    defaultGmailQuery(DEFAULT_GMAIL_SUBJECT, DEFAULT_IGNORED_SENDERS),
  spreadsheetId:
    DEFAULT_SPREADSHEET_ID,
  spreadsheetUrl:
    envValue("EVENT_EXPENSE_SPREADSHEET_URL", "LAKE_ANNA_SPREADSHEET_URL") ||
    spreadsheetUrlForId(DEFAULT_SPREADSHEET_ID),
  sheetRange: envValue("EVENT_EXPENSE_RECEIPTS_RANGE", "LAKE_ANNA_RECEIPTS_RANGE", "Receipts!A:M"),
  replyEnabled: envFlag("EVENT_EXPENSE_REPLY_ENABLED", "LAKE_ANNA_REPLY_ENABLED", true),
  ocrEnabled: envFlag("EVENT_EXPENSE_OCR_ENABLED", "LAKE_ANNA_OCR_ENABLED", true),
  shareUploadedReceipts: envFlag("EVENT_EXPENSE_SHARE_RECEIPTS", "LAKE_ANNA_SHARE_RECEIPTS", true),
  bodyModelProvider: envValue("EVENT_EXPENSE_BODY_MODEL_PROVIDER", "LAKE_ANNA_BODY_MODEL_PROVIDER", ""),
  bodyModel: envValue("EVENT_EXPENSE_BODY_MODEL", "LAKE_ANNA_BODY_MODEL", ""),
  bodyModelReasoning: envValue(
    "EVENT_EXPENSE_BODY_MODEL_REASONING",
    "LAKE_ANNA_BODY_MODEL_REASONING",
    "low",
  ),
  bodyModelTimeoutMs: Number(
    envValue("EVENT_EXPENSE_BODY_MODEL_TIMEOUT_MS", "LAKE_ANNA_BODY_MODEL_TIMEOUT_MS", "120000"),
  ),
  bodyModelUrl: envValue(
    "EVENT_EXPENSE_BODY_MODEL_URL",
    "LAKE_ANNA_BODY_MODEL_URL",
    "http://127.0.0.1:11434/api/generate",
  ),
  driveFolderId:
    envValue("EVENT_EXPENSE_RECEIPTS_FOLDER_ID", "LAKE_ANNA_RECEIPTS_FOLDER_ID") ||
    "1aQ8wHsQijk_iydrD7W3tTX3f9h6w43Zh",
  gogBin: process.env.GOG_BIN || "/home/zhihongz/.openclaw/bin/gog",
  codexBin: envValue("EVENT_EXPENSE_CODEX_BIN", "LAKE_ANNA_CODEX_BIN", "/usr/bin/codex"),
  stateFile:
    envValue("EVENT_EXPENSE_STATE", "LAKE_ANNA_RECEIPTS_STATE") ||
    `${DEFAULT_WORKFLOW_DIR}/state.json`,
  downloadDir:
    envValue("EVENT_EXPENSE_DOWNLOAD_DIR", "LAKE_ANNA_RECEIPTS_DOWNLOAD_DIR") ||
    `${DEFAULT_WORKFLOW_DIR}/downloads`,
  logFile:
    envValue("EVENT_EXPENSE_LOG", "LAKE_ANNA_RECEIPTS_LOG") ||
    `${DEFAULT_WORKFLOW_DIR}/monitor.log`,
};

const DEFAULT_STATE = {
  version: 1,
  attachments: {},
  emailPdfs: {},
  typedAmounts: {},
  receiptExpenses: {},
  replies: {},
  skippedMessages: {},
  updatedAt: null,
};

const RECEIPT_MIME_TYPES = new Set([
  "application/pdf",
  "image/jpeg",
  "image/png",
  "image/heic",
  "image/heif",
]);

const RECEIPT_EXTENSIONS = new Set([
  ".pdf",
  ".jpg",
  ".jpeg",
  ".png",
  ".heic",
  ".heif",
]);

const RECEIPT_EXPENSE_LABEL_MAX_LENGTH = 40;
const RUN_LOCK_STALE_MS = 30 * 60 * 1000;

function parseArgs(argv) {
  let queryExplicit = Boolean(envValue("EVENT_EXPENSE_GMAIL_QUERY", "LAKE_ANNA_GMAIL_QUERY"));
  let subjectExplicit = Boolean(envValue("EVENT_EXPENSE_GMAIL_SUBJECT", "LAKE_ANNA_GMAIL_SUBJECT"));
  let slugExplicit = Boolean(envValue("EVENT_EXPENSE_EVENT_SLUG", "LAKE_ANNA_EVENT_SLUG"));
  let titleExplicit = Boolean(envValue("EVENT_EXPENSE_SPREADSHEET_TITLE", "LAKE_ANNA_SPREADSHEET_TITLE"));
  let stateExplicit = Boolean(envValue("EVENT_EXPENSE_STATE", "LAKE_ANNA_RECEIPTS_STATE"));
  let downloadDirExplicit = Boolean(envValue("EVENT_EXPENSE_DOWNLOAD_DIR", "LAKE_ANNA_RECEIPTS_DOWNLOAD_DIR"));
  let logFileExplicit = Boolean(envValue("EVENT_EXPENSE_LOG", "LAKE_ANNA_RECEIPTS_LOG"));
  const args = {
    dryRun: false,
    noReply: false,
    setupCheck: false,
    max: Number(envValue("EVENT_EXPENSE_MAX", "LAKE_ANNA_RECEIPTS_MAX", "50")),
  };

  for (const arg of argv) {
    if (arg === "--dry-run") args.dryRun = true;
    else if (arg === "--no-reply") args.noReply = true;
    else if (arg === "--setup-check") args.setupCheck = true;
    else if (arg.startsWith("--max=")) args.max = Number(arg.slice("--max=".length));
    else if (arg.startsWith("--query=")) {
      CONFIG.query = arg.slice("--query=".length);
      queryExplicit = true;
    } else if (arg.startsWith("--event-name=")) {
      CONFIG.eventName = arg.slice("--event-name=".length);
      if (!slugExplicit) CONFIG.eventSlug = slugify(CONFIG.eventName);
      if (!subjectExplicit) CONFIG.gmailSubject = CONFIG.eventName;
      if (!titleExplicit) CONFIG.spreadsheetTitle = CONFIG.eventName;
      const generatedWorkflowDir = workflowDirForSlug(CONFIG.eventSlug);
      if (!stateExplicit) CONFIG.stateFile = `${generatedWorkflowDir}/state.json`;
      if (!downloadDirExplicit) CONFIG.downloadDir = `${generatedWorkflowDir}/downloads`;
      if (!logFileExplicit) CONFIG.logFile = `${generatedWorkflowDir}/monitor.log`;
      if (!queryExplicit) {
        CONFIG.query = defaultGmailQuery(CONFIG.gmailSubject, CONFIG.ignoredSenderEmails);
      }
    } else if (arg.startsWith("--event-slug=")) {
      CONFIG.eventSlug = arg.slice("--event-slug=".length);
      slugExplicit = true;
      const generatedWorkflowDir = workflowDirForSlug(CONFIG.eventSlug);
      if (!stateExplicit) CONFIG.stateFile = `${generatedWorkflowDir}/state.json`;
      if (!downloadDirExplicit) CONFIG.downloadDir = `${generatedWorkflowDir}/downloads`;
      if (!logFileExplicit) CONFIG.logFile = `${generatedWorkflowDir}/monitor.log`;
    } else if (arg.startsWith("--subject=")) {
      CONFIG.gmailSubject = arg.slice("--subject=".length);
      subjectExplicit = true;
      if (!queryExplicit) {
        CONFIG.query = defaultGmailQuery(CONFIG.gmailSubject, CONFIG.ignoredSenderEmails);
      }
    } else if (arg.startsWith("--subject-regex=")) {
      CONFIG.gmailSubjectRegex = arg.slice("--subject-regex=".length);
    } else if (arg.startsWith("--spreadsheet-title=")) {
      CONFIG.spreadsheetTitle = arg.slice("--spreadsheet-title=".length);
      titleExplicit = true;
    } else if (arg.startsWith("--spreadsheet-id=")) {
      CONFIG.spreadsheetId = arg.slice("--spreadsheet-id=".length);
      CONFIG.spreadsheetUrl = spreadsheetUrlForId(CONFIG.spreadsheetId);
    } else if (arg.startsWith("--spreadsheet-url=")) {
      CONFIG.spreadsheetUrl = arg.slice("--spreadsheet-url=".length);
    } else if (arg.startsWith("--drive-folder-id=")) {
      CONFIG.driveFolderId = arg.slice("--drive-folder-id=".length);
    } else if (arg.startsWith("--state-file=")) {
      CONFIG.stateFile = arg.slice("--state-file=".length);
      stateExplicit = true;
    } else if (arg.startsWith("--download-dir=")) {
      CONFIG.downloadDir = arg.slice("--download-dir=".length);
      downloadDirExplicit = true;
    } else if (arg.startsWith("--log-file=")) {
      CONFIG.logFile = arg.slice("--log-file=".length);
      logFileExplicit = true;
    } else if (arg.startsWith("--ignored-senders=")) {
      CONFIG.ignoredSenderEmails = parseCsv(arg.slice("--ignored-senders=".length));
      if (!queryExplicit) {
        CONFIG.query = defaultGmailQuery(CONFIG.gmailSubject, CONFIG.ignoredSenderEmails);
      }
    } else if (arg === "--help" || arg === "-h") {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!Number.isFinite(args.max) || args.max < 1) {
    throw new Error("--max must be a positive number");
  }

  return args;
}

function printHelp() {
  console.log(`${CONFIG.workflowName} receipt monitor

Search Gmail for event expense receipts, upload receipt attachments to the
configured Drive folder, extract typed dollar amounts from the message body,
OCR receipt attachments for totals, and append rows to the configured
spreadsheet.

Current event:
  Event name: ${CONFIG.eventName}
  Event slug: ${CONFIG.eventSlug}
  Gmail subject: ${CONFIG.gmailSubject}
  Spreadsheet: ${CONFIG.spreadsheetTitle}

Options:
  --dry-run       Search and report intended work without uploading or writing
  --no-reply      Process receipts without replying to matching emails
  --setup-check   Validate local paths and configuration without Gmail access
  --max=N         Maximum Gmail search results to inspect, default 50
  --query=QUERY   Override the Gmail query

Event parameters:
  --event-name=NAME
  --spreadsheet-id=ID
  --drive-folder-id=ID

Generated from event name or IDs:
  Event slug, Gmail subject, Gmail query, spreadsheet URL, workflow state path,
  download path, and log path.

Advanced overrides:
  --subject=TEXT
  --subject-regex=REGEX
  --ignored-senders=email1,email2
  --spreadsheet-title=TITLE
  --spreadsheet-url=URL
  --state-file=PATH
  --download-dir=PATH
  --log-file=PATH

Environment variables use EVENT_EXPENSE_* names. Existing LAKE_ANNA_* names
are still accepted for backward compatibility.

Body parsing:
  EVENT_EXPENSE_BODY_MODEL_PROVIDER=codex|ollama
  EVENT_EXPENSE_BODY_MODEL=codex/gpt-5.4-mini or qwen2.5:7b
  EVENT_EXPENSE_BODY_MODEL_REASONING=low|medium|high|xhigh for Codex`);
}

async function log(message) {
  const line = `[${new Date().toISOString()}] ${message}`;
  console.log(line);
  try {
    mkdirSync(path.dirname(CONFIG.logFile), { recursive: true });
    await appendFile(CONFIG.logFile, `${line}\n`, "utf8");
  } catch {
    // Keep stdout logging working even if the sidecar log is unavailable.
  }
}

function readState() {
  try {
    return {
      ...DEFAULT_STATE,
      ...JSON.parse(readFileSync(CONFIG.stateFile, "utf8")),
    };
  } catch (error) {
    if (error.code === "ENOENT") return structuredClone(DEFAULT_STATE);
    throw error;
  }
}

function writeState(state) {
  state.updatedAt = new Date().toISOString();
  mkdirSync(path.dirname(CONFIG.stateFile), { recursive: true });
  writeFileSync(CONFIG.stateFile, `${JSON.stringify(state, null, 2)}\n`, "utf8");
}

function createRunLock() {
  const lockFile = `${CONFIG.stateFile}.lock`;
  mkdirSync(path.dirname(lockFile), { recursive: true });

  const openLock = () => {
    const fd = openSync(lockFile, "wx");
    writeFileSync(
      fd,
      `${JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() })}\n`,
      "utf8",
    );
    return fd;
  };

  let fd;
  try {
    fd = openLock();
  } catch (error) {
    if (error.code !== "EEXIST") throw error;

    try {
      const ageMs = Date.now() - statSync(lockFile).mtimeMs;
      if (ageMs > RUN_LOCK_STALE_MS) {
        unlinkSync(lockFile);
        fd = openLock();
      } else {
        return null;
      }
    } catch (staleError) {
      if (staleError.code === "ENOENT") {
        fd = openLock();
      } else {
        throw staleError;
      }
    }
  }

  let released = false;
  return () => {
    if (released) return;
    released = true;
    try {
      closeSync(fd);
    } catch {
      // The process is exiting; best effort cleanup is enough here.
    }
    try {
      unlinkSync(lockFile);
    } catch {
      // A stale lock will be cleared on a later run.
    }
  };
}

async function runGog(args, options = {}) {
  const { stdout, stderr } = await execFileAsync(CONFIG.gogBin, args, {
    encoding: "utf8",
    maxBuffer: 20 * 1024 * 1024,
    env: {
      ...process.env,
      HOME: process.env.HOME || "/home/zhihongz",
    },
    ...options,
  });

  return { stdout, stderr };
}

async function runGogJson(args) {
  try {
    const { stdout } = await runGog([...args, "-j", "--no-input"]);
    const trimmed = stdout.trim();
    return trimmed ? JSON.parse(trimmed) : null;
  } catch (error) {
    const stderr = error.stderr || error.cause?.stderr || "";
    const stdout = error.stdout || error.cause?.stdout || "";
    const details = [stderr.trim(), stdout.trim()].filter(Boolean).join("\n");
    if (/invalid_grant|expired|revoked|no TTY available|keyring/i.test(details)) {
      throw new Error(
        `Gog authentication failed. Reconnect ${CONFIG.account} for Gmail, Drive, and Sheets, then rerun this monitor.\n${details}`,
      );
    }
    throw error;
  }
}

function runCommandWithInput(file, args, input, options = {}) {
  return new Promise((resolve, reject) => {
    const maxBuffer = options.maxBuffer || 20 * 1024 * 1024;
    const child = spawn(file, args, {
      cwd: options.cwd || process.cwd(),
      env: options.env || process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";
    let settled = false;
    const fail = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      error.stdout = stdout;
      error.stderr = stderr;
      reject(error);
    };
    const timer = options.timeout
      ? setTimeout(() => {
          child.kill("SIGTERM");
          fail(new Error(`Command timed out after ${options.timeout}ms: ${file}`));
        }, options.timeout)
      : null;

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
      if (stdout.length + stderr.length > maxBuffer) {
        child.kill("SIGTERM");
        fail(new Error(`Command output exceeded ${maxBuffer} bytes: ${file}`));
      }
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
      if (stdout.length + stderr.length > maxBuffer) {
        child.kill("SIGTERM");
        fail(new Error(`Command output exceeded ${maxBuffer} bytes: ${file}`));
      }
    });
    child.on("error", fail);
    child.on("close", (code, signal) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code === 0) {
        resolve({ stdout, stderr });
        return;
      }
      const error = new Error(`Command failed with ${signal || `exit code ${code}`}: ${file}`);
      error.stdout = stdout;
      error.stderr = stderr;
      reject(error);
    });

    child.stdin.end(input);
  });
}

function asArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function unwrapCollection(value) {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== "object") return [];

  for (const key of ["messages", "items", "results", "data", "result"]) {
    const candidate = value[key];
    if (Array.isArray(candidate)) return candidate;
    if (candidate && typeof candidate === "object") {
      const nested = unwrapCollection(candidate);
      if (nested.length) return nested;
    }
  }

  return [];
}

function unwrapMessage(value) {
  if (value?.payload) return value;
  for (const key of ["message", "result", "data"]) {
    if (value?.[key]?.payload) return value[key];
  }
  return value;
}

function unwrapDriveFile(value) {
  if (value?.id) return value;
  for (const key of ["file", "result", "data"]) {
    if (value?.[key]?.id) return value[key];
  }
  const files = unwrapCollection(value);
  return files.find((item) => item?.id) || {};
}

function collectMessageIds(searchResult) {
  const ids = new Set();
  for (const item of unwrapCollection(searchResult)) {
    if (typeof item === "string") ids.add(item);
    if (item?.id) ids.add(item.id);
    if (item?.messageId) ids.add(item.messageId);
    for (const nested of asArray(item?.messages)) {
      if (nested?.id) ids.add(nested.id);
    }
  }
  return [...ids];
}

function headersFrom(message) {
  const headers = message?.payload?.headers || [];
  const normalized = new Map();
  for (const header of headers) {
    if (header?.name) normalized.set(header.name.toLowerCase(), header.value || "");
  }
  return {
    get(name) {
      return normalized.get(name.toLowerCase()) || "";
    },
  };
}

function collectAttachments(part, output = []) {
  if (!part) return output;

  if (part.filename && part.body?.attachmentId) {
    output.push({
      index: output.length,
      filename: part.filename,
      attachmentId: part.body.attachmentId,
      mimeType: part.mimeType || "",
      size: part.body.size || "",
      contentDisposition: partHeader(part, "content-disposition"),
    });
  }

  for (const child of part.parts || []) {
    collectAttachments(child, output);
  }

  return output;
}

function partHeader(part, name) {
  const header = (part.headers || []).find(
    (candidate) => candidate?.name?.toLowerCase() === name.toLowerCase(),
  );
  return header?.value || "";
}

function isReceiptAttachment(attachment) {
  const extension = path.extname(attachment.filename || "").toLowerCase();
  const mimeType = (attachment.mimeType || "").toLowerCase();
  const isSmallInlineImage =
    mimeType.startsWith("image/") &&
    /inline/i.test(attachment.contentDisposition || "") &&
    Number(attachment.size || 0) < 50_000;

  if (isSmallInlineImage) return false;
  return RECEIPT_EXTENSIONS.has(extension) || RECEIPT_MIME_TYPES.has(mimeType);
}

function sanitizeFilename(filename) {
  const cleaned = String(filename || "receipt")
    .replace(/[/\\?%*:|"<>]/g, "-")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned.slice(0, 160) || "receipt";
}

function shortHash(value) {
  return String(value || "")
    .replace(/[^a-zA-Z0-9]/g, "")
    .slice(-10);
}

function digest(value) {
  return createHash("sha256").update(String(value || "")).digest("hex").slice(0, 16);
}

function stableAttachmentKey(message, attachment) {
  const parts = [
    message.id || "",
    attachment.index ?? "",
    sanitizeFilename(attachment.filename).toLowerCase(),
    String(attachment.mimeType || "").toLowerCase(),
    String(attachment.size || ""),
  ];
  return `${message.id}:attachment:${digest(parts.join("\0"))}`;
}

function attachmentMatches(candidate, message, attachment) {
  if (!candidate || candidate.messageId !== message.id) return false;
  const candidateName =
    candidate.attachmentName ||
    candidate.filename ||
    candidate.row?.[5] ||
    "";
  const candidateMime = candidate.mimeType || candidate.row?.[8] || "";
  const candidateSize = candidate.size || candidate.row?.[9] || "";
  return (
    sanitizeFilename(candidateName).toLowerCase() ===
      sanitizeFilename(attachment.filename).toLowerCase() &&
    String(candidateMime).toLowerCase() === String(attachment.mimeType || "").toLowerCase() &&
    String(candidateSize || "") === String(attachment.size || "")
  );
}

function findExistingAttachment(state, key, message, attachment) {
  if (state.attachments[key]) return { key, value: state.attachments[key] };
  for (const [candidateKey, candidate] of Object.entries(state.attachments || {})) {
    if (attachmentMatches(candidate, message, attachment)) {
      return { key: candidateKey, value: candidate };
    }
  }
  return { key, value: null };
}

function dateForFilename(message, fallbackDateHeader) {
  const internalDate = Number(message?.internalDate);
  const date = Number.isFinite(internalDate)
    ? new Date(internalDate)
    : new Date(fallbackDateHeader || Date.now());
  if (Number.isNaN(date.getTime())) return new Date().toISOString().slice(0, 10);
  return date.toISOString().slice(0, 10);
}

function isoEmailDate(message, fallbackDateHeader) {
  const internalDate = Number(message?.internalDate);
  const date = Number.isFinite(internalDate)
    ? new Date(internalDate)
    : new Date(fallbackDateHeader || Date.now());
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

async function downloadAttachment(messageId, attachment, uploadName) {
  const localPath = path.join(CONFIG.downloadDir, uploadName);
  await runGog([
    "gmail",
    "attachment",
    messageId,
    attachment.attachmentId,
    "--account",
    CONFIG.account,
    "--out",
    CONFIG.downloadDir,
    "--name",
    uploadName,
    "--no-input",
  ]);
  return localPath;
}

async function uploadToDrive(localPath, uploadName) {
  const result = await runGogJson([
    "drive",
    "upload",
    localPath,
    "--account",
    CONFIG.account,
    "--parent",
    CONFIG.driveFolderId,
    "--name",
    uploadName,
  ]);
  const file = unwrapDriveFile(result);
  const sharedLink = await shareDriveFile(file.id, uploadName);
  const webViewLink =
    sharedLink ||
    file.webViewLink || (file.id ? `https://drive.google.com/file/d/${file.id}/view` : "");
  return {
    id: file.id || "",
    webViewLink,
    name: file.name || uploadName,
  };
}

async function shareDriveFile(fileId, label) {
  if (!CONFIG.shareUploadedReceipts || !fileId) return "";
  try {
    const result = await runGogJson([
      "drive",
      "share",
      fileId,
      "--account",
      CONFIG.account,
      "--to",
      "anyone",
      "--role",
      "reader",
      "--force",
    ]);
    return result?.link || "";
  } catch (error) {
    await log(`Could not make uploaded receipt shareable (${label || fileId}): ${error.message}`);
    return "";
  }
}

async function appendReceiptRow(row) {
  await runGogJson([
    "sheets",
    "append",
    CONFIG.spreadsheetId,
    CONFIG.sheetRange,
    "--account",
    CONFIG.account,
    "--input=USER_ENTERED",
    "--values-json",
    JSON.stringify([row]),
  ]);
}

async function appendSummaryRow(row) {
  const rows = await getSheetValues("Expenses!A1:Z1000");
  const nextRow = Math.max(1, rows.length + 1);
  const normalized = [...row];
  while (normalized.length < 5) normalized.push("");
  await runGogJson([
    "sheets",
    "update",
    CONFIG.spreadsheetId,
    `Expenses!A${nextRow}:E${nextRow}`,
    "--account",
    CONFIG.account,
    "--input=USER_ENTERED",
    "--values-json",
    JSON.stringify([normalized.slice(0, 5)]),
  ]);
}

async function getSheetValues(range) {
  const result = await runGogJson([
    "sheets",
    "get",
    CONFIG.spreadsheetId,
    range,
    "--account",
    CONFIG.account,
    "--render=FORMATTED_VALUE",
  ]);
  return Array.isArray(result?.values) ? result.values : [];
}

let spreadsheetTitleCache = "";

async function getSpreadsheetTitle() {
  if (spreadsheetTitleCache) return spreadsheetTitleCache;
  try {
    const result = await runGogJson([
      "sheets",
      "metadata",
      CONFIG.spreadsheetId,
      "--account",
      CONFIG.account,
    ]);
    spreadsheetTitleCache = result?.title || CONFIG.spreadsheetTitle || CONFIG.eventName;
  } catch {
    spreadsheetTitleCache = CONFIG.spreadsheetTitle || CONFIG.eventName;
  }
  return spreadsheetTitleCache;
}

function compactRows(rows) {
  const width = Math.max(0, ...rows.map((row) => row.length));
  const keepIndexes = [];
  for (let index = 0; index < width; index += 1) {
    if (rows.some((row) => String(row[index] || "").trim())) {
      keepIndexes.push(index);
    }
  }

  return rows
    .filter((row) => row.some((cell) => String(cell || "").trim()))
    .map((row) => keepIndexes.map((index) => String(row[index] || "")));
}

function formatTable(title, rows) {
  const compacted = compactRows(rows);
  if (!compacted.length) return `${title}\n(no rows)`;

  const widths = [];
  for (const row of compacted) {
    row.forEach((cell, index) => {
      widths[index] = Math.min(
        48,
        Math.max(widths[index] || 0, String(cell || "").length),
      );
    });
  }

  const renderRow = (row) =>
    row
      .map((cell, index) => {
        const text = String(cell || "");
        const clipped = text.length > 48 ? `${text.slice(0, 45)}...` : text;
        return clipped.padEnd(widths[index] || 1, " ");
      })
      .join(" | ")
      .trimEnd();

  const lines = compacted.slice(0, 100).map(renderRow);
  if (compacted.length > 100) {
    lines.push(`... ${compacted.length - 100} more row(s)`);
  }

  return `${title}\n${lines.join("\n")}`;
}

async function buildSpreadsheetSnapshot() {
  const spreadsheetTitle = await getSpreadsheetTitle();
  const [summaryRows, receiptRows] = await Promise.all([
    getSheetValues("Expenses!A1:Z100"),
    getSheetValues("Receipts!A1:M100"),
  ]);

  return [
    `${spreadsheetTitle} spreadsheet`,
    CONFIG.spreadsheetUrl,
    "",
    formatTable("Expenses", summaryRows),
    "",
    formatTable("Receipts", receiptRows),
  ].join("\n");
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function wrapText(value, maxChars, maxLines) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return [""];

  const words = text.split(" ");
  const lines = [];
  let current = "";
  for (const word of words) {
    if (word.length > maxChars) {
      if (current) {
        lines.push(current);
        current = "";
      }
      for (let index = 0; index < word.length; index += maxChars) {
        lines.push(word.slice(index, index + maxChars));
      }
      continue;
    }
    const next = current ? `${current} ${word}` : word;
    if (next.length > maxChars) {
      lines.push(current);
      current = word;
    } else {
      current = next;
    }
  }
  if (current) lines.push(current);

  const clipped = lines.slice(0, maxLines);
  if (lines.length > maxLines && clipped.length) {
    clipped[clipped.length - 1] = `${clipped[clipped.length - 1].slice(0, Math.max(0, maxChars - 3))}...`;
  }
  return clipped.length ? clipped : [""];
}

function normalizeExpenseSnapshotRows(rows) {
  const compacted = rows.filter((row) => row.some((cell) => String(cell || "").trim()));
  if (!compacted.length) return [["Item", "Amount", "Payer", "Receipt", "Comments"]];
  const header = [...compacted[0].slice(0, 5)];
  while (header.length < 5) header.push("");
  if (!header[3]) header[3] = "Receipt";
  const body = compacted.slice(1, 35).map((row) => {
    const normalized = [...row.slice(0, 5)];
    while (normalized.length < 5) normalized.push("");
    if (normalized[3]) normalized[3] = "Receipt link";
    return normalized;
  });
  return [header, ...body];
}

let sharpRenderer = null;

async function renderSvgToPng(svg, imagePath) {
  if (!sharpRenderer) {
    try {
      const sharpModule = await import("sharp");
      sharpRenderer = sharpModule.default || sharpModule;
    } catch (error) {
      throw new Error(
        `Cannot render Expenses snapshot PNG because the sharp package is unavailable: ${error.message}`,
      );
    }
  }

  await sharpRenderer(Buffer.from(svg, "utf8")).png().toFile(imagePath);
  const stats = statSync(imagePath);
  if (!stats.size) {
    throw new Error("Rendered Expenses snapshot PNG is empty");
  }
}

function svgTextLines({
  lines,
  x,
  y,
  width,
  lineHeight,
  anchor = "start",
  fill = "#111827",
  weight = "400",
}) {
  const textX = anchor === "end" ? x + width : x;
  return [
    `<text x="${textX}" y="${y}" text-anchor="${anchor}" fill="${fill}" font-family="Arial, Droid Sans Fallback, Noto Sans, sans-serif" font-size="14" font-weight="${weight}">`,
    ...lines.map((line, index) => (
      `<tspan x="${textX}" dy="${index === 0 ? 0 : lineHeight}">${escapeHtml(line)}</tspan>`
    )),
    "</text>",
  ].join("");
}

async function buildExpensesSnapshotImage(message) {
  const spreadsheetTitle = await getSpreadsheetTitle();
  const rows = normalizeExpenseSnapshotRows(await getSheetValues("Expenses!A1:E100"));
  const columns = [
    { width: 260, maxChars: 32, maxLines: 2, align: "start" },
    { width: 90, maxChars: 10, maxLines: 1, align: "end" },
    { width: 145, maxChars: 18, maxLines: 2, align: "start" },
    { width: 115, maxChars: 13, maxLines: 1, align: "start" },
    { width: 460, maxChars: 58, maxLines: 3, align: "start" },
  ];
  const margin = 28;
  const gap = 14;
  const tableWidth = columns.reduce((sum, column) => sum + column.width, 0);
  const width = tableWidth + margin * 2;
  const lineHeight = 18;
  const verticalPadding = 12;
  const titleHeight = 78;
  const preparedRows = rows.map((row, rowIndex) => {
    const cells = columns.map((column, columnIndex) => wrapText(row[columnIndex] || "", column.maxChars, rowIndex === 0 ? 1 : column.maxLines));
    const height = Math.max(42, Math.max(...cells.map((cellLines) => cellLines.length)) * lineHeight + verticalPadding * 2);
    return { cells, height };
  });
  const height = titleHeight + preparedRows.reduce((sum, row) => sum + row.height, 0) + margin;

  let y = titleHeight;
  const updatedAt = new Date().toLocaleString("en-US", { timeZone: "America/New_York" });
  const parts = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`,
    `<rect width="${width}" height="${height}" fill="#f8fafc"/>`,
    `<text x="${margin}" y="34" fill="#111827" font-family="Arial, Droid Sans Fallback, Noto Sans, sans-serif" font-size="24" font-weight="700">${escapeHtml(spreadsheetTitle)} Expenses</text>`,
    `<text x="${margin}" y="58" fill="#475569" font-family="Arial, Droid Sans Fallback, Noto Sans, sans-serif" font-size="13">Updated ${escapeHtml(updatedAt)}</text>`,
    `<rect x="${margin - 1}" y="${titleHeight - 1}" width="${tableWidth + 2}" height="${height - titleHeight - margin + 2}" rx="8" fill="#ffffff" stroke="#cbd5e1"/>`,
  ];

  for (const [rowIndex, row] of preparedRows.entries()) {
    const isHeader = rowIndex === 0;
    const fill = isHeader ? "#0f172a" : rowIndex % 2 === 0 ? "#ffffff" : "#f8fafc";
    parts.push(`<rect x="${margin}" y="${y}" width="${tableWidth}" height="${row.height}" fill="${fill}"/>`);
    let x = margin;
    for (const [columnIndex, column] of columns.entries()) {
      const lines = row.cells[columnIndex];
      const textY = y + verticalPadding + 14;
      parts.push(
        svgTextLines({
          lines,
          x: x + gap,
          y: textY,
          width: column.width - gap * 2,
          lineHeight,
          anchor: column.align,
          fill: isHeader ? "#ffffff" : "#111827",
          weight: isHeader ? "700" : columnIndex === 1 ? "600" : "400",
        }),
      );
      if (columnIndex < columns.length - 1) {
        parts.push(`<line x1="${x + column.width}" y1="${y}" x2="${x + column.width}" y2="${y + row.height}" stroke="#e2e8f0"/>`);
      }
      x += column.width;
    }
    parts.push(`<line x1="${margin}" y1="${y + row.height}" x2="${margin + tableWidth}" y2="${y + row.height}" stroke="#e2e8f0"/>`);
    y += row.height;
  }

  parts.push("</svg>");

  const imagePath = path.join(CONFIG.downloadDir, `expenses-snapshot-${shortHash(message.id)}.png`);
  await renderSvgToPng(`${parts.join("\n")}\n`, imagePath);
  return imagePath;
}

function extractEmailAddress(value) {
  const match = String(value || "").match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i);
  return match?.[0] || "";
}

function replySubject(subject) {
  const trimmed = String(subject || "").trim();
  return /^re:/i.test(trimmed) ? trimmed : `Re: ${trimmed || `${CONFIG.eventName} receipts`}`;
}

function handledSummaryLines({ attachments, generatedReceipts, typedAmounts }) {
  const lines = [];
  const generatedReceiptFiles = generatedReceipts.filter((receipt) => receipt?.filename);
  if (attachments.length) {
    lines.push("Receipt attachment(s) on this email:");
    lines.push(...attachments.map((attachment) => `- ${attachment.filename}`));
  }
  if (generatedReceiptFiles.length) {
    if (lines.length) lines.push("");
    lines.push("Generated receipt PDF(s):");
    lines.push(...generatedReceiptFiles.map((receipt) => `- ${receipt.filename}`));
  }
  if (typedAmounts.length) {
    if (lines.length) lines.push("");
    lines.push("Typed amount(s) found in this email:");
    lines.push(
      ...typedAmounts.map(
        (entry) => `- ${typedAmountDisplay(entry)} from: ${entry.excerpt}`,
      ),
    );
  }
  return lines.length ? lines : ["No receipt attachment or typed amount was processed."];
}

async function replyWithSpreadsheet({
  args,
  state,
  message,
  headers,
  attachments,
  generatedReceipts,
  typedAmounts,
}) {
  if (!CONFIG.replyEnabled || args.noReply) return "disabled";
  if (!attachments.length && !generatedReceipts.length && !typedAmounts.length) {
    return "nothing-processed";
  }

  const key = message.id || "";
  if (!key) return "missing-message-id";
  if (state.replies?.[key]?.status === "sent") return "already-sent";

  const to = extractEmailAddress(headers.get("reply-to")) || extractEmailAddress(headers.get("from"));
  if (!to) {
    state.replies[key] = {
      status: "skipped",
      reason: "could not determine reply recipient",
      skippedAt: new Date().toISOString(),
    };
    writeState(state);
    return "missing-recipient";
  }

  const spreadsheetTitle = await getSpreadsheetTitle();
  const snapshotImagePath = await buildExpensesSnapshotImage(message);
  const body = [
    `The ${spreadsheetTitle} spreadsheet has been updated.`,
    "",
    ...handledSummaryLines({ attachments, generatedReceipts, typedAmounts }),
    "",
    `Full spreadsheet: ${CONFIG.spreadsheetUrl}`,
    "",
    "Attached is a PNG image snapshot of the Expenses sheet.",
    "",
    `This is an automated reply from the ${CONFIG.workflowName} workflow.`,
  ].join("\n");

  if (args.dryRun) {
    await log(`Would reply to ${to} for message ${message.id} with ${path.basename(snapshotImagePath)}`);
    return "dry-run";
  }

  const bodyFile = path.join(CONFIG.downloadDir, `reply-${shortHash(message.id)}.txt`);
  writeFileSync(bodyFile, body, "utf8");

  const result = await runGogJson([
    "gmail",
    "send",
    "--account",
    CONFIG.account,
    "--to",
    to,
    "--subject",
    replySubject(headers.get("subject")),
    "--body-file",
    bodyFile,
    "--attach",
    snapshotImagePath,
    "--reply-to-message-id",
    message.id,
  ]);

  state.replies[key] = {
    status: "sent",
    sentAt: new Date().toISOString(),
    to,
    messageId: message.id,
    replyId: result?.id || result?.message?.id || "",
    snapshotImage: snapshotImagePath,
  };
  writeState(state);
  return "sent";
}

function buildRow({
  message,
  headers,
  attachment,
  driveFile,
  receiptTotal = null,
  notes = `Uploaded by ${CONFIG.workflowName} workflow`,
}) {
  const totalNote = receiptTotal
    ? `OCR total $${receiptTotal.amount.toFixed(2)} from "${receiptTotal.label}"`
    : "";
  return [
    new Date().toISOString(),
    isoEmailDate(message, headers.get("date")),
    headers.get("from"),
    headers.get("subject"),
    message.id || "",
    attachment.filename,
    driveFile.id,
    driveFile.webViewLink,
    attachment.mimeType,
    String(attachment.size || ""),
    "",
    [notes, totalNote].filter(Boolean).join("; "),
    receiptTotal ? receiptTotal.amount.toFixed(2) : "",
  ];
}

function decodeBase64Url(value) {
  if (!value) return "";
  const normalized = String(value).replace(/-/g, "+").replace(/_/g, "/");
  return Buffer.from(normalized, "base64").toString("utf8");
}

function collectBodyTextParts(part, output = []) {
  if (!part) return output;

  const data = part.body?.data;
  if (data && !part.filename) {
    output.push(decodeBase64Url(data));
  }

  for (const child of part.parts || []) {
    collectBodyTextParts(child, output);
  }

  return output;
}

function htmlToText(value) {
  return String(value || "")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n")
    .replace(/<\/div>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&#39;/gi, "'")
    .replace(/&quot;/gi, '"')
    .replace(/&#(\d+);/g, (_match, code) => String.fromCodePoint(Number(code)))
    .replace(/\r/g, "\n")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function messageBodyText(rawMessage, message) {
  if (typeof rawMessage?.body === "string" && rawMessage.body.trim()) {
    return htmlToText(rawMessage.body);
  }
  return htmlToText(collectBodyTextParts(message.payload).join("\n"));
}

function normalizeAmount(raw) {
  const amount = Number(String(raw || "").replace(/,/g, ""));
  if (!Number.isFinite(amount) || amount <= 0 || amount > 10000) return null;
  return Math.round(amount * 100) / 100;
}

function amountMatches(line) {
  return [
    ...String(line || "").matchAll(
      /(?:US\$|\$)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})|[0-9]+(?:\.\d{2}))(?!\d)/gi,
    ),
  ]
    .map((match) => normalizeAmount(match[1]))
    .filter((amount) => amount !== null);
}

function totalKeywordScore(line) {
  const lower = String(line || "").toLowerCase();
  if (/\b(?:sub\s*total|subtotal|sales tax|tax|savings|change|discount|coupon)\b/.test(lower)) {
    return 0;
  }
  if (/\b(?:grand\s+total|total\s+due|amount\s+due|balance\s+due|total\s+amount)\b/.test(lower)) {
    return 100;
  }
  if (/\btotal\b/.test(lower)) return 80;
  if (/\b(?:paid|card|credit|debit|approved amount)\b/.test(lower)) return 55;
  return 0;
}

function extractReceiptTotal(text) {
  const lines = String(text || "")
    .split(/\n+/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean);
  const candidates = [];

  for (const [index, line] of lines.entries()) {
    const score = totalKeywordScore(line);
    if (!score) continue;

    const scanWindow = lines.slice(index, index + 4);
    for (const [offset, candidateLine] of scanWindow.entries()) {
      if (offset && totalKeywordScore(candidateLine)) break;
      const amounts = amountMatches(candidateLine);
      for (const amount of amounts) {
        candidates.push({
          amount,
          label: line.slice(0, 80),
          line: candidateLine.slice(0, 160),
          score: score - offset * 8,
          index,
        });
      }
      if (amounts.length) break;
    }
  }

  candidates.sort((left, right) => right.score - left.score || right.index - left.index);
  return candidates[0] || null;
}

function receiptTextLines(text) {
  return String(text || "")
    .split(/\n+/)
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean);
}

function normalizeReceiptDescription(value) {
  const raw = String(value || "")
    .replace(/[|_*#]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!raw) return "";

  if (/\blidl\b/i.test(raw) || /\blidi\b/i.test(raw) || /\b1idl\b/i.test(raw)) {
    return "Lidl";
  }
  if (/\b11dl\b/i.test(raw) || /\bl\s*&\s*d\s*[l1i]\b/i.test(raw)) {
    return "Lidl";
  }

  const cleaned = raw
    .replace(/^[^a-zA-Z0-9$]+/, "")
    .replace(/[^a-zA-Z0-9.)]+$/, "")
    .trim();
  return cleaned.length > 80 ? `${cleaned.slice(0, 77).trimEnd()}...` : cleaned;
}

function truncateReceiptExpenseLabel(value) {
  const cleaned = normalizeReceiptDescription(value);
  if (cleaned.length <= RECEIPT_EXPENSE_LABEL_MAX_LENGTH) return cleaned;

  const clipped = cleaned
    .slice(0, RECEIPT_EXPENSE_LABEL_MAX_LENGTH)
    .replace(/[\s,;:-]+$/g, "")
    .trim();
  return clipped || cleaned.slice(0, RECEIPT_EXPENSE_LABEL_MAX_LENGTH).trim();
}

function knownReceiptMerchant(text) {
  const raw = String(text || "");
  const knownMerchants = [
    ["Costco", /\bcostco\b/i],
    ["Walmart", /\bwalmart\b/i],
    ["Target", /\btarget\b/i],
    ["ALDI", /\baldi\b/i],
    ["Kroger", /\bkroger\b/i],
    ["Food Lion", /\bfood\s+lion\b/i],
    ["Publix", /\bpublix\b/i],
    ["Safeway", /\bsafeway\b/i],
    ["The Home Depot", /\bhome\s+depot\b/i],
    ["Lowe's", /\blowe'?s\b/i],
    ["Amazon", /\bamazon\b/i],
    ["Instacart", /\binstacart\b/i],
  ];

  if (/\blidl\b/i.test(raw) || /\blidi\b/i.test(raw) || /\b1idl\b/i.test(raw)) {
    return "Lidl";
  }
  if (/\b11dl\b/i.test(raw) || /\bl\s*&\s*d\s*[l1i]\b/i.test(raw)) {
    return "Lidl";
  }

  for (const [name, pattern] of knownMerchants) {
    if (pattern.test(raw)) return name;
  }
  return "";
}

function cleanReceiptCandidate(line) {
  return normalizeReceiptDescription(
    String(line || "")
      .replace(/\s+(?:US\$|\$)?[0-9]{1,4}(?:,[0-9]{3})*(?:\.\d{2})\s*$/i, "")
      .replace(/\s+[A-Z]?\s*[0-9]{1,4}(?:\.\d{2})\s*$/i, "")
      .replace(/^\d+\s*[xX]\s+/, "")
      .trim(),
  );
}

function looksLikeReceiptNoise(line, { allowAmount = false } = {}) {
  const cleaned = normalizeReceiptDescription(line);
  const lower = cleaned.toLowerCase();
  if (!cleaned || !/[a-z]/i.test(cleaned)) return true;
  if (/^[A-Z]{1,3}$/.test(cleaned)) return true;
  if (!allowAmount && amountMatches(cleaned).length) return true;
  if (/https?:\/\/|www\.|\.com\b|@/.test(lower)) return true;
  if (/^\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b/.test(lower)) return true;
  if (/^\d{1,2}:\d{2}\b/.test(lower)) return true;
  if (
    /\b(?:total|subtotal|tax|change|balance|amount|due|visa|mastercard|discover|amex|card|debit|credit|tender|payment|approved|auth|merchant|terminal|batch|receipt|invoice|date|time|cashier|operator|register|order|transaction|phone|tel|thank|welcome|return|savings|coupon|discount|description|qty|quantity|price)\b/.test(
      lower,
    )
  ) {
    return true;
  }
  if (/\b(?:street|st\.?|avenue|ave\.?|road|rd\.?|drive|dr\.?|blvd|lane|ln\.?|highway|hwy)\b/.test(lower)) {
    return true;
  }
  return false;
}

function looksLikeReceiptItemNoise(line) {
  const cleaned = normalizeReceiptDescription(line);
  const lower = cleaned.toLowerCase();
  if (looksLikeReceiptNoise(cleaned)) return true;
  if (/[@$]/.test(cleaned)) return true;
  if (/\b\d+(?:\.\d+)?\s*(?:lb|lbs|oz|kg|g)\b/i.test(cleaned)) return true;
  if (/^\d+(?:\.\d+)?\s*(?:lb|lbs|oz|kg|g)?\b/i.test(cleaned)) return true;
  if (/\b(?:discount|coupon|sale|return|policy|feedback|review|serving|calories|nutrition)\b/.test(lower)) {
    return true;
  }
  return false;
}

function extractReceiptItemNames(lines) {
  const descriptionIndex = lines.findIndex((line) =>
    /\b(?:description|item|items|product)\b/i.test(line),
  );
  if (descriptionIndex < 0) return [];

  const items = [];
  const seen = new Set();
  for (const line of lines.slice(descriptionIndex + 1)) {
    if (
      /\b(?:sub\s*total|subtotal|sales\s+tax|tax|total\s+due|total|card|change|payment|customer|merchant|terminal|approved)\b/i.test(
        line,
      )
    ) {
      break;
    }

    const candidate = cleanReceiptCandidate(line);
    const key = candidate.toLowerCase();
    if (!candidate || seen.has(key) || looksLikeReceiptItemNoise(candidate)) continue;
    seen.add(key);
    items.push(candidate);
    if (items.length >= 3) break;
  }

  return items;
}

function extractReceiptFallbackDescription(lines, attachment) {
  const descriptionIndex = lines.findIndex((line) =>
    /\b(?:description|item|items|product)\b/i.test(line),
  );
  const headerLines = descriptionIndex >= 0 ? lines.slice(0, descriptionIndex) : lines.slice(0, 12);

  for (const line of headerLines) {
    const candidate = cleanReceiptCandidate(line);
    if (!looksLikeReceiptNoise(candidate)) return candidate;
  }

  for (const line of lines.slice(0, 30)) {
    const candidate = cleanReceiptCandidate(line);
    if (!looksLikeReceiptNoise(candidate, { allowAmount: true })) return candidate;
  }

  return sanitizeFilename(attachment?.filename || "Receipt");
}

function extractReceiptDescription(text, attachment) {
  const merchant = knownReceiptMerchant(text);
  const lines = receiptTextLines(text).map(normalizeReceiptDescription).filter(Boolean);
  const items = extractReceiptItemNames(lines);
  const itemSummary = items.join(", ");

  if (merchant && itemSummary) {
    return truncateReceiptExpenseLabel(`${merchant} - ${itemSummary}`);
  }
  if (merchant) return truncateReceiptExpenseLabel(merchant);
  if (itemSummary) return truncateReceiptExpenseLabel(itemSummary);
  return truncateReceiptExpenseLabel(extractReceiptFallbackDescription(lines, attachment));
}

async function extractPdfText(localPath) {
  try {
    const { stdout } = await execFileAsync(
      "pdftotext",
      ["-layout", "-enc", "UTF-8", localPath, "-"],
      {
        encoding: "utf8",
        maxBuffer: 5 * 1024 * 1024,
      },
    );
    return stdout || "";
  } catch (error) {
    if (error.code !== "ENOENT") {
      await log(`PDF text extraction failed for ${path.basename(localPath)}: ${error.message}`);
    }
    return "";
  }
}

async function deleteDriveFileQuietly(fileId, purpose) {
  if (!fileId) return;
  try {
    await runGog([
      "drive",
      "delete",
      fileId,
      "--account",
      CONFIG.account,
      "--force",
      "--no-input",
    ]);
  } catch (error) {
    await log(`Could not delete temporary ${purpose} file ${fileId}: ${error.message}`);
  }
}

async function extractTextWithDriveOcr(localPath, uploadName) {
  let docId = "";
  try {
    const result = await runGogJson([
      "drive",
      "upload",
      localPath,
      "--account",
      CONFIG.account,
      "--name",
      `ocr-${uploadName}`,
      "--parent",
      CONFIG.driveFolderId,
      "--convert-to",
      "doc",
    ]);
    const doc = unwrapDriveFile(result);
    docId = doc.id || "";
    if (!docId) return "";

    const { stdout } = await runGog([
      "docs",
      "cat",
      docId,
      "--account",
      CONFIG.account,
      "--max-bytes=200000",
      "--no-input",
    ]);
    return stdout || "";
  } catch (error) {
    await log(`Drive OCR failed for ${uploadName}: ${error.message}`);
    return "";
  } finally {
    await deleteDriveFileQuietly(docId, "OCR");
  }
}

async function detectReceiptTotal({ localPath, uploadName, attachment }) {
  if (!CONFIG.ocrEnabled) return null;

  const extension = path.extname(attachment.filename || uploadName).toLowerCase();
  const mimeType = String(attachment.mimeType || "").toLowerCase();
  const textParts = [];

  if (extension === ".pdf" || mimeType === "application/pdf") {
    const pdfText = await extractPdfText(localPath);
    if (pdfText.trim()) textParts.push(pdfText);
  }

  let total = extractReceiptTotal(textParts.join("\n"));
  if (!total) {
    const ocrText = await extractTextWithDriveOcr(localPath, uploadName);
    if (ocrText.trim()) textParts.push(ocrText);
    total = extractReceiptTotal(textParts.join("\n"));
  }

  const combinedText = textParts.join("\n");
  if (total) {
    total.description = extractReceiptDescription(combinedText, attachment);
    await log(
      `Detected receipt total $${total.amount.toFixed(2)} in ${attachment.filename} from "${total.label}" for "${total.description}"`,
    );
  }
  return total;
}

function typedAmountOperation(line, amountIndex = 0) {
  const lower = String(line || "").toLowerCase();
  const before = lower.slice(Math.max(0, amountIndex - 45), amountIndex);
  const context = lower.slice(Math.max(0, amountIndex - 45), amountIndex + 90);

  const directOverrideBeforeAmount =
    /\b(?:final|actual|total|amount|paid|pay|owe|owed|reimburse|venmo|zelle|split|should\s+be|comes?\s+to|is|=|use)\s*(?:us\$|\$)?\s*$/i.test(
      before,
    ) || /(?:最终|最后|实际|应付|报销|算|收|用|总共|合计|金额|费用).{0,12}(?:us\$|\$)?\s*$/.test(before);
  if (directOverrideBeforeAmount) return "override";

  if (
    /\b(?:coupon|discount|deduct|deduction|subtract|minus|take\s+off|taken\s+off|credit|refund|rebate)\b/i.test(context) ||
    /(?:优惠券|折扣|扣掉|扣除|减去|减掉|抵扣|退回|退款|返现|自用|自己|个人|不是公摊|不用分摊)/.test(context)
  ) {
    return "subtract";
  }
  if (
    /\b(?:add|plus|include|including|tip|fee|extra|surcharge)\b/i.test(context) ||
    /(?:加上|另加|加|包括|小费|停车费|手续费|额外|另外)/.test(context)
  ) {
    return "add";
  }
  return "override";
}

function normalizeTypedAmountOperation(value) {
  const operation = String(value || "").toLowerCase().trim();
  if (["override", "subtract", "add"].includes(operation)) return operation;
  return "override";
}

function looseAmountMatches(text) {
  return [
    ...String(text || "").matchAll(
      /(?:US\$|\$)?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)(?!\d)/gi,
    ),
  ]
    .map((match) => normalizeAmount(match[1]))
    .filter((amount) => amount !== null);
}

function normalizeModelTypedAmountOperation(item) {
  const operation = normalizeTypedAmountOperation(item?.operation);
  const excerpt = String(item?.excerpt || item?.reason || "").toLowerCase();
  if (
    operation !== "override" &&
    (/\b(?:final|charge|chargeable|should\s+be|comes?\s+to|total\s+after|actual\s+total|amount\s+to\s+use|use\s+this)\b/.test(
      excerpt,
    ) ||
      /(?:最终|最后|实际|应付|报销|算|收|用).{0,12}(?:金额|总额|总共|合计|费用)?/.test(excerpt))
  ) {
    return "override";
  }
  return operation;
}

function normalizeModelItemAmount(item, operation, excerpt) {
  const normalized = normalizeAmount(item?.amount);
  if (normalized === null) return null;
  if (!["subtract", "add"].includes(operation)) return normalized;

  const amounts = looseAmountMatches(excerpt);
  if (amounts.length <= 1) return normalized;
  return Math.round(amounts.reduce((sum, amount) => sum + amount, 0) * 100) / 100;
}

function addTypedAmount(results, seen, amount, line, source, amountIndex = 0) {
  const normalized = normalizeAmount(amount);
  if (normalized === null) return;

  const excerpt = line.replace(/\s+/g, " ").trim().slice(0, 160);
  const key = `${normalized.toFixed(2)}:${excerpt.toLowerCase()}`;
  if (seen.has(key)) return;

  seen.add(key);
  results.push({
    amount: normalized,
    excerpt,
    operation: typedAmountOperation(line, amountIndex),
    source,
  });
}

function addModelTypedAmount(results, seen, item) {
  const excerpt = String(item?.excerpt || item?.reason || "model-detected amount")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 160);
  const operation = normalizeModelTypedAmountOperation(item);
  const normalized = normalizeModelItemAmount(item, operation, excerpt);
  if (normalized === null) return;
  const key = `${operation}:${normalized.toFixed(2)}:${excerpt.toLowerCase()}`;
  if (seen.has(key)) return;

  seen.add(key);
  results.push({
    amount: normalized,
    excerpt,
    operation,
    source: "model",
    model: CONFIG.bodyModel,
  });
}

function parseJsonObject(text) {
  const raw = String(text || "").trim();
  try {
    return JSON.parse(raw);
  } catch {
    const start = raw.indexOf("{");
    const end = raw.lastIndexOf("}");
    if (start >= 0 && end > start) {
      return JSON.parse(raw.slice(start, end + 1));
    }
    throw new Error("model did not return JSON");
  }
}

const BODY_AMOUNT_OUTPUT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    items: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        properties: {
          amount: { type: "number" },
          operation: { type: "string", enum: ["override", "subtract", "add"] },
          excerpt: { type: "string" },
          confidence: { type: "number" },
        },
        required: ["amount", "operation", "excerpt", "confidence"],
      },
    },
  },
  required: ["items"],
};

function effectiveBodyModelProvider() {
  const provider = String(CONFIG.bodyModelProvider || "").trim().toLowerCase();
  if (provider) return provider;
  if (!CONFIG.bodyModel) return "";
  if (
    CONFIG.bodyModel.toLowerCase() === "codex" ||
    CONFIG.bodyModel.toLowerCase().startsWith("codex/") ||
    /^gpt-/i.test(CONFIG.bodyModel)
  ) {
    return "codex";
  }
  return "ollama";
}

function codexCliModelName() {
  const model = String(CONFIG.bodyModel || "").trim();
  if (!model || model.toLowerCase() === "codex") return "gpt-5.4-mini";
  return model.replace(/^codex\//i, "").replace(/^openai-codex\//i, "");
}

function buildBodyAmountPrompt(text) {
  return [
    "Extract event expense amount instructions from this email body.",
    "Return only JSON with this shape:",
    '{"items":[{"amount":12.34,"operation":"override|subtract|add","excerpt":"short quote from body","confidence":0.0}]}',
    "",
    "Rules:",
    "- operation override means this is the final amount to use, pay, owe, reimburse, or split.",
    "- operation subtract means this amount should be deducted from a receipt total, such as coupon, discount, personal share, refund, credit, or 'deduct from total'.",
    "- operation add means this amount should be added to a receipt total, such as tip, fee, extra, or surcharge.",
    "- Combine related arithmetic in one item. Example: '$5 coupon and $3 for myself, deduct both' => amount 8, operation subtract.",
    "- Do not net coupon/discount/personal deductions against each other. If the body says '$14.25 of my own snacks and $5 coupon, deduct those', return amount 19.25, operation subtract.",
    "- If a sentence includes both deductions and additions, return separate items for the total deductions and total additions.",
    "- If the body says a final after-adjustment amount, use operation override for that final amount.",
    "- Phrases like 'so charge 106.40', 'final amount is 106.40', or 'please use 106.40' are final override amounts.",
    "- Chinese is supported. Treat 优惠券/折扣/扣掉/扣除/减去/自用/自己/个人 as subtract. Treat 加上/另加/小费/停车费/手续费/额外 as add. Treat 最终/最后/实际/应付/报销/总共/合计 as override when they indicate the final amount.",
    "- Ignore quoted automated workflow replies, spreadsheet tables, message IDs, dates, and receipt OCR text pasted from earlier replies.",
    "- If there is no usable expense amount instruction, return {\"items\":[]}.",
    "",
    "Email body:",
    "-----",
    String(text || "").slice(0, 12000),
    "-----",
  ].join("\n");
}

async function parseModelItemsFromPayload(parsed) {
  const results = [];
  const seen = new Set();
  for (const item of Array.isArray(parsed?.items) ? parsed.items : []) {
    addModelTypedAmount(results, seen, item);
  }
  return results;
}

async function extractTypedAmountsWithOllama(text) {
  if (!CONFIG.bodyModel || !String(text || "").trim()) return [];

  try {
    const response = await fetch(CONFIG.bodyModelUrl, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model: CONFIG.bodyModel,
        prompt: buildBodyAmountPrompt(text),
        stream: false,
        format: "json",
        options: {
          temperature: 0,
        },
      }),
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const payload = await response.json();
    const parsed = parseJsonObject(payload?.response || "");
    return parseModelItemsFromPayload(parsed);
  } catch (error) {
    await log(`Email body Ollama amount extraction failed; falling back to rules: ${error.message}`);
    return [];
  }
}

async function extractTypedAmountsWithCodex(text) {
  if (!String(text || "").trim()) return [];

  const tempDir = mkdtempSync(path.join(tmpdir(), "event-expense-codex-"));
  const schemaPath = path.join(tempDir, "schema.json");
  const outputPath = path.join(tempDir, "output.json");

  try {
    writeFileSync(schemaPath, JSON.stringify(BODY_AMOUNT_OUTPUT_SCHEMA), "utf8");
    const model = codexCliModelName();
    const timeout = Number.isFinite(CONFIG.bodyModelTimeoutMs)
      ? CONFIG.bodyModelTimeoutMs
      : 120000;
    await runCommandWithInput(
      CONFIG.codexBin,
      [
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "-m",
        model,
        "-c",
        `model_reasoning_effort="${CONFIG.bodyModelReasoning || "low"}"`,
        "--output-schema",
        schemaPath,
        "-o",
        outputPath,
        "-",
      ],
      buildBodyAmountPrompt(text),
      {
        encoding: "utf8",
        maxBuffer: 20 * 1024 * 1024,
        timeout,
        env: {
          ...process.env,
          HOME: process.env.HOME || "/home/zhihongz",
        },
      },
    );
    const parsed = parseJsonObject(readFileSync(outputPath, "utf8"));
    return parseModelItemsFromPayload(parsed);
  } catch (error) {
    const details = [error.message, error.stderr, error.stdout]
      .filter(Boolean)
      .join("\n")
      .trim();
    await log(`Email body Codex amount extraction failed; falling back to rules: ${details}`);
    return [];
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
}

async function extractTypedAmountsWithModel(text) {
  const provider = effectiveBodyModelProvider();
  if (provider === "codex") return extractTypedAmountsWithCodex(text);
  if (provider === "ollama") return extractTypedAmountsWithOllama(text);
  return [];
}

function extractTypedAmountsWithRules(text) {
  const results = [];
  const seen = new Set();
  const lines = String(text || "")
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);

  for (const line of lines) {
    const currencyMatches = line.matchAll(
      /(?:^|[^\w])(?:US\$|\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)(?!\d)/gi,
    );
    for (const match of currencyMatches) {
      addTypedAmount(
        results,
        seen,
        match[1],
        line,
        "currency",
        (match.index || 0) + match[0].lastIndexOf(match[1]),
      );
    }

    if (!/\b(amount|paid|owe|owed|total|cost|spent|share|reimburse|venmo|zelle|split)\b/i.test(line)) {
      continue;
    }

    const keywordMatches = line.matchAll(
      /\b(?:amount|paid|owe|owed|total|cost|spent|share|reimburse|venmo|zelle|split)\b[^0-9$]{0,35}\$?\s*([0-9]{1,4}(?:\.\d{1,2})?)(?!\d)/gi,
    );
    for (const match of keywordMatches) {
      addTypedAmount(
        results,
        seen,
        match[1],
        line,
        "keyword",
        (match.index || 0) + match[0].lastIndexOf(match[1]),
      );
    }
  }

  return results;
}

async function extractTypedAmounts(text) {
  const modelResults = await extractTypedAmountsWithModel(text);
  if (modelResults.length) return modelResults;
  return extractTypedAmountsWithRules(text);
}

function typedAmountDisplay(typedAmount) {
  const operationLabel =
    typedAmount.operation === "subtract"
      ? "deduct"
      : typedAmount.operation === "add"
        ? "add"
        : "use";
  return `$${typedAmount.amount.toFixed(2)} (${operationLabel})`;
}

function combineReceiptAdjustments(typedAmounts) {
  const adjustments = typedAmounts.filter((entry) => entry.operation === "subtract" || entry.operation === "add");
  if (!adjustments.length) return null;

  const subtractTotal = adjustments
    .filter((entry) => entry.operation === "subtract")
    .reduce((sum, entry) => sum + entry.amount, 0);
  const addTotal = adjustments
    .filter((entry) => entry.operation === "add")
    .reduce((sum, entry) => sum + entry.amount, 0);
  const net = Math.round((addTotal - subtractTotal) * 100) / 100;
  if (net === 0) return null;

  return {
    amount: Math.abs(net),
    excerpt: adjustments.map((entry) => entry.excerpt).join(" | ").slice(0, 160),
    operation: net > 0 ? "add" : "subtract",
    source: adjustments.some((entry) => entry.source === "model") ? "model" : "rules",
    model: adjustments.find((entry) => entry.model)?.model || "",
  };
}

function selectTypedAmountForReceipt(typedAmounts, attachmentIndex, attachmentCount) {
  if (!typedAmounts.length) return null;
  if (attachmentCount > 1 && typedAmounts[attachmentIndex]) {
    return typedAmounts[attachmentIndex];
  }
  return (
    typedAmounts.find((entry) => entry.operation === "override") ||
    combineReceiptAdjustments(typedAmounts) ||
    typedAmounts[0]
  );
}

function resolveReceiptExpenseAmount(receiptTotal, typedAmountOverride) {
  if (!typedAmountOverride) {
    return receiptTotal ? { amount: receiptTotal.amount, source: "receipt-ocr", note: "" } : null;
  }

  if (typedAmountOverride.operation === "subtract") {
    if (!receiptTotal) return null;
    return {
      amount: Math.max(0, Math.round((receiptTotal.amount - typedAmountOverride.amount) * 100) / 100),
      source: "email-body-adjustment",
      note: `Email body adjustment: -$${typedAmountOverride.amount.toFixed(2)}`,
    };
  }

  if (typedAmountOverride.operation === "add") {
    if (!receiptTotal) return null;
    return {
      amount: Math.round((receiptTotal.amount + typedAmountOverride.amount) * 100) / 100,
      source: "email-body-adjustment",
      note: `Email body adjustment: +$${typedAmountOverride.amount.toFixed(2)}`,
    };
  }

  return {
    amount: typedAmountOverride.amount,
    source: "email-body",
    note: `Email body amount: $${typedAmountOverride.amount.toFixed(2)}`,
  };
}

function payerFromHeader(fromHeader) {
  const raw = String(fromHeader || "").trim();
  const email = extractEmailAddress(raw);
  const name = raw
    .replace(/<[^>]+>/g, "")
    .replace(email, "")
    .replace(/^"|"$/g, "")
    .trim();
  return name || email || raw;
}

function isWorkflowSentMessage({ headers, bodyText }) {
  const fromEmail = extractEmailAddress(headers.get("from")).toLowerCase();
  return (
    fromEmail === CONFIG.account.toLowerCase() ||
    CONFIG.ignoredSenderEmails.map((email) => email.toLowerCase()).includes(fromEmail) ||
    /automated reply from the (?:Lake Anna receipt|Event Expense) workflow/i.test(bodyText) ||
    new RegExp(`automated reply from the ${escapeRegExp(CONFIG.workflowName)} workflow`, "i").test(bodyText)
  );
}

function isWorkflowSender(headers, message) {
  const fromEmail = extractEmailAddress(headers.get("from")).toLowerCase();
  return (
    isSentMessage(message) ||
    fromEmail === CONFIG.account.toLowerCase() ||
    CONFIG.ignoredSenderEmails.map((email) => email.toLowerCase()).includes(fromEmail)
  );
}

function isSentMessage(message) {
  return (message.labelIds || []).includes("SENT");
}

function subjectMatchesEvent(subject) {
  const pattern = CONFIG.gmailSubjectRegex || escapeRegExp(CONFIG.gmailSubject);
  try {
    return new RegExp(pattern, "i").test(String(subject || ""));
  } catch {
    return new RegExp(escapeRegExp(CONFIG.gmailSubject), "i").test(String(subject || ""));
  }
}

function buildTypedAmountRow({ message, headers, typedAmount, receiptLink }) {
  const comment = typedAmountComment({ message, headers, typedAmount });
  return [
    "Typed email amount",
    typedAmount.amount.toFixed(2),
    payerFromHeader(headers.get("from")),
    receiptLink || "",
    comment,
  ];
}

function buildReceiptExpenseRow({
  message,
  headers,
  attachment,
  receiptTotal,
  typedAmountOverride = null,
  receiptLink,
}) {
  const resolvedAmount = resolveReceiptExpenseAmount(receiptTotal, typedAmountOverride);
  const amount = resolvedAmount.amount;
  const description = receiptTotal?.description || sanitizeFilename(attachment.filename || "Receipt");
  const commentParts = [
    `Subject: ${headers.get("subject")}`,
    `Message ID: ${message.id}`,
  ];
  if (typedAmountOverride) {
    commentParts.push(resolvedAmount.note);
    commentParts.push(`Excerpt: ${typedAmountOverride.excerpt}`);
    if (typedAmountOverride.source === "model") {
      commentParts.push(`Body parsed by model: ${typedAmountOverride.model || CONFIG.bodyModel}`);
    }
    if (receiptTotal && typedAmountOverride.operation === "override") {
      commentParts.push(`OCR total ignored: $${receiptTotal.amount.toFixed(2)} from ${receiptTotal.label}`);
    } else if (receiptTotal) {
      commentParts.push(`OCR total: $${receiptTotal.amount.toFixed(2)} from ${receiptTotal.label}`);
    }
  } else if (receiptTotal) {
    commentParts.push(`OCR label: ${receiptTotal.label}`);
  }
  if (receiptTotal?.description) commentParts.push(`OCR item: ${receiptTotal.description}`);

  return [
    description,
    amount.toFixed(2),
    payerFromHeader(headers.get("from")),
    receiptLink || "",
    commentParts.join("; "),
  ];
}

async function processReceiptExpense({
  args,
  state,
  key,
  message,
  headers,
  attachment,
  receiptTotal,
  typedAmountOverride = null,
  receiptLink,
}) {
  const resolvedAmount = resolveReceiptExpenseAmount(receiptTotal, typedAmountOverride);
  if (!resolvedAmount) return "no-total";
  if (state.receiptExpenses?.[key]?.status === "processed") return "already-processed";

  const row = buildReceiptExpenseRow({
    message,
    headers,
    attachment,
    receiptTotal,
    typedAmountOverride,
    receiptLink,
  });
  const amount = resolvedAmount.amount;

  if (args.dryRun) {
    await log(
      `Would append receipt expense $${amount.toFixed(2)} from ${attachment.filename}`,
    );
    return "dry-run";
  }

  await appendSummaryRow(row);
  state.receiptExpenses[key] = {
    status: "processed",
    processedAt: new Date().toISOString(),
    messageId: message.id,
    attachmentName: attachment.filename,
    amount,
    amountSource: resolvedAmount.source,
    typedAmountExcerpt: typedAmountOverride?.excerpt || "",
    typedAmountOperation: typedAmountOverride?.operation || "",
    typedAmountSource: typedAmountOverride?.source || "",
    bodyModel: typedAmountOverride?.model || "",
    label: receiptTotal?.label || "",
    description: receiptTotal?.description || "",
    receiptLink,
    row,
  };
  writeState(state);
  return "processed";
}

function normalizeSummaryRow(row, fallbackComment) {
  const normalized = [...(row || [])];
  while (normalized.length < 5) normalized.push("");
  if (!String(normalized[4] || "").trim()) normalized[4] = fallbackComment;
  return normalized;
}

function migrateSummaryRow(row, fallbackComment) {
  if (!Array.isArray(row)) return row;
  if (row.length >= 5) return normalizeSummaryRow(row, fallbackComment);
  return normalizeSummaryRow(row, fallbackComment);
}

function typedAmountMatches(candidate, message, typedAmount) {
  if (!candidate || candidate.messageId !== message.id) return false;
  const candidateAmount = normalizeAmount(candidate.amount || candidate.row?.[1]);
  return candidateAmount !== null && candidateAmount === typedAmount.amount;
}

function findExistingTypedAmount(state, key, message, typedAmount) {
  if (state.typedAmounts[key]) return { key, value: state.typedAmounts[key] };
  for (const [candidateKey, candidate] of Object.entries(state.typedAmounts || {})) {
    if (typedAmountMatches(candidate, message, typedAmount)) {
      return { key: candidateKey, value: candidate };
    }
  }
  return { key, value: null };
}

function typedAmountComment({ message, headers, typedAmount }) {
  const parts = [
    `Subject: ${headers.get("subject")}`,
    `Message ID: ${message.id}`,
    `Body amount: ${typedAmountDisplay(typedAmount)}`,
    `Excerpt: ${typedAmount.excerpt}`,
  ];
  if (typedAmount.source === "model") {
    parts.push(`Parsed by model: ${typedAmount.model || CONFIG.bodyModel}`);
  }
  return parts.join("; ");
}

function receiptExpenseKey(key) {
  return `${key}:expense`;
}

function skippedAsNoOp(state, messageId) {
  const reason = String(state.skippedMessages?.[messageId]?.reason || "");
  return /no pdf\/image receipt attachment or typed amount|workflow account|workflow marker|message was sent/i.test(
    reason,
  );
}

function attachmentAlreadyProcessed(state, message, attachment) {
  const key = stableAttachmentKey(message, attachment);
  const existingAttachment = findExistingAttachment(state, key, message, attachment);
  const existing = existingAttachment.value;
  if (existing?.status !== "processed") return false;
  const expense = state.receiptExpenses?.[receiptExpenseKey(existingAttachment.key)];
  return !existing.receiptTotal || expense?.status === "processed";
}

function messageAlreadyComplete(state, message, attachments) {
  const messageId = message.id || "";
  if (!messageId) return false;
  if (skippedAsNoOp(state, messageId)) return true;
  if (state.replies?.[messageId]?.status !== "sent") return false;

  if (attachments.length) {
    return attachments.every((attachment) => attachmentAlreadyProcessed(state, message, attachment));
  }

  if (state.emailPdfs?.[`${messageId}:email-pdf`]?.status === "processed") return true;
  return Object.values(state.typedAmounts || {}).some(
    (entry) => entry?.messageId === messageId && entry?.status === "processed",
  );
}

function toPdfText(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[^\x09\x0a\x0d\x20-\x7e]/g, "?");
}

function escapePdfString(value) {
  return toPdfText(value)
    .replace(/\\/g, "\\\\")
    .replace(/\(/g, "\\(")
    .replace(/\)/g, "\\)")
    .replace(/\r/g, "");
}

function wrapPdfLine(line, maxChars) {
  const words = String(line || "").split(/\s+/).filter(Boolean);
  if (!words.length) return [""];

  const lines = [];
  let current = "";
  for (const word of words) {
    if (word.length > maxChars) {
      if (current) {
        lines.push(current);
        current = "";
      }
      for (let index = 0; index < word.length; index += maxChars) {
        lines.push(word.slice(index, index + maxChars));
      }
      continue;
    }

    const next = current ? `${current} ${word}` : word;
    if (next.length > maxChars) {
      lines.push(current);
      current = word;
    } else {
      current = next;
    }
  }

  if (current) lines.push(current);
  return lines;
}

function buildPdfBytes(lines) {
  const pageWidth = 612;
  const pageHeight = 792;
  const margin = 54;
  const fontSize = 9;
  const leading = 12;
  const linesPerPage = Math.floor((pageHeight - margin * 2) / leading);
  const maxChars = 94;

  const wrappedLines = lines.flatMap((line) =>
    String(line || "")
      .split("\n")
      .flatMap((part) => wrapPdfLine(part, maxChars)),
  );

  const pages = [];
  for (let index = 0; index < wrappedLines.length; index += linesPerPage) {
    pages.push(wrappedLines.slice(index, index + linesPerPage));
  }
  if (!pages.length) pages.push([""]);

  const objects = [];
  const addObject = (body) => {
    objects.push(body);
    return objects.length;
  };

  const catalogId = addObject("<< /Type /Catalog /Pages 2 0 R >>");
  const pagesId = addObject("");
  const fontId = addObject("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>");
  const pageIds = [];

  for (const pageLines of pages) {
    const textOps = pageLines.map((line) => `(${escapePdfString(line)}) Tj T*`).join("\n");
    const stream = [
      "BT",
      `/F1 ${fontSize} Tf`,
      `${margin} ${pageHeight - margin} Td`,
      `${leading} TL`,
      textOps,
      "ET",
    ].join("\n");
    const contentId = addObject(`<< /Length ${Buffer.byteLength(stream, "utf8")} >>\nstream\n${stream}\nendstream`);
    const pageId = addObject(
      `<< /Type /Page /Parent ${pagesId} 0 R /MediaBox [0 0 ${pageWidth} ${pageHeight}] /Resources << /Font << /F1 ${fontId} 0 R >> >> /Contents ${contentId} 0 R >>`,
    );
    pageIds.push(pageId);
  }

  objects[pagesId - 1] =
    `<< /Type /Pages /Kids [${pageIds.map((id) => `${id} 0 R`).join(" ")}] /Count ${pageIds.length} >>`;

  const chunks = ["%PDF-1.4\n"];
  const offsets = [0];
  for (const [index, body] of objects.entries()) {
    offsets.push(Buffer.byteLength(chunks.join(""), "utf8"));
    chunks.push(`${index + 1} 0 obj\n${body}\nendobj\n`);
  }

  const xrefOffset = Buffer.byteLength(chunks.join(""), "utf8");
  chunks.push(`xref\n0 ${objects.length + 1}\n`);
  chunks.push("0000000000 65535 f \n");
  for (const offset of offsets.slice(1)) {
    chunks.push(`${String(offset).padStart(10, "0")} 00000 n \n`);
  }
  chunks.push(
    `trailer\n<< /Size ${objects.length + 1} /Root ${catalogId} 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`,
  );

  return Buffer.from(chunks.join(""), "utf8");
}

function writeEmailPdf({ message, headers, bodyText, typedAmounts, outputPath }) {
  const lines = [
    `${CONFIG.eventName} Email Receipt`,
    `Generated: ${new Date().toISOString()}`,
    `Email Date: ${isoEmailDate(message, headers.get("date")) || headers.get("date")}`,
    `From: ${headers.get("from")}`,
    `To: ${headers.get("to")}`,
    `Subject: ${headers.get("subject")}`,
    `Message ID: ${message.id || ""}`,
    "",
    "Detected typed amount(s):",
    ...(typedAmounts.length
      ? typedAmounts.map((entry) => `${typedAmountDisplay(entry)} - ${entry.excerpt}`)
      : ["(none)"]),
    "",
    "Email body:",
    ...(bodyText || "(empty)").slice(0, 20000).split("\n"),
  ];

  writeFileSync(outputPath, buildPdfBytes(lines));
}

async function processTypedAmount({
  args,
  state,
  message,
  headers,
  typedAmount,
  index,
  receiptLink,
}) {
  const key = `${message.id}:typed:${index}:${typedAmount.amount.toFixed(2)}:${shortHash(typedAmount.excerpt)}`;
  const existingTypedAmount = findExistingTypedAmount(state, key, message, typedAmount);
  const existing = existingTypedAmount.value;
  if (existing && existingTypedAmount.key !== key && !state.typedAmounts[key]) {
    state.typedAmounts[key] = existing;
    writeState(state);
  }
  if (existing?.status === "processed") return "already-processed";

  const fallbackComment = typedAmountComment({ message, headers, typedAmount });
  const row =
    existing?.row && existing.row.length >= 4
      ? migrateSummaryRow(existing.row, fallbackComment)
      : buildTypedAmountRow({ message, headers, typedAmount, receiptLink });
  if (args.dryRun) {
    await log(
      `Would append typed amount $${typedAmount.amount.toFixed(2)} from message ${message.id}`,
    );
    return "dry-run";
  }

  await appendSummaryRow(row);
  state.typedAmounts[key] = {
    status: "processed",
    processedAt: new Date().toISOString(),
    messageId: message.id,
    amount: typedAmount.amount,
    excerpt: typedAmount.excerpt,
    row,
  };
  writeState(state);
  return "processed";
}

async function processEmailPdf({ args, state, message, headers, bodyText, typedAmounts }) {
  const key = `${message.id}:email-pdf`;
  const existing = state.emailPdfs[key];
  if (existing?.status === "processed") {
    return {
      status: "already-processed",
      receipt: existing.receipt || null,
      link: existing?.receipt?.webViewLink || existing?.row?.[7] || "",
    };
  }

  const uploadName = [
    dateForFilename(message, headers.get("date")),
    CONFIG.eventSlug,
    shortHash(message.id),
    "email-receipt.pdf",
  ].join("-");

  const receipt = {
    filename: uploadName,
    mimeType: "application/pdf",
  };

  if (args.dryRun) {
    await log(`Would generate email PDF receipt ${uploadName} from message ${message.id}`);
    return { status: "dry-run", receipt, link: "" };
  }

  const localPath = path.join(CONFIG.downloadDir, uploadName);
  writeEmailPdf({ message, headers, bodyText, typedAmounts, outputPath: localPath });
  const stats = statSync(localPath);
  const driveFile = await uploadToDrive(localPath, uploadName);
  const row = buildRow({
    message,
    headers,
    attachment: {
      filename: uploadName,
      mimeType: "application/pdf",
      size: stats.size,
    },
    driveFile,
    notes: `Generated PDF from email body by ${CONFIG.workflowName} workflow`,
  });

  state.emailPdfs[key] = {
    status: "uploaded",
    uploadedAt: new Date().toISOString(),
    messageId: message.id,
    receipt: {
      ...receipt,
      driveFile,
      size: stats.size,
    },
    row,
  };
  writeState(state);

  await appendReceiptRow(row);
  state.emailPdfs[key] = {
    ...state.emailPdfs[key],
    status: "processed",
    processedAt: new Date().toISOString(),
  };
  writeState(state);

  return {
    status: "processed",
    receipt: state.emailPdfs[key].receipt,
    link: state.emailPdfs[key].receipt?.webViewLink || state.emailPdfs[key].row?.[7] || "",
  };
}

async function processAttachment({
  args,
  state,
  message,
  headers,
  attachment,
  typedAmountOverride = null,
}) {
  const key = stableAttachmentKey(message, attachment);
  const existingAttachment = findExistingAttachment(state, key, message, attachment);
  const existing = existingAttachment.value;
  if (existing && existingAttachment.key !== key && !state.attachments[key]) {
    state.attachments[key] = existing;
    writeState(state);
  }
  if (existing?.status === "processed") {
    const existingReceiptTotal = existing.receiptTotal || null;
    const expenseStatus = await processReceiptExpense({
      args,
      state,
      key: receiptExpenseKey(key),
      message,
      headers,
      attachment,
      receiptTotal: existingReceiptTotal,
      typedAmountOverride,
      receiptLink: existing?.driveFile?.webViewLink || existing?.row?.[7] || "",
    });
    return {
      status: "already-processed",
      link: existing?.driveFile?.webViewLink || existing?.row?.[7] || "",
      expenseStatus,
    };
  }

  if (existing?.status === "uploaded" && existing.row) {
    let expenseStatus = "no-total";
    if (!args.dryRun) {
      await appendReceiptRow(existing.row);
      expenseStatus = await processReceiptExpense({
        args,
        state,
        key: receiptExpenseKey(key),
        message,
        headers,
        attachment,
        receiptTotal: existing.receiptTotal || null,
        typedAmountOverride,
        receiptLink: existing?.driveFile?.webViewLink || existing?.row?.[7] || "",
      });
      state.attachments[key] = {
        ...existing,
        status: "processed",
        processedAt: new Date().toISOString(),
      };
      writeState(state);
    }
    return {
      status: "row-appended",
      link: existing?.driveFile?.webViewLink || existing?.row?.[7] || "",
      expenseStatus,
    };
  }

  const uploadName = [
    dateForFilename(message, headers.get("date")),
    CONFIG.eventSlug,
    shortHash(message.id),
    sanitizeFilename(attachment.filename),
  ].join("-");

  if (args.dryRun) {
    await log(`Would upload ${attachment.filename} from message ${message.id}`);
    return { status: "dry-run", link: "" };
  }

  const localPath = await downloadAttachment(message.id, attachment, uploadName);
  statSync(localPath);
  const receiptTotal = await detectReceiptTotal({ localPath, uploadName, attachment });
  const driveFile = await uploadToDrive(localPath, uploadName);
  const row = buildRow({ message, headers, attachment, driveFile, receiptTotal });

  state.attachments[key] = {
    status: "uploaded",
    uploadedAt: new Date().toISOString(),
    messageId: message.id,
    attachmentId: attachment.attachmentId,
    attachmentName: attachment.filename,
    mimeType: attachment.mimeType,
    size: attachment.size,
    receiptTotal,
    driveFile,
    row,
  };
  writeState(state);

  await appendReceiptRow(row);
  const expenseStatus = await processReceiptExpense({
    args,
    state,
    key: receiptExpenseKey(key),
    message,
    headers,
    attachment,
    receiptTotal,
    typedAmountOverride,
    receiptLink: driveFile.webViewLink || "",
  });
  state.attachments[key] = {
    ...state.attachments[key],
    status: "processed",
    processedAt: new Date().toISOString(),
  };
  writeState(state);
  return {
    status: "processed",
    link: driveFile.webViewLink || "",
    expenseStatus,
  };
}

function setupCheck() {
  mkdirSync(CONFIG.downloadDir, { recursive: true });
  mkdirSync(path.dirname(CONFIG.stateFile), { recursive: true });
  mkdirSync(path.dirname(CONFIG.logFile), { recursive: true });
  statSync(CONFIG.gogBin);
  if (effectiveBodyModelProvider() === "codex") statSync(CONFIG.codexBin);
  console.log(JSON.stringify(CONFIG, null, 2));
}

async function runMonitor(args) {
  mkdirSync(CONFIG.downloadDir, { recursive: true });
  const state = readState();

  await log(`Searching Gmail account ${CONFIG.account} with query: ${CONFIG.query}`);
  const searchResult = await runGogJson([
    "gmail",
    "messages",
    "search",
    CONFIG.query,
    "--account",
    CONFIG.account,
    "--all",
    "--max",
    String(args.max),
    "--results-only",
  ]);

  const messageIds = collectMessageIds(searchResult);
  if (!messageIds.length) {
    await log("No matching Gmail messages found.");
    return;
  }

  let processed = 0;
  let receiptExpenseRows = 0;
  let generatedPdfRows = 0;
  let typedProcessed = 0;
  let replied = 0;
  let skipped = 0;
  for (const messageId of messageIds) {
    const rawMessage = await runGogJson([
      "gmail",
      "get",
      messageId,
      "--account",
      CONFIG.account,
      "--format=full",
    ]);
    const message = unwrapMessage(rawMessage);
    const headers = headersFrom(message);
    const subject = headers.get("subject");

    if (!subjectMatchesEvent(subject)) {
      skipped += 1;
      state.skippedMessages[messageId] = {
        reason: `subject did not match ${CONFIG.gmailSubjectRegex || CONFIG.gmailSubject} after fetch`,
        subject,
        skippedAt: new Date().toISOString(),
      };
      continue;
    }

    if (isWorkflowSender(headers, message)) {
      skipped += 1;
      state.skippedMessages[messageId] = {
        reason: "message was sent by the workflow account or is in sent mail",
        subject,
        skippedAt: new Date().toISOString(),
      };
      continue;
    }

    const attachments = collectAttachments(message.payload).filter(isReceiptAttachment);
    if (messageAlreadyComplete(state, message, attachments)) {
      skipped += 1;
      continue;
    }

    const bodyText = messageBodyText(rawMessage, message);
    if (isWorkflowSentMessage({ headers, bodyText })) {
      skipped += 1;
      state.skippedMessages[messageId] = {
        reason: "message was sent by the workflow account, is in sent mail, or contains the workflow marker",
        subject,
        skippedAt: new Date().toISOString(),
      };
      continue;
    }

    const typedAmounts = await extractTypedAmounts(bodyText);
    if (!attachments.length && !typedAmounts.length) {
      skipped += 1;
      state.skippedMessages[messageId] = {
        reason: "no pdf/image receipt attachment or typed amount",
        subject,
        skippedAt: new Date().toISOString(),
      };
      continue;
    }

    const generatedReceipts = [];
    if (!attachments.length && typedAmounts.length) {
      const result = await processEmailPdf({
        args,
        state,
        message,
        headers,
        bodyText,
        typedAmounts,
      });
      if (result.receipt) generatedReceipts.push(result.receipt);
      if (result.status === "processed" || result.status === "dry-run") {
        generatedPdfRows += 1;
      } else if (result.status !== "already-processed") {
        skipped += 1;
      }
    }

    const typedAmountIndexesUsedByReceipts = new Set();
    for (const [attachmentIndex, attachment] of attachments.entries()) {
      const typedAmountOverride = selectTypedAmountForReceipt(
        typedAmounts,
        attachmentIndex,
        attachments.length,
      );
      if (typedAmountOverride) {
        typedAmounts.forEach((_entry, index) => typedAmountIndexesUsedByReceipts.add(index));
      }

      const result = await processAttachment({
        args,
        state,
        message,
        headers,
        attachment,
        typedAmountOverride,
      });
      if (result.status === "processed" || result.status === "row-appended") processed += 1;
      else if (result.status !== "already-processed") skipped += 1;
      if (result.expenseStatus === "processed" || result.expenseStatus === "dry-run") {
        receiptExpenseRows += 1;
      }
      if (result.link) {
        generatedReceipts.push({ webViewLink: result.link });
      }
    }

    for (const [index, typedAmount] of typedAmounts.entries()) {
      if (typedAmountIndexesUsedByReceipts.has(index)) continue;
      const typedReceiptLink = generatedReceipts[0]?.webViewLink || "";
      const result = await processTypedAmount({
        args,
        state,
        message,
        headers,
        typedAmount,
        index,
        receiptLink: typedReceiptLink,
      });
      if (result === "processed" || result === "dry-run") typedProcessed += 1;
      else if (result !== "already-processed") skipped += 1;
    }

    const replyResult = await replyWithSpreadsheet({
      args,
      state,
      message,
      headers,
      attachments,
      generatedReceipts,
      typedAmounts,
    });
    if (replyResult === "sent" || replyResult === "dry-run") replied += 1;
    else if (!["already-sent", "disabled", "nothing-processed"].includes(replyResult)) {
      skipped += 1;
    }
  }

  if (!args.dryRun) writeState(state);
  await log(
    `Finished. New receipt rows: ${processed}; receipt expense rows: ${receiptExpenseRows}; generated email PDF rows: ${generatedPdfRows}; typed amount rows: ${typedProcessed}; replies: ${replied}; skipped/no-op: ${skipped}.`,
  );
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.setupCheck) {
    setupCheck();
    return;
  }

  const releaseRunLock = createRunLock();
  if (!releaseRunLock) {
    await log(`Another ${CONFIG.workflowName} monitor run is active; exiting.`);
    return;
  }

  try {
    await runMonitor(args);
  } finally {
    releaseRunLock();
  }
}

main().catch(async (error) => {
  await log(`ERROR: ${error.stack || error.message}`);
  process.exitCode = 1;
});
