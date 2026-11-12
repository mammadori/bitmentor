// The register talks to Nostr relays (NIP-01) only for its copies (static/register-nostr.js): it sends its own events
// and asks for the ones encrypted to the owner. The relays are the ones of community.yml, the only ones the register's
// CSP opens. A relay is someone else's server: what it sends is checked by the caller (signature, encryption), and here
// it is limited in time, in the size of a message and in the number of events. `Socket` is the WebSocket to use (the
// tests give their own).
(() => {
  const OPEN_MS = 8000;
  const WAIT_MS = 12000;
  // An event of a copy is at most NIP-44's 87472 characters plus its envelope (id, keys, tags, signature: under 1 KB)
  const MAX_FRAME = 90 * 1024;
  // What one relay may send for one request: events, messages and characters in all, ignored ones included. Past any
  // of them the register stops listening to that relay
  const RELAY_EVENTS = 500;
  const RELAY_FRAMES = 1000;
  const RELAY_BUDGET = 8 * 1024 * 1024;
  // The events kept from all the relays together, taken in turn from each relay so none can crowd out the others
  const MAX_EVENTS = 2000;

  function connect(url, Socket) {
    return new Promise((resolve, reject) => {
      let socket;
      try {
        socket = new Socket(url);
      } catch (error) {
        reject(error);
        return;
      }
      const timer = setTimeout(() => {
        socket.close();
        reject(new Error("timeout"));
      }, OPEN_MS);
      socket.onopen = () => {
        clearTimeout(timer);
        resolve(socket);
      };
      socket.onerror = () => {
        clearTimeout(timer);
        reject(new Error("error"));
      };
    });
  }

  function read(message) {
    if (typeof message.data !== "string" || message.data.length > MAX_FRAME) return null;
    try {
      const value = JSON.parse(message.data);
      return Array.isArray(value) ? value : null;
    } catch {
      return null;
    }
  }

  // Sends the events to one relay: the ids it accepted
  async function sendTo(url, events, Socket) {
    const socket = await connect(url, Socket);
    const waiting = new Set(events.map((event) => event.id));
    const accepted = [];
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, WAIT_MS);
      socket.onmessage = (message) => {
        const value = read(message);
        if (!value || value[0] !== "OK" || !waiting.has(value[1])) return;
        waiting.delete(value[1]);
        if (value[2] === true) accepted.push(value[1]);
        if (!waiting.size) {
          clearTimeout(timer);
          resolve();
        }
      };
      socket.onclose = () => {
        clearTimeout(timer);
        resolve();
      };
      for (const event of events) socket.send(JSON.stringify(["EVENT", event]));
    });
    socket.close();
    return accepted;
  }

  // Sends the events to every relay at once: {url: number of events accepted}
  async function publish(urls, events, { Socket = globalThis.WebSocket } = {}) {
    const results = await Promise.allSettled(urls.map((url) => sendTo(url, events, Socket)));
    return Object.fromEntries(urls.map((url, i) => [url, results[i].status === "fulfilled" ? results[i].value.length : 0]));
  }

  // The events one relay has for the filter, until it says it sent them all (EOSE) or the time is up
  async function askOne(url, filter, Socket) {
    const socket = await connect(url, Socket);
    const id = `c${Math.random().toString(36).slice(2, 10)}`;
    const found = [];
    let frames = 0;
    let characters = 0;
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, WAIT_MS);
      const stop = () => {
        clearTimeout(timer);
        socket.onmessage = null;
        resolve();
      };
      socket.onmessage = (message) => {
        frames += 1;
        characters += typeof message.data === "string" ? message.data.length : MAX_FRAME;
        if (frames > RELAY_FRAMES || characters > RELAY_BUDGET) {
          stop();
          return;
        }
        const value = read(message);
        if (!value || value[1] !== id) return;
        if (value[0] === "EVENT" && value[2] && typeof value[2] === "object") found.push(value[2]);
        if (value[0] === "EOSE" || value[0] === "CLOSED" || found.length >= RELAY_EVENTS) stop();
      };
      socket.onclose = () => {
        clearTimeout(timer);
        resolve();
      };
      socket.send(JSON.stringify(["REQ", id, filter]));
    });
    try {
      socket.send(JSON.stringify(["CLOSE", id]));
    } catch { /* already closed */ }
    socket.close();
    return found;
  }

  // An event's id as NIP-01 makes it: the SHA-256 of its content, author, time, kind and tags (as registerNostr.eventId)
  async function contentId(event) {
    const text = JSON.stringify([0, event.pubkey, event.created_at, event.kind, event.tags, event.content]);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  // The events of every relay, each once, one from each relay in turn up to MAX_EVENTS: {events, answered: how many
  // relays answered}. An event whose id is not its content's is dropped. The same id with another signature is kept
  // apart: a relay could send a genuine event's id and content with a false signature, and if the first one seen won,
  // the genuine copy from the other relays would be lost. The caller checks the signatures (registerNostr.openCopy)
  async function request(urls, filter, { Socket = globalThis.WebSocket } = {}) {
    const results = await Promise.allSettled(urls.map((url) => askOne(url, filter, Socket)));
    const lists = results.filter((result) => result.status === "fulfilled").map((result) => result.value);
    const events = new Map();
    for (let i = 0; events.size < MAX_EVENTS && lists.some((list) => i < list.length); i++) {
      for (const list of lists) {
        const event = list[i];
        if (!event || typeof event.id !== "string" || typeof event.sig !== "string" || events.size >= MAX_EVENTS) continue;
        const key = `${event.id}:${event.sig}`;
        if (events.has(key)) continue;
        let id;
        try {
          id = await contentId(event);
        } catch {
          continue;
        }
        if (id === event.id) events.set(key, event);
      }
    }
    return { events: [...events.values()], answered: lists.length };
  }

  globalThis.registerRelays = { publish, request };
})();
