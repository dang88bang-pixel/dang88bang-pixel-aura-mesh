/**
 * WebSocket client for the visualiser.
 *
 * Talks to *this* server's /ws endpoint, never to the agent directly: the
 * browser is frequently on a different host than the mesh, and same-origin
 * relative URLs are the only thing guaranteed to work behind the preview
 * proxy, a corporate reverse proxy or an Android WebView.
 */
export class DataFetcher {
  constructor({ onTelemetry, onStatus } = {}) {
    this.onTelemetry = onTelemetry ?? (() => {});
    this.onStatus = onStatus ?? (() => {});
    this.socket = null;
    this.reconnectDelay = 500;
    this.shouldReconnect = true;
    this.frames = 0;
    this.lastFrameAt = 0;
    this.fps = 0;
  }

  connect() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}/ws`;
    this.onStatus({ state: 'connecting', url });

    try {
      this.socket = new WebSocket(url);
    } catch (error) {
      this.#scheduleReconnect(error.message);
      return;
    }

    this.socket.onopen = () => {
      this.reconnectDelay = 500;
      this.onStatus({ state: 'connected', url });
    };

    this.socket.onmessage = (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (payload.type === 'telemetry') {
        const now = performance.now();
        if (this.lastFrameAt) {
          const instant = 1000 / Math.max(1, now - this.lastFrameAt);
          this.fps = this.fps * 0.85 + instant * 0.15;
        }
        this.lastFrameAt = now;
        this.frames++;
        this.onTelemetry(payload);
      } else {
        this.onStatus({ state: 'message', payload });
      }
    };

    this.socket.onclose = () => {
      this.onStatus({ state: 'disconnected' });
      this.#scheduleReconnect('closed');
    };

    this.socket.onerror = () => {
      this.onStatus({ state: 'error' });
    };
  }

  #scheduleReconnect(reason) {
    if (!this.shouldReconnect) return;
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, 10000);
    this.onStatus({ state: 'reconnecting', reason, delay: this.reconnectDelay });
    setTimeout(() => this.connect(), this.reconnectDelay);
  }

  send(command, payload = {}) {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ command, payload }));
      return true;
    }
    return false;
  }

  async rest(path, options = {}) {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}`);
    return response.json();
  }

  disconnect() {
    this.shouldReconnect = false;
    this.socket?.close();
  }
}
