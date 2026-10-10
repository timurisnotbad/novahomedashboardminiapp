// Nova Home — WhatsApp bridge for the shared inbox («Чаты»).
//
// Connects to WhatsApp the same way WhatsApp Web / Wazzup do: the business
// phone scans a QR code once (WhatsApp → Связанные устройства), the session is
// kept in ./auth, and the phone keeps working as usual.
//
//   incoming / outgoing messages, delivery ticks  ->  POST {BACKEND}/api/inbox/hook
//   the backend sends replies                      ->  POST http://127.0.0.1:{PORT}/send
//
// Listens on 127.0.0.1 only; every request carries the shared secret
// (INBOX_SECRET, derived from BOT_TOKEN when not set — same rule as the
// Python side in backend/config.py).
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import makeWASocket, {
  Browsers,
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  getContentType,
  isJidGroup,
  isLidUser,
  jidNormalizedUser,
  normalizeMessageContent,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import pino from "pino";
import QRCode from "qrcode";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

// ---- settings: the project's .env (same file the bot and server read) -------
function loadEnv(file) {
  const out = {};
  if (!fs.existsSync(file)) return out;
  for (let line of fs.readFileSync(file, "utf8").replace(/^﻿/, "").split(/\r?\n/)) {
    line = line.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const i = line.indexOf("=");
    const key = line.slice(0, i).trim();
    let v = line.slice(i + 1).trim();
    if (v.length >= 2 && (v[0] === '"' || v[0] === "'") && v.endsWith(v[0])) v = v.slice(1, -1);
    else v = v.split(/\s+#/)[0].trim();
    if (v.startsWith("#")) v = "";
    if (!(key in process.env)) out[key] = v;
  }
  return out;
}
const ENV = { ...loadEnv(path.join(ROOT, ".env")), ...process.env };
const PORT = parseInt(ENV.WA_BRIDGE_PORT || "8100", 10);
const BACKEND = (ENV.INBOX_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const SECRET = ENV.INBOX_SECRET
  || (ENV.BOT_TOKEN ? crypto.createHash("sha256").update("inbox:" + ENV.BOT_TOKEN).digest("hex").slice(0, 32) : "dev-inbox");
const MEDIA_DIR = ENV.INBOX_MEDIA_DIR || path.join(ROOT, "data", "inbox_media");
const AUTH_DIR = path.join(HERE, "auth");
const MAX_MEDIA = 30 * 1024 * 1024; // larger files are announced but not downloaded
const HISTORY_DAYS = parseInt(ENV.INBOX_HISTORY_DAYS || "30", 10);

fs.mkdirSync(MEDIA_DIR, { recursive: true });
const logger = pino({ level: ENV.WA_LOG_LEVEL || "warn" });
const log = (...a) => console.log(new Date().toISOString().slice(0, 19).replace("T", " "), ...a);

// ---- state ------------------------------------------------------------------
const state = { status: "starting", qr: null, qrAt: null, me: null, error: null, since: Date.now() };
let sock = null;
let reconnectTimer = null;

// ---- backend delivery (with a retry queue: the server may be restarting) ----
const queue = [];
let flushing = false;

let lastStatus = null;

function post(payload) {
  if (payload.event === "status") {
    // only real changes: a long outage must not fill the queue with "reconnecting"
    if (payload.status === lastStatus) return;
    lastStatus = payload.status;
  }
  queue.push(payload);
  flush();
}

async function flush() {
  if (flushing) return;
  flushing = true;
  try {
    while (queue.length) {
      const item = queue[0];
      try {
        const res = await fetch(BACKEND + "/api/inbox/hook", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Inbox-Secret": SECRET },
          body: JSON.stringify(item),
          signal: AbortSignal.timeout(15000),
        });
        if (res.status === 403) log("backend rejected the secret — INBOX_SECRET differs between .env and server?");
        if (!res.ok && res.status >= 500) throw new Error("HTTP " + res.status);
        queue.shift();
      } catch (e) {
        log("backend unreachable (" + e.message + "), retry in 10 s; queued:", queue.length);
        await new Promise((r) => setTimeout(r, 10000));
      }
    }
  } finally {
    flushing = false;
  }
}

// ---- message parsing ----------------------------------------------------------
const EXT = {
  "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
  "video/mp4": "mp4", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a",
  "application/pdf": "pdf",
};
const KIND = {
  conversation: "text", extendedTextMessage: "text", imageMessage: "image",
  videoMessage: "video", audioMessage: "audio", documentMessage: "document",
  documentWithCaptionMessage: "document", stickerMessage: "sticker",
  locationMessage: "location", liveLocationMessage: "location",
  contactMessage: "contact", contactsArrayMessage: "contact",
  reactionMessage: "reaction", protocolMessage: "protocol",
};

async function phoneJid(jid, alt) {
  // WhatsApp increasingly addresses people by a private "LID" instead of the
  // phone number. Prefer the phone-number jid so a chat keeps one identity
  // and can be matched with the guest's phone in the bookings.
  if (!jid) return jid;
  if (!isLidUser(jid)) return jidNormalizedUser(jid);
  if (alt && !isLidUser(alt)) return jidNormalizedUser(alt);
  try {
    const pn = await sock.signalRepository.lidMapping.getPNForLID(jid);
    if (pn) return jidNormalizedUser(pn);
  } catch (e) { /* mapping unknown yet */ }
  return jidNormalizedUser(jid);
}

function tsOf(m) {
  const t = m.messageTimestamp;
  const n = typeof t === "number" ? t : t && typeof t.toNumber === "function" ? t.toNumber() : Number(t || 0);
  return n ? new Date(n * 1000).toISOString() : new Date().toISOString();
}

async function parse(m, { download }) {
  const key = m.key || {};
  const remote = key.remoteJid || "";
  if (!remote || remote === "status@broadcast" || remote.endsWith("@newsletter") || isJidGroup(remote)) return null;
  const content = normalizeMessageContent(m.message);
  if (!content) return null;
  const type = getContentType(content);
  const kind = KIND[type] || "other";
  if (kind === "protocol" || type === "senderKeyDistributionMessage") return null;
  const body = content[type] || {};
  const jid = await phoneJid(remote, key.remoteJidAlt);
  const out = {
    id: key.id,
    jid,
    lid: isLidUser(remote) ? jidNormalizedUser(remote) : null,
    from_me: !!key.fromMe,
    push_name: key.fromMe ? null : m.pushName || null,
    at: tsOf(m),
    kind,
    text: "",
  };
  if (kind === "reaction") {
    out.text = body.text || "";
    out.reaction_to = body.key && body.key.id;
    return out;
  }
  if (type === "conversation") out.text = content.conversation || "";
  else if (type === "extendedTextMessage") out.text = body.text || "";
  else if (kind === "location") {
    out.text = body.name || body.address || "";
    out.lat = body.degreesLatitude;
    out.lng = body.degreesLongitude;
  } else if (kind === "contact") {
    out.text = body.displayName || (body.contacts || []).map((c) => c.displayName).join(", ");
    out.vcard = body.vcard || null;
  } else out.text = body.caption || "";
  const ctx = body.contextInfo;
  if (ctx && ctx.stanzaId) out.quoted_id = ctx.stanzaId;

  if (["image", "video", "audio", "document", "sticker"].includes(kind)) {
    const mime = (body.mimetype || "").split(";")[0];
    out.mime = mime || null;
    out.file_name = body.fileName || null;
    out.voice = !!body.ptt;
    const size = Number(body.fileLength || 0);
    if (download && size <= MAX_MEDIA) {
      try {
        const buf = await downloadMediaMessage(m, "buffer", {}, { logger, reuploadRequest: sock.updateMediaMessage });
        const ext = EXT[mime] || (body.fileName && path.extname(body.fileName).slice(1)) || "bin";
        const name = `${key.id.replace(/[^A-Za-z0-9]/g, "")}.${ext.replace(/[^A-Za-z0-9]/g, "").slice(0, 8) || "bin"}`;
        fs.writeFileSync(path.join(MEDIA_DIR, name), buf);
        out.media = name;
      } catch (e) {
        log("media download failed", key.id, e.message);
      }
    }
  }
  return out;
}

const STATUS = { 0: "failed", 1: "pending", 2: "sent", 3: "delivered", 4: "read", 5: "read" };

// ---- WhatsApp connection ------------------------------------------------------
async function connect() {
  clearTimeout(reconnectTimer);
  const { state: auth, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  let version;
  try {
    ({ version } = await fetchLatestBaileysVersion());
  } catch (e) { /* offline: the library default */ }
  state.status = "connecting";
  sock = makeWASocket({
    auth,
    version,
    logger,
    browser: Browsers.windows("Nova Inbox"),
    markOnlineOnConnect: false, // don't steal notifications from the phone
    syncFullHistory: false,
    generateHighQualityLinkPreview: false,
  });
  const me = sock;

  me.ev.on("creds.update", saveCreds);

  me.ev.on("connection.update", async (u) => {
    if (me !== sock) return;
    if (u.qr) {
      state.status = "qr";
      state.qr = await QRCode.toDataURL(u.qr, { margin: 1, width: 320 });
      state.qrAt = Date.now();
      log("QR code ready — open «Чаты» → Подключение and scan it with the business phone");
    }
    if (u.connection === "open") {
      state.status = "connected";
      state.qr = null;
      state.error = null;
      state.since = Date.now();
      state.me = me.user ? { id: jidNormalizedUser(me.user.id), name: me.user.name || null } : null;
      log("connected as", state.me && state.me.id);
      post({ event: "status", status: "connected", me: state.me });
    }
    if (u.connection === "close") {
      const code = u.lastDisconnect && u.lastDisconnect.error && u.lastDisconnect.error.output
        ? u.lastDisconnect.error.output.statusCode : 0;
      const loggedOut = code === DisconnectReason.loggedOut;
      state.error = (u.lastDisconnect && u.lastDisconnect.error && u.lastDisconnect.error.message) || null;
      log("connection closed, code", code, state.error || "");
      if (loggedOut) {
        // unlinked from the phone: forget the session and show a fresh QR
        state.status = "logged_out";
        state.me = null;
        fs.rmSync(AUTH_DIR, { recursive: true, force: true });
        post({ event: "status", status: "logged_out" });
        reconnectTimer = setTimeout(connect, 2000);
      } else {
        state.status = "reconnecting";
        post({ event: "status", status: "reconnecting" });
        reconnectTimer = setTimeout(connect, code === DisconnectReason.restartRequired ? 500 : 5000);
      }
    }
  });

  me.ev.on("messages.upsert", async ({ messages, type }) => {
    for (const m of messages) {
      try {
        const msg = await parse(m, { download: true });
        if (msg) post({ event: "message", message: msg, notify: type === "notify" });
      } catch (e) {
        log("message parse failed", e.message);
      }
    }
  });

  // the phone's recent chats on first link, so the inbox doesn't start empty
  me.ev.on("messaging-history.set", async ({ messages, contacts }) => {
    const cutoff = Date.now() - HISTORY_DAYS * 86400000;
    const batch = [];
    for (const m of messages || []) {
      try {
        const msg = await parse(m, { download: false });
        if (msg && Date.parse(msg.at) >= cutoff) batch.push(msg);
      } catch (e) { /* skip */ }
    }
    const names = [];
    for (const c of contacts || []) {
      const nm = c.name || c.notify || c.verifiedName;
      if (c.id && nm && !isJidGroup(c.id)) names.push({ jid: await phoneJid(c.id, c.phoneNumber), name: nm, saved: !!c.name });
    }
    for (let i = 0; i < batch.length; i += 200) {
      post({ event: "history", messages: batch.slice(i, i + 200) });
    }
    if (names.length) post({ event: "contacts", contacts: names });
    if (batch.length) log("history:", batch.length, "messages");
  });

  me.ev.on("contacts.upsert", async (list) => {
    const names = [];
    for (const c of list || []) {
      const nm = c.name || c.notify || c.verifiedName;
      if (c.id && nm && !isJidGroup(c.id)) names.push({ jid: await phoneJid(c.id, c.phoneNumber), name: nm, saved: !!c.name });
    }
    if (names.length) post({ event: "contacts", contacts: names });
  });

  // ✓ / ✓✓ / blue ticks for what we sent
  me.ev.on("messages.update", (updates) => {
    for (const { key, update } of updates || []) {
      if (!key || !key.fromMe || update.status == null) continue;
      post({ event: "ack", id: key.id, status: STATUS[update.status] || "sent" });
    }
  });
}

// ---- HTTP API for the backend ---------------------------------------------------
function readJson(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (c) => {
      raw += c;
      if (raw.length > 1e6) reject(new Error("too large"));
    });
    req.on("end", () => {
      try { resolve(raw ? JSON.parse(raw) : {}); } catch (e) { reject(e); }
    });
  });
}

