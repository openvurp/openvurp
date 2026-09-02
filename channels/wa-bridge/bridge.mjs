// Il ponte: parla il protocollo di WhatsApp Web (Baileys) e traduce tutto in
// righe JSON su stdout/stdin. Il cervello sta in Python — qui SOLO trasporto:
// e' la stessa regola degli altri canali, un ponte che decide e' un secondo
// cervello che invecchia male.
//
// In uscita:  {"type":"qr","dataurl":...} | {"type":"open","me":...}
//             {"type":"close","code":...} | {"type":"loggedout"}
//             {"type":"message","from":jid,"name":...,"text":...}
// In entrata: {"type":"send","to":jid,"text":...}

import {
  makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion,
  DisconnectReason,
} from "@whiskeysockets/baileys";
import QRCode from "qrcode";
import readline from "readline";

const AUTH_DIR = process.argv[2];
if (!AUTH_DIR) { console.error("uso: node bridge.mjs <cartella-auth>"); process.exit(2); }
const out = (o) => process.stdout.write(JSON.stringify(o) + "\n");

let sock = null;

async function avvia() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion().catch(() => ({ version: undefined }));
  sock = makeWASocket({
    auth: state, version, printQRInTerminal: false,
    browser: ["openvurp", "Chrome", "1.0"],
  });
  sock.ev.on("creds.update", saveCreds);
  sock.ev.on("connection.update", async (u) => {
    if (u.qr) out({ type: "qr", dataurl: await QRCode.toDataURL(u.qr) });
    if (u.connection === "open") out({ type: "open", me: sock.user?.id || "" });
    if (u.connection === "close") {
      const code = u.lastDisconnect?.error?.output?.statusCode;
      out({ type: "close", code });
      // La sessione scaduta non si riapre da sola: serve un nuovo QR, e va
      // detto. Tutto il resto (rete che balla) si riaggancia in silenzio.
      if (code === DisconnectReason.loggedOut) out({ type: "loggedout" });
      else setTimeout(() => avvia().catch(() => {}), 2000);
    }
  });
  sock.ev.on("messages.upsert", ({ messages, type }) => {
    if (type !== "notify") return;
    for (const m of messages) {
      if (m.key.fromMe) continue;
      const jid = m.key.remoteJid || "";
      // Solo chat private: un bot nei gruppi WhatsApp e' un altro progetto.
      if (jid.endsWith("@g.us") || jid === "status@broadcast") continue;
      const testo = m.message?.conversation
        || m.message?.extendedTextMessage?.text || "";
      if (!testo) continue;
      out({ type: "message", from: jid, name: m.pushName || "", text: testo });
    }
  });
}

readline.createInterface({ input: process.stdin }).on("line", (riga) => {
  try {
    const c = JSON.parse(riga);
    if (c.type === "send" && sock) sock.sendMessage(c.to, { text: String(c.text || "") });
  } catch { /* una riga rotta non ferma il ponte */ }
});

avvia().catch((e) => { out({ type: "fatal", error: String(e) }); process.exit(1); });
