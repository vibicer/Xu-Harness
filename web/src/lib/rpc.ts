/** WebSocket JSON-RPC client for the Xu brain (contract/methods.md). */

export type EventHandler = (event: string, params: Record<string, unknown>) => void;

interface RpcEnvelope {
  jsonrpc: "2.0";
  id: number | string | null;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

interface RpcNotification {
  jsonrpc: "2.0";
  method: string;
  params?: Record<string, unknown>;
}

export class RpcError extends Error {
  code: number;
  data?: unknown;
  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.name = "RpcError";
    this.code = code;
    this.data = data;
  }
}

interface Pending {
  resolve: (value: unknown) => void;
  reject: (err: RpcError) => void;
}

type RpcMessage = RpcEnvelope | RpcNotification;

export class BrainClient {
  private ws: WebSocket | null = null;
  private url: string;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private handlers = new Set<EventHandler>();
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private closedByUs = false;

  connected = false;
  onStatusChange?: (connected: boolean) => void;

  constructor(url?: string) {
    this.url = url ?? this.deriveUrl();
  }

  private deriveUrl(): string {
    if (typeof location === "undefined") return "ws://127.0.0.1:9876";
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    // Brain WS runs on port 9876, or XU_BRAIN_PORT if set server-side.
    // When served by the brain's web server, derive WS host from page host.
    const host = location.hostname;
    return `${proto}//${host}:9876`;
  }

  connect(): void {
    this.closedByUs = false;
    const ws = new WebSocket(this.url);
    this.ws = ws;

    ws.onopen = () => {
      this.connected = true;
      this.onStatusChange?.(true);
    };

    ws.onmessage = (ev: MessageEvent) => {
      let msg: RpcMessage;
      try {
        msg = JSON.parse(String(ev.data)) as RpcMessage;
      } catch {
        return;
      }
      if (
        typeof msg === "object" &&
        "id" in msg &&
        typeof msg.id === "number" &&
        this.pending.has(msg.id)
      ) {
        const p = this.pending.get(msg.id)!;
        this.pending.delete(msg.id);
        const envelope = msg as RpcEnvelope;
        if (envelope.error) p.reject(new RpcError(envelope.error.code, envelope.error.message, envelope.error.data));
        else p.resolve(envelope.result);
      } else if ("method" in msg && msg.method === "event") {
        const { event, ...params } = (msg as RpcNotification).params ?? {};
        if (event) this.handlers.forEach((h) => h(String(event), params));
      }
    };

    ws.onclose = () => {
      this.connected = false;
      this.onStatusChange?.(false);
      this.pending.forEach((p) => p.reject(new RpcError(-32000, "brain connection closed")));
      this.pending.clear();
      if (!this.closedByUs) this.scheduleReconnect();
    };

    ws.onerror = () => ws.close();
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined;
      this.connect();
    }, 1500);
  }

  close(): void {
    this.closedByUs = true;
    clearTimeout(this.reconnectTimer);
    this.ws?.close();
  }

  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new RpcError(-32000, "brain not connected"));
    }
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (v: unknown) => resolve(v as T),
        reject,
      });
      this.ws!.send(JSON.stringify({ jsonrpc: "2.0", id, method, params: params ?? {} }));
    });
  }

  onEvent(handler: EventHandler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }
}