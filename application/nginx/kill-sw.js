// Replacement for Navidrome's PWA service worker.
//
// The stock worker precaches index.html and serves it cache-first for
// navigations, which bypasses HTTP cache headers entirely. Behind SSO that
// means a stale, unauthenticated index (no appConfig.auth) is replayed after a
// successful Google login, so the SPA keeps showing its login form. Offline
// support is not useful for this deployment, so the worker is disabled here:
// it wipes the caches, unregisters itself, and reloads any open tab.
self.addEventListener('install', () => {
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys()
      await Promise.all(names.map((name) => caches.delete(name)))
      await self.registration.unregister()
      const clients = await self.clients.matchAll({ type: 'window' })
      clients.forEach((client) => client.navigate(client.url))
    })(),
  )
})
