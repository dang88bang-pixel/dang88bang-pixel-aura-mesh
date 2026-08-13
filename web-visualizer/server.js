/**
 * AURA 6.0 visualiser host.
 *
 * Serves the Babylon.js bundle and bridges the browser to the Python edge
 * agent. The browser must never talk to the agent directly: in the field the
 * agent sits on a private mesh address the operator's phone cannot resolve,
 * and in a hosted preview the browser is not even on the same machine. So
 * everything goes through this process over same-origin relative URLs.
 */

import express from 'express';
import http from 'http';
import path from 'path';
import { fileURLToPath } from 'url';
import { WebSocketServer, WebSocket } from 'ws';
import { createProxyMiddleware } from 'http-proxy-middleware';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || '0.0.0.0';
const AGENT_HTTP = process.env.AGENT_URL || 'http://127.0.0.1:8080';
const AGENT_WS = AGENT_HTTP.replace(/^http/, 'ws') + '/ws/agent/events';
const AGENT_TOKEN = process.env.AGENT_TOKEN || '';

const app = express();
app.disable('x-powered-by');

// The preview runs behind a proxy that terminates TLS on another host.
app.set('trust proxy', true);

// NOTE: the JSON body parser is mounted *after* the /api proxy on purpose.
// If it ran first it would consume the request stream, and every proxied POST
// would hang until the agent timed out ("socket hang up" on the client).

// ---------------------------------------------------------------------
// static assets
// ---------------------------------------------------------------------
app.use(express.static(path.join(__dirname, 'public'), {
  etag: true,
  maxAge: '1h',
  setHeaders(res, filePath) {
    if (filePath.endsWith('.html')) res.setHeader('Cache-Control', 'no-cache');
  },
}));
app.use('/src', express.static(path.join(__dirname, 'src'), { maxAge: '1h' }));
// Babylon ships as ES modules; serve them straight from node_modules so the
// browser can use native import maps without a bundler step.
app.use('/vendor/babylonjs', express.static(path.join(__dirname, 'node_modules/@babylonjs')));

// ---------------------------------------------------------------------
// health & config
// ---------------------------------------------------------------------
app.get('/healthz', (_req, res) => {
  res.json({
    status: 'ok',
    service: 'aura-visualizer',
    version: '6.0.0',
    agent: AGENT_HTTP,
    agentReachable: lastAgentState.reachable,
    clients: wss ? wss.clients.size : 0,
    uptime: Math.round(process.uptime()),
  });
});

/** Runtime config handed to the browser so nothing is hard-coded client-side. */
app.get('/config.json', (_req, res) => {
  res.json({
    wsPath: '/ws',
    demoMode: DEMO_MODE,
    agent: lastAgentState.reachable ? 'connected' : 'demo',
    palette: PALETTE,
  });
});

// ---------------------------------------------------------------------
// REST proxy to the edge agent
// ---------------------------------------------------------------------
app.use('/api', createProxyMiddleware({
  target: AGENT_HTTP,
  changeOrigin: true,
  timeout: 10_000,
  proxyTimeout: 10_000,
  // Express strips the '/api' mount prefix before the proxy sees the request,
  // but the agent's routes are all under /api/v1/... - put it back.
  pathRewrite: (path) => `/api${path}`,
  on: {
    proxyReq(proxyReq) {
      if (AGENT_TOKEN) proxyReq.setHeader('Authorization', `Bearer ${AGENT_TOKEN}`);
    },
    error(err, _req, res) {
      // Never let a dead agent surface as an unhandled proxy crash; the UI
      // falls back to the built-in demo scene instead.
      lastAgentState.reachable = false;
      if (res && typeof res.status === 'function' && !res.headersSent) {
        res.status(503).json({ error: 'edge agent unreachable', detail: err.message });
      }
    },
  },
}));

// Body parsing for this server's own routes only - mounted after the proxy.
app.use(express.json({ limit: '2mb' }));

const server = http.createServer(app);

