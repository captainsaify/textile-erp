// whatsapp-web.js relay: WhatsApp <-> ERP backend.
//
// Inbound: every message (1:1 or group) is POSTed to the backend's
// /internal/whatsapp-bridge/messages with a shared secret; the backend
// does all the thinking (sender allowlist, permissions, commands).
// Outbound: the backend POSTs /send here to deliver replies.
//
// Self-bot mode: when the bridge is logged in with a person's own
// number, their messages arrive as fromMe. Those are processed in the
// self-chat ("Message yourself") always, and in BRIDGE_ALLOWED_CHATS
// if configured -- never in other chats, so normal conversations can't
// trigger commands. Replies the bridge itself sends are loop-guarded.
//
// Login: first run prints a QR code -- scan it from the *bot's* phone
// (WhatsApp > Linked devices). The session persists in ./session so
// restarts don't need a rescan.

const crypto = require('crypto');
const fs = require('fs');
const express = require('express');
const qrcode = require('qrcode-terminal');
const { Client, LocalAuth } = require('whatsapp-web.js');

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';
const SHARED_SECRET = process.env.BRIDGE_SHARED_SECRET || '';
const PORT = parseInt(process.env.BRIDGE_PORT || '3001', 10);

// Puppeteer's downloaded Chrome-for-Testing is blocked by Gatekeeper on
// macOS; prefer the real installed Chrome, overridable via env.
const DEFAULT_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const CHROME_PATH =
  process.env.BRIDGE_CHROME_PATH || (fs.existsSync(DEFAULT_CHROME) ? DEFAULT_CHROME : undefined);

// Optional containment: comma-separated chat ids (e.g. the business
// group's ...@g.us). When set, ONLY these chats plus the self-chat are
// relayed at all. When unset: others' messages relay from any chat;
// own (fromMe) messages relay only from the self-chat.
const ALLOWED_CHATS = (process.env.BRIDGE_ALLOWED_CHATS || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

if (!SHARED_SECRET) {
  console.error('BRIDGE_SHARED_SECRET is required (same value as the backend .env)');
  process.exit(1);
}

function secretMatches(presented) {
  if (typeof presented !== 'string') return false;
  const a = Buffer.from(SHARED_SECRET);
  const b = Buffer.from(presented);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// E.164 from the backend ("+9198...") -> web.js chat id ("9198...@c.us")
function toChatId(value) {
  if (value.includes('@')) return value;
  return `${value.replace(/^\+/, '')}@c.us`;
}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: './session' }),
  puppeteer: {
    headless: true,
    executablePath: CHROME_PATH,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
  },
});

client.on('qr', (qr) => {
  qrcode.generate(qr, { small: true });
  console.log('Scan this QR with the bot phone: WhatsApp > Linked devices > Link a device');
});

let ownId = null;
let ownLid = null; // WhatsApp's privacy id for our own account (...@lid)
const knownForeignLids = new Set();
client.on('ready', async () => {
  ownId = client.info.wid._serialized;
  console.log(`whatsapp connected as ${client.info.wid.user}`);
  try {
    const me = await client.getContactById(ownId);
    if (me && me.lid) {
      ownLid = typeof me.lid === 'string' ? me.lid : me.lid._serialized;
      console.log(`own lid resolved: ${ownLid}`);
    }
  } catch (err) {
    console.log('own lid lookup failed (will resolve lazily):', err.message);
  }
});

// The self-chat may appear under the phone JID or the LID, and which of
// the two shows up in from/to varies by which device typed the message.
async function isSelfChat(msg) {
  if (!msg.fromMe) return false;
  if (msg.from === msg.to) return true;
  const to = msg.to || '';
  if (to === ownId || (ownLid !== null && to === ownLid)) return true;
  if (!to.endsWith('@lid') || knownForeignLids.has(to)) return false;
  try {
    const contact = await client.getContactById(to);
    if (contact && contact.isMe) {
      ownLid = to;
      console.log(`own lid resolved lazily: ${ownLid}`);
      return true;
    }
  } catch (err) {
    console.log(`lid contact lookup failed for ${to}:`, err.message);
  }
  knownForeignLids.add(to);
  return false;
}

