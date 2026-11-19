// The QR code of a second factor being set up (templates/admin/second_factor.html, config/second_factor.py): the
// otpauth:// link of the page, drawn here as the staff registers' codes are (static/staff-pair.js). Nothing is sent.
(() => {
  const image = document.getElementById("totp-qr");
  if (!image || typeof qrcode !== "function") return;
  const modules = qrcode(image.dataset.uri || "");
  const size = modules.length + 8;
  let path = "";
  modules.forEach((row, y) => row.forEach((dark, x) => { if (dark) path += `M${x + 4},${y + 4}h1v1h-1z`; }));
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges">`
    + `<rect width="${size}" height="${size}" fill="#fff"/><path d="${path}" fill="#000"/></svg>`;
  image.src = `data:image/svg+xml,${encodeURIComponent(svg)}`;
  image.hidden = false;
})();
