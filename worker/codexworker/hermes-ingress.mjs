import http from "node:http";
import net from "node:net";

export const HERMES_PROFILE = "dsh-compatible-desktop-native";
export const peerAddress = address => address?.replace(/^::ffff:/, "") ?? "";
const privateIPv4 = address => net.isIP(address) === 4 && (address.startsWith("127.") ||
  address.startsWith("10.") || address.startsWith("192.168.") ||
  (address.startsWith("172.") && Number(address.split(".")[1]) >= 16 && Number(address.split(".")[1]) <= 31));

// An explicitly configured, limited native-messaging surface; not a manager
// fallback and never a second conversation backend. No forwarded header trust.
export class HermesIngress {
  constructor(manager, config) {
    this.manager = manager; this.config = config; this.peerRequests = 0;
    if (!privateIPv4(config.host) || !Number.isInteger(config.port) || config.port < 0 || config.port > 65535 ||
      typeof config.token !== "string" || config.token.length < 24 ||
      !Array.isArray(config.allowedRemoteAddresses) || !config.allowedRemoteAddresses.length ||
      !config.allowedRemoteAddresses.every(privateIPv4)) {
      throw new Error("Hermes ingress requires an explicit private IPv4 bind, exact peer allowlist and a strong token.");
    }
  }

  async start() {
    this.server = http.createServer((req, res) => {
      const address = peerAddress(req.socket.remoteAddress);
      if (!this.config.allowedRemoteAddresses.includes(address)) {
        res.writeHead(403, { "content-type": "application/json", connection: "close" });
        res.end(JSON.stringify({ error: "peer_not_allowed", detail: "This peer is not allowed on the Hermes native-messaging ingress.", retryable: false })); return;
      }
      void this.manager.handle(req, res, { token: this.config.token, hermesIngress: this, peerAddress: address });
    });
    this.server.headersTimeout = 10000; this.server.requestTimeout = 15000;
    await new Promise((resolve, reject) => {
      this.server.once("error", reject); this.server.listen(this.config.port, this.config.host, resolve);
    });
    this.config.port = this.server.address().port;
  }

  recordPeer(address) {
    if (address === this.config.host || address.startsWith("127.")) return;
    this.peerRequests++; this.lastPeerRequestAt = new Date().toISOString();
  }

  recordDelivery(result) {
    this.lastAcceptedMessageId = result.messageId;
    this.lastAcceptedThreadId = result.session?.threadId ?? null;
  }

  status() {
    return { enabled: true, listening: Boolean(this.server?.listening), profile: HERMES_PROFILE,
      host: this.config.host, port: this.config.port, peerRequests: this.peerRequests,
      lastPeerRequestAt: this.lastPeerRequestAt ?? null,
      lastAcceptedMessageId: this.lastAcceptedMessageId ?? null,
      lastAcceptedThreadId: this.lastAcceptedThreadId ?? null };
  }

  async close() {
    if (!this.server?.listening) return;
    this.server.closeIdleConnections();
    await new Promise(resolve => this.server.close(resolve));
  }
}