// Group messages identify the author by LID under WhatsApp's privacy
// addressing; the backend allowlist is keyed by phone number, so map
// LID -> phone JID via contact lookup (cached).
const lidPhoneCache = new Map();
async function resolveSenderJid(jid) {
  if (!jid || !jid.endsWith('@lid')) return jid;
  if (lidPhoneCache.has(jid)) return lidPhoneCache.get(jid);
  let resolved = jid;
  try {
    const contact = await client.getContactById(jid);
    if (contact && contact.number) resolved = `${contact.number}@c.us`;
    else if (contact && contact.id && String(contact.id._serialized).endsWith('@c.us'))
      resolved = contact.id._serialized;
  } catch (err) {
    console.log(`sender lid resolution failed for ${jid}:`, err.message);
  }
  if (resolved !== jid) console.log(`sender lid mapped: ${jid} -> ${resolved}`);
  lidPhoneCache.set(jid, resolved);
  return resolved;
}

// Loop guard: replies this bridge sends also fire message_create as
// fromMe -- without this they'd be fed back into the backend forever.
const pendingSends = new Map(); // "chatId|body" -> count
function markPendingSend(chatId, body) {
  const key = `${chatId}|${body}`;
  pendingSends.set(key, (pendingSends.get(key) || 0) + 1);
  setTimeout(() => {
    const n = pendingSends.get(key);
    if (n > 1) pendingSends.set(key, n - 1);
    else pendingSends.delete(key);
  }, 60_000).unref();
}
function isPendingSend(chatId, body) {
  const key = `${chatId}|${body}`;
  const n = pendingSends.get(key);
  if (!n) return false;
  if (n > 1) pendingSends.set(key, n - 1);
  else pendingSends.delete(key);
  return true;
}

function chatAllowed(chatId, fromMe, isSelfChat) {
  const self = isSelfChat || (ownId !== null && chatId === ownId);
  if (ALLOWED_CHATS.length > 0) return self || ALLOWED_CHATS.includes(chatId);
  return fromMe ? self : true;
}
client.on('auth_failure', (msg) => console.error('whatsapp auth failure:', msg));
client.on('disconnected', (reason) => console.error('whatsapp disconnected:', reason));

client.on('message_create', async (msg) => {
  if (msg.from === 'status@broadcast' || msg.to === 'status@broadcast') return;
  console.log(`evt message_create type=${msg.type} fromMe=${msg.fromMe} from=${msg.from} to=${msg.to}`);
  // WhatsApp increasingly addresses chats by LID (privacy id, ...@lid)
  // instead of the phone JID. Normalize the self-chat to the phone JID
  // so the backend replies via the form sendMessage reliably accepts.
  const selfChat = await isSelfChat(msg);
  let chatId = msg.fromMe ? msg.to : msg.from;
  if (selfChat && ownId) chatId = ownId;
  if (msg.fromMe && msg.type === 'chat' && isPendingSend(chatId, msg.body)) return;
  if (!chatAllowed(chatId, msg.fromMe, selfChat)) {
    if (chatId.endsWith('@g.us')) console.log(`skipped chat ${chatId} (not in BRIDGE_ALLOWED_CHATS)`);
    return;
  }
  const payload = {
    message_id: (msg.id && msg.id._serialized) || `noid_${Date.now()}_${Math.random().toString(36).slice(2)}`,
    chat_id: chatId,
    sender: msg.fromMe ? ownId || msg.from : await resolveSenderJid(msg.author || msg.from),
    is_group: chatId.endsWith('@g.us'),
    kind: msg.type,
    body: msg.type === 'chat' ? msg.body : null,
  };
  try {
    const res = await fetch(`${BACKEND_URL}/internal/whatsapp-bridge/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': SHARED_SECRET },
      body: JSON.stringify(payload),
    });
    if (!res.ok) console.error(`backend rejected message ${msg.id._serialized}: ${res.status}`);
  } catch (err) {
    console.error('backend unreachable:', err.message);
  }
});

const app = express();
app.use(express.json());

app.get('/healthz', (_req, res) => {
  res.json({ status: 'ok', whatsapp: client.info ? 'connected' : 'connecting' });
});

app.post('/send', async (req, res) => {
  if (!secretMatches(req.get('X-Bridge-Secret'))) {
    return res.status(401).json({ error: 'unauthorized' });
  }
  const { chat_id: chatId, body } = req.body || {};
  if (!chatId || !body) {
    return res.status(400).json({ error: 'chat_id and body are required' });
  }
  try {
    const target = toChatId(chatId);
    markPendingSend(target, body);
    await client.sendMessage(target, body);
    return res.json({ status: 'sent' });
  } catch (err) {
    console.error('send failed:', err.stack || err.message);
    return res.status(502).json({ error: 'send_failed', detail: String(err.message) });
  }
});

app.listen(PORT, '127.0.0.1', () => console.log(`bridge listening on 127.0.0.1:${PORT}`));
client.initialize();
