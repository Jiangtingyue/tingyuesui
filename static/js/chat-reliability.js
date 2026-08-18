/** Recoverable, idempotent chat request state. No UI or model logic lives here. */
(function (global) {
  'use strict';

  const STORAGE_KEY = 'daxigua:pending-chat-request:v1';
  const TERMINAL = new Set(['completed', 'interrupted', 'failed', 'blocked']);

  function read() {
    try {
      const parsed = JSON.parse(global.localStorage.getItem(STORAGE_KEY) || 'null');
      return parsed && typeof parsed === 'object' ? parsed : null;
    } catch (_) {
      return null;
    }
  }

  function write(value) {
    try {
      if (!value) {
        global.localStorage.removeItem(STORAGE_KEY);
        return null;
      }
      global.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
      return value;
    } catch (_) {
      return null;
    }
  }

  function createId() {
    if (global.crypto?.randomUUID) return global.crypto.randomUUID();
    const random = global.crypto?.getRandomValues
      ? Array.from(global.crypto.getRandomValues(new Uint8Array(12)), (n) => n.toString(16).padStart(2, '0')).join('')
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
    return `req_${random}`;
  }

  function begin(payload, requestedId = '') {
    const item = {
      client_request_id: requestedId || createId(),
      session_id: String(payload?.session_id || ''),
      created_at_ms: Date.now(),
      acknowledged: false,
      payload: payload || null,
    };
    return write(item);
  }

  function acknowledge(clientRequestId, receipt = {}) {
    const item = read();
    if (!item || item.client_request_id !== clientRequestId) return item;
    item.acknowledged = true;
    item.trace_id = String(receipt.trace_id || '');
    item.user_message_id = Number(receipt.user_message_id || 0) || null;
    // Once the server owns the user message, the browser no longer retains its text.
    item.payload = null;
    return write(item);
  }

  function clear(clientRequestId = '') {
    const item = read();
    if (!item || (clientRequestId && item.client_request_id !== clientRequestId)) return false;
    write(null);
    return true;
  }

  async function status(clientRequestId) {
    const response = await global.fetch(
      `/api/chat/requests/${encodeURIComponent(clientRequestId)}`,
      { cache: 'no-store' },
    );
    if (response.status === 404) return { status: 'missing', terminal: false };
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    return data;
  }

  async function reconcile() {
    const pending = read();
    if (!pending) return { status: 'none', terminal: true };
    const result = await status(pending.client_request_id);
    if (TERMINAL.has(result.status)) clear(pending.client_request_id);
    return { ...result, pending };
  }

  function cancel(clientRequestId) {
    if (!clientRequestId) return Promise.resolve(null);
    return global.fetch(
      `/api/chat/requests/${encodeURIComponent(clientRequestId)}/cancel`,
      { method: 'POST', keepalive: true },
    ).catch(() => null);
  }

  global.ChatReliability = Object.freeze({
    begin,
    acknowledge,
    cancel,
    clear,
    createId,
    isTerminal: (statusValue) => TERMINAL.has(String(statusValue || '')),
    pending: read,
    reconcile,
    status,
  });
})(window);
