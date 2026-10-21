const CACHE='pot-shell-v1';
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
// Financial data and authenticated responses are deliberately never cached.
self.addEventListener('fetch',e=>{if(e.request.mode==='navigate')e.respondWith(fetch(e.request).catch(()=>new Response('<!doctype html><title>Offline</title><body style="background:#101512;color:#e7eee8;font:18px system-ui;padding:40px"><h1>You’re offline</h1><p>Reconnect to view your paper trades. Your server keeps the saved records.</p><button onclick="location.reload()">Try again</button></body>',{headers:{'Content-Type':'text/html'}})));});
