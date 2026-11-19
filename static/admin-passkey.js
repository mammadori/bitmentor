// Passkeys of /admin/ (config/webauthn.py, config/second_factor.py): the form #passkey-form, with data-passkey "create"
// (the second factor's page: add one) or "get" (the sign-in: use one), and the options of the server in the JSON of
// #passkey-options, binary values in base64url. The browser makes or uses the passkey; its answer goes back in the
// form's hidden `credential`, as JSON with base64url values, by the form's own submit. Nothing else is sent anywhere.
// Without WebAuthn the page's .passkey-unsupported line shows; without JavaScript the button stays off.
(() => {
  function toBase64url(buffer) {
    const bytes = buffer instanceof ArrayBuffer ? new Uint8Array(buffer)
      : new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
    let text = "";
    for (const byte of bytes) text += String.fromCharCode(byte);
    return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  // An ArrayBuffer; a TypeError for anything that is not base64url without padding
  function fromBase64url(text) {
    if (typeof text !== "string" || !/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) {
      throw new TypeError("not base64url");
    }
    const binary = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4));
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes.buffer;
  }

  const withIds = (list) => (Array.isArray(list) ? list : []).map((item) => ({ ...item, id: fromBase64url(item.id) }));

  // The options of navigator.credentials.create, from the server's JSON
  function creationOptions(json) {
    return { ...json, challenge: fromBase64url(json.challenge), user: { ...json.user, id: fromBase64url(json.user.id) },
      excludeCredentials: withIds(json.excludeCredentials) };
  }

  // The options of navigator.credentials.get, from the server's JSON
  function requestOptions(json) {
    return { ...json, challenge: fromBase64url(json.challenge), allowCredentials: withIds(json.allowCredentials) };
  }

  // What the server reads: the credential's id and the parts of its response, in base64url
  function credentialJSON(credential) {
    const response = credential.response;
    const out = { id: credential.id, rawId: toBase64url(credential.rawId), type: credential.type,
      response: { clientDataJSON: toBase64url(response.clientDataJSON) } };
    if (response.attestationObject) {
      out.response.attestationObject = toBase64url(response.attestationObject);
    } else {
      out.response.authenticatorData = toBase64url(response.authenticatorData);
      out.response.signature = toBase64url(response.signature);
      // An empty user handle is none, as the server reads it
      out.response.userHandle = response.userHandle && response.userHandle.byteLength
        ? toBase64url(response.userHandle) : null;
    }
    return out;
  }

  function supported(win) {
    return typeof win.PublicKeyCredential === "function" && !!(win.navigator && win.navigator.credentials);
  }

  // The text of a failure: the page's own sentence, and the browser's name of the error
  function failure(sentence, problem) {
    const name = problem && typeof problem.name === "string" ? problem.name : "Error";
    return `${sentence} (${name})`;
  }

  function wire(doc, win) {
    const form = doc.getElementById("passkey-form");
    const data = doc.getElementById("passkey-options");
    if (!form || !data) return;
    const button = form.querySelector('input[type="submit"]');
    const error = form.querySelector(".passkey-error");
    if (!supported(win)) {
      form.querySelector(".passkey-unsupported").hidden = false;
      return;
    }
    let options;
    try {
      options = JSON.parse(data.textContent);
    } catch {
      return;
    }
    const creating = form.dataset.passkey === "create";
    button.disabled = false;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      error.hidden = true;
      if (creating && !form.reportValidity()) return;
      button.disabled = true;
      try {
        const credential = creating
          ? await win.navigator.credentials.create({ publicKey: creationOptions(options) })
          : await win.navigator.credentials.get({ publicKey: requestOptions(options) });
        form.elements.credential.value = JSON.stringify(credentialJSON(credential));
        // submit() does not come back to this listener
        form.submit();
      } catch (problem) {
        error.textContent = failure(form.dataset.failed || "", problem);
        error.hidden = false;
        button.disabled = false;
      }
    });
  }

  globalThis.adminPasskey = { toBase64url, fromBase64url, creationOptions, requestOptions, credentialJSON, supported,
    failure };
  if (typeof document !== "undefined") wire(document, window);
})();
