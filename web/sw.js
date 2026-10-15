// Offline shell only. Live railway data is never cached by the service worker (it must stay fresh).
const C = "rv-shell-v2";
self.addEventListener("install", e => e.waitUntil(caches.open(C).then(c => c.addAll(["./", "manifest.webmanifest", "icon.svg"]))));
self.addEventListener("activate", e => e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k))))));
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.pathname.startsWith("/api/") || u.pathname.startsWith("/webhooks/")) return;
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request).then(r => r || caches.match("./"))));
});
