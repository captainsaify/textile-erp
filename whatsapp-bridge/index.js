// whatsapp-web.js relay: WhatsApp <-> ERP backend.
//
// Inbound: every message (1:1 or group) is POSTed to the backend's
// /internal/whatsapp-bridge/messages with a shared secret; the backend
// does all the thinking (sender allowlist, permissions, commands).
// Outbound: the backend POSTs /send here to deliver replies.
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

client.on('ready', () => console.log(`whatsapp connected as ${client.info.wid.user}`));
client.on('auth_failure', (msg) => console.error('whatsapp auth failure:', msg));
client.on('disconnected', (reason) => console.error('whatsapp disconnected:', reason));

client.on('message', async (msg) => {
  if (msg.fromMe || msg.from === 'status@broadcast') return;
  const payload = {
    message_id: msg.id._serialized,
    chat_id: msg.from,
    sender: msg.author || msg.from,
    is_group: msg.from.endsWith('@g.us'),
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
    await client.sendMessage(toChatId(chatId), body);
    return res.json({ status: 'sent' });
  } catch (err) {
    console.error('send failed:', err.message);
    return res.status(502).json({ error: 'send_failed' });
  }
});

app.listen(PORT, '127.0.0.1', () => console.log(`bridge listening on 127.0.0.1:${PORT}`));
client.initialize();