function reply(res, code, obj) {
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(JSON.stringify(obj));
}

function toJid(to) {
  if (!to) throw new Error("no recipient");
  if (to.includes("@")) return to;
  const digits = String(to).replace(/\D/g, "");
  if (digits.length < 7) throw new Error("bad phone number");
  return digits + "@s.whatsapp.net";
}

function requireOpen() {
  if (state.status !== "connected" || !sock) {
    const e = new Error("WhatsApp не подключён");
    e.code = 503;
    throw e;
  }
}

const server = http.createServer(async (req, res) => {
  const secret = req.headers["x-inbox-secret"] || "";
  const ok = secret.length === SECRET.length && crypto.timingSafeEqual(Buffer.from(secret), Buffer.from(SECRET));
  if (!ok) return reply(res, 403, { error: "forbidden" });
  try {
    if (req.method === "GET" && req.url === "/status") {
      return reply(res, 200, { ...state, queued: queue.length });
    }
    if (req.method === "POST" && req.url === "/check") {
      // does this number have WhatsApp? → its canonical jid
      requireOpen();
      const { phone } = await readJson(req);
      const digits = String(phone || "").replace(/\D/g, "");
      const [r] = (await sock.onWhatsApp(digits)) || [];
      return reply(res, 200, r && r.exists ? { exists: true, jid: jidNormalizedUser(r.jid) } : { exists: false });
    }
    if (req.method === "POST" && req.url === "/send") {
      requireOpen();
      const b = await readJson(req);
      const jid = toJid(b.jid);
      let content;
      if (b.media) {
        const file = path.join(MEDIA_DIR, path.basename(b.media));
        const buf = fs.readFileSync(file);
        const mime = b.mime || "application/octet-stream";
        if (mime.startsWith("image/") && mime !== "image/gif") content = { image: buf, caption: b.text || undefined, mimetype: mime };
        else if (mime.startsWith("video/")) content = { video: buf, caption: b.text || undefined, mimetype: mime };
        else if (mime.startsWith("audio/")) content = { audio: buf, mimetype: mime, ptt: !!b.voice };
        else content = { document: buf, mimetype: mime, fileName: b.file_name || path.basename(file), caption: b.text || undefined };
      } else {
        if (!b.text || !String(b.text).trim()) throw new Error("empty message");
        content = { text: String(b.text) };
      }
      const opts = {};
      if (b.quoted_id) {
        opts.quoted = {
          key: { remoteJid: jid, id: b.quoted_id, fromMe: !!b.quoted_from_me },
          message: { conversation: b.quoted_text || "" },
        };
      }
      const sent = await sock.sendMessage(jid, content, opts);
      return reply(res, 200, { id: sent.key.id, jid: jidNormalizedUser(sent.key.remoteJid || jid), at: tsOf(sent) });
    }
    if (req.method === "POST" && req.url === "/read") {
      if (state.status !== "connected") return reply(res, 200, { ok: false });
      const b = await readJson(req);
      const keys = (b.ids || []).map((id) => ({ remoteJid: toJid(b.jid), id, fromMe: false }));
      if (keys.length) await sock.readMessages(keys);
      return reply(res, 200, { ok: true });
    }
    if (req.method === "POST" && req.url === "/logout") {
      try { if (sock) await sock.logout(); } catch (e) { /* already gone */ }
      fs.rmSync(AUTH_DIR, { recursive: true, force: true });
      state.status = "logged_out";
      state.me = null;
      setTimeout(connect, 1500);
      return reply(res, 200, { ok: true });
    }
    return reply(res, 404, { error: "not found" });
  } catch (e) {
    log("api error", req.url, e.message);
    return reply(res, e.code === 503 ? 503 : 400, { error: e.message });
  }
});

server.on("error", (e) => {
  if (e.code === "EADDRINUSE") {
    log(`port ${PORT} is busy — the bridge is already running (or set WA_BRIDGE_PORT)`);
    process.exit(1);
  }
  throw e;
});

server.listen(PORT, "127.0.0.1", () => {
  log(`Nova WhatsApp bridge on http://127.0.0.1:${PORT} → ${BACKEND}`);
  connect().catch((e) => {
    state.status = "error";
    state.error = e.message;
    log("connect failed:", e.message);
    reconnectTimer = setTimeout(connect, 10000);
  });
});

process.on("unhandledRejection", (e) => log("unhandled:", e && e.message ? e.message : e));
