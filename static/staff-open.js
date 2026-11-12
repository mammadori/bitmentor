// The page a staff register's pairing link opens (templates/shops/staff_open.html). The pairing is in the link's
// fragment (#pair=…), which the site never receives: this page keeps it for a moment on this phone, for the register
// the code opens, and the register page takes it after the button (static/register.js, takePending). Only a page with a
// valid code has the button and the register's id; nothing is kept from any other.
(() => {
  const form = document.querySelector("form[data-register]");
  const match = /^#pair=([0-9A-Za-z._-]{1,2400})$/.exec(location.hash);
  if (!form || !match) return;
  try {
    localStorage.setItem("register-pairing-pending",
      JSON.stringify({ register: form.dataset.register, code: match[1], at: Date.now() }));
  } catch { /* without storage the register stays unpaired, and says so */ }
  // The fragment stays out of the history and of any address shown later
  history.replaceState(null, "", location.pathname + location.search);
})();