// ---------------------------------------------------------------------
// WebSocket bridge (browser <-> agent), with a demo fallback
// ---------------------------------------------------------------------
const wss = new WebSocketServer({ server, path: '/ws' });
const lastAgentState = { reachable: false, lastFrame: null, lastError: '' };
const DEMO_MODE = process.env.DEMO_MODE !== '0';

let agentSocket = null;
let reconnectDelay = 500;
let reconnectTimer = null;

function broadcast(payload) {
  const message = typeof payload === 'string' ? payload : JSON.stringify(payload);
  for (const client of wss.clients) {
    if (client.readyState === WebSocket.OPEN) {
      client.send(message);
    }
  }
}

function connectAgent() {
  clearTimeout(reconnectTimer);
  const url = AGENT_TOKEN ? `${AGENT_WS}?token=${encodeURIComponent(AGENT_TOKEN)}` : AGENT_WS;
  let socket;
  try {
    socket = new WebSocket(url);
  } catch (err) {
    scheduleReconnect(err.message);
    return;
  }
  agentSocket = socket;

  socket.on('open', () => {
    lastAgentState.reachable = true;
    reconnectDelay = 500;
    console.log(`[bridge] connected to agent at ${AGENT_WS}`);
    broadcast({ type: 'bridge', status: 'connected' });
  });

  socket.on('message', (data) => {
    const text = data.toString();
    lastAgentState.lastFrame = Date.now();
    broadcast(text);
  });

  socket.on('close', () => {
    lastAgentState.reachable = false;
    broadcast({ type: 'bridge', status: 'disconnected' });
    scheduleReconnect('closed');
  });

  socket.on('error', (err) => {
    lastAgentState.reachable = false;
    lastAgentState.lastError = err.message;
    scheduleReconnect(err.message);
  });
}

function scheduleReconnect(reason) {
  if (agentSocket) {
    agentSocket.removeAllListeners();
    try { agentSocket.terminate(); } catch { /* already dead */ }
    agentSocket = null;
  }
  clearTimeout(reconnectTimer);
  // Exponential backoff capped at 15 s so a missing agent does not spam logs.
  reconnectDelay = Math.min(reconnectDelay * 2, 15_000);
  reconnectTimer = setTimeout(connectAgent, reconnectDelay);
  if (reconnectDelay <= 2000) console.warn(`[bridge] agent unreachable (${reason})`);
}

wss.on('connection', (client) => {
  client.send(JSON.stringify({
    type: 'hello',
    source: 'visualizer',
    version: '6.0.0',
    agentConnected: lastAgentState.reachable,
    demoMode: DEMO_MODE && !lastAgentState.reachable,
  }));

  client.on('message', (data) => {
    // Forward UI commands upstream; ignore them when running on demo data.
    if (agentSocket && agentSocket.readyState === WebSocket.OPEN) {
      agentSocket.send(data.toString());
    }
  });
});

// ---------------------------------------------------------------------
// demo scene generator
// ---------------------------------------------------------------------
// Without this the visualiser is a black screen whenever the Python agent is
// not running, which makes the UI impossible to develop or demo on its own.
import { DemoSimulator } from './src/demo-simulator.js';

const demo = new DemoSimulator();
setInterval(() => {
  if (lastAgentState.reachable || !DEMO_MODE) return;
  if (wss.clients.size === 0) return;
  broadcast(demo.step());
}, 100);

const PALETTE = {
  structure: '#6A7A8A',
  person: '#00FF88',
  device: '#FFCC00',
  hazard: '#FF4444',
  exit: '#FF8800',
  background: '#0A0D14',
};

server.listen(PORT, HOST, () => {
  console.log(`AURA 6.0 visualiser on http://${HOST}:${PORT}`);
  console.log(`  edge agent : ${AGENT_HTTP}`);
  console.log(`  demo mode  : ${DEMO_MODE ? 'enabled (used when the agent is down)' : 'disabled'}`);
  connectAgent();
});

const shutdown = () => {
  console.log('\nshutting down');
  clearTimeout(reconnectTimer);
  if (agentSocket) try { agentSocket.terminate(); } catch { /* noop */ }
  wss.close();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 2000).unref();
};
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
