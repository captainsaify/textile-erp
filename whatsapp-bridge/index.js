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

// Link without a QR: WhatsApp > Linked devices > Link with phone number.
// Set to the bot number, digits only with country code (e.g. 919000000000).
// Needed when whoever is linking only has the phone in front of them and
// can't scan a code shown on another screen.
const PAIRING_NUMBER = (process.env.BRIDGE_PAIRING_NUMBER || '').replace(/[^0-9]/g, '');

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

// whatsapp-web.js hooks WhatsApp Web's internals, so a web-side rollout
// can break it outright -- on 2026-07-25 the build served from ~20:04
// (2.3000.1043849311+) failed Client.inject entirely. Pinning to a build
// this library is known to work with is the documented remedy; copies
// land in .wwebjs_cache as they are served, so a working one is usually
// already on disk. Set BRIDGE_WEB_VERSION='' to follow live WhatsApp Web.
const WEB_VERSION = process.env.BRIDGE_WEB_VERSION ?? '';
const webVersionCache = WEB_VERSION
  ? {
      webVersion: WEB_VERSION,
      webVersionCache: { type: 'local', path: './.wwebjs_cache' },
    }
  : {};

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: './session' }),
  puppeteer: {
    headless: true,
    executablePath: CHROME_PATH,
    args: ['--no-sandbox', '--disable-setuid-sandbox'],
    // whatsapp-web.js's injection step evaluates a lot of code in-page;
    // on a cold profile it can exceed Puppeteer's 30s protocol default
    // and abort the whole startup.
    protocolTimeout: 180000,
  },
  ...webVersionCache,
});

let pairingRequested = false;
client.on('qr', async (qr) => {
  if (PAIRING_NUMBER && !pairingRequested) {
    pairingRequested = true;
    try {
      const code = await client.requestPairingCode(PAIRING_NUMBER);
      console.log(`PAIRING CODE: ${code}`);
      console.log('Enter it in WhatsApp > Linked devices > Link with phone number instead');
      return;
    } catch (err) {
      console.error('pairing code request failed, falling back to QR:', err.message);
    }
  }
  qrcode.generate(qr, { small: true });
  console.log('Scan this QR with the bot phone: WhatsApp > Linked devices > Link a device');
});

// Startup visibility: without these, a client stuck part-way through
// loading looks identical to one that simply hasn't finished.
client.on('loading_screen', (percent, message) =>
  console.log(`whatsapp loading ${percent}% ${message || ''}`),
);
client.on('authenticated', () => console.log('whatsapp authenticated (session restored)'));
client.on('change_state', (state) => console.log(`whatsapp state: ${state}`));

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

// WhatsApp now carries the real phone number alongside the LID in a
// few different places depending on message shape; check them all
// before falling back to a contact lookup.
function phoneFromMessage(msg) {
  const data = msg._data || {};
  const candidates = [
    data.senderPn,
    data.participantPn,
    data.authorPn,
    data.from?._serialized,
    data.author?._serialized,
    msg.id?.participant?._serialized,
    msg.id?.participant,
  ];
  for (const candidate of candidates) {
    const value = typeof candidate === 'string' ? candidate : candidate?._serialized;
    if (typeof value === 'string' && value.endsWith('@c.us')) return value;
  }
  return null;
}

async function resolveSenderJid(jid, msg) {
  if (!jid || !jid.endsWith('@lid')) return jid;

  const fromMessage = msg ? phoneFromMessage(msg) : null;
  if (fromMessage) {
    if (!lidPhoneCache.has(jid)) console.log(`sender lid mapped (msg): ${jid} -> ${fromMessage}`);
    lidPhoneCache.set(jid, fromMessage);
    return fromMessage;
  }

  if (lidPhoneCache.has(jid)) return lidPhoneCache.get(jid);
  let resolved = jid;
  try {
    const contact = await client.getContactById(jid);
    if (contact && contact.number) {
      resolved = `${String(contact.number).replace(/[^0-9]/g, '')}@c.us`;
    } else if (contact?.id?._serialized && String(contact.id._serialized).endsWith('@c.us')) {
      resolved = contact.id._serialized;
    }
  } catch (err) {
    console.log(`sender lid resolution failed for ${jid}:`, err.message);
  }
  if (resolved === jid) {
    // Surface the shape so an unmapped sender is diagnosable rather than
    // just silently unauthorized.
    console.log(
      `sender lid UNMAPPED ${jid}; msg id keys=${JSON.stringify(Object.keys(msg?._data || {}).filter((k) => /pn|phone|author|sender|participant/i.test(k)))}`,
    );
  } else {
    console.log(`sender lid mapped (contact): ${jid} -> ${resolved}`);
  }
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
  const messageId =
    (msg.id && msg.id._serialized) || `noid_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  const sender = msg.fromMe
    ? ownId || msg.from
    : await resolveSenderJid(msg.author || msg.from, msg);

  // photos/PDFs take the OCR path -- download and relay the bytes
  if (msg.hasMedia && (msg.type === 'image' || msg.type === 'document')) {
    try {
      // Media isn't always decryptable from the object the event hands
      // us -- for our own sent messages the store entry can still be
      // settling -- so retry, and re-fetch the message from the chat
      // before the later attempts.
      let media = null;
      let lastErr = null;
      for (const [attempt, delayMs] of [0, 1500, 3000, 5000].entries()) {
        if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
        let target = msg;
        if (attempt > 0) {
          try {
            const chat = await msg.getChat();
            const recent = await chat.fetchMessages({ limit: 5 });
            const fresh = recent.find(
              (m) => m.hasMedia && (m.id?._serialized === msg.id?._serialized || m.body === msg.body),
            );
            if (fresh) target = fresh;
          } catch (err) {
            lastErr = err;
          }
        }
        try {
          media = await target.downloadMedia();
          if (media && media.data) break;
          media = null;
        } catch (err) {
          lastErr = err;
        }
      }
      if (!media) {
        const detail = lastErr
          ? JSON.stringify({
              name: lastErr.name,
              message: lastErr.message,
              stack: (lastErr.stack || '').split('\n').slice(0, 4).join(' | '),
            })
          : 'empty payload';
        console.error(`media download failed for ${messageId}: ${detail}`);
        return;
      }
      const res = await fetch(`${BACKEND_URL}/internal/whatsapp-bridge/media`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Bridge-Secret': SHARED_SECRET },
        body: JSON.stringify({
          message_id: messageId,
          chat_id: chatId,
          sender,
          is_group: chatId.endsWith('@g.us'),
          mime_type: media.mimetype || 'image/jpeg',
          filename: media.filename || null,
          data_base64: media.data,
        }),
      });
      if (!res.ok) console.error(`backend rejected media ${messageId}: ${res.status}`);
    } catch (err) {
      console.error('media relay failed:', err.message);
    }
    return;
  }

  const payload = {
    message_id: messageId,
    chat_id: chatId,
    sender,
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
    if (!res.ok) console.error(`backend rejected message ${messageId}: ${res.status}`);
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
