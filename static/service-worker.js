// Margin — self-hosted read-later server. Copyright (C) 2026 Marc Schlienger
// Licensed under the GNU AGPL v3.0 or later; see the LICENSE file.

/* Offline reading for the home-screen app.

   The point of a read-later queue is the train, the plane, the basement flat
   with no signal. Two kinds of response age quite differently, so each gets
   its own policy:

     the queue (/) and the shell   network-first — items are added and archived
       (CSS, manifest, icons)      constantly, so a stale list is wrong while
                                   online. Offline it is served from the cache
                                   with a line saying so, because the
                                   alternative is a blank page.

     a saved page (/read/… and     cache-first, refreshed behind. These files
       /files/…)                   are written once and not changed again, so
                                   a page opened once stays readable with no
                                   network at all.

   Everything else — saving, archiving, deleting — is network only. They
   change what is on the server, and pretending to do that offline would be
   a lie the queue then has to un-tell. */

const SHELL_CACHE = "margin-shell-v1";
const SAVED_CACHE = "margin-saved-v1";
// Entries, not bytes. A saved PDF can be a few megabytes and the browser
// evicts the whole origin when it runs out of room, so this stays modest.
const SAVED_MAX = 40;
const SHELL = [
  "/",
  "/static/style.css",
  "/manifest.json",
  "/favicon.svg",
  "/static/icon-192.png",
];

// The template leaves this comment where a banner belongs, so the worker can
// say "this is the last list I saw" without parsing the page.
const OFFLINE_MARKER = "<!--offline-notice-->";
const OFFLINE_BANNER =
  '<p class="offline">Offline — this is the last queue Margin saw. ' +
  'Pages you have already opened are still readable.</p>';

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  const keep = [SHELL_CACHE, SAVED_CACHE];
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !keep.includes(k))
                                      .map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const isShell = (url) =>
  SHELL.includes(url.pathname) || url.pathname.startsWith("/static/");

// A saved page and the file behind it: written once, never edited. The
// queue's own listing is not here — that is "/", and it changes.
const isSaved = (url) =>
  /^\/(read|files)\/[^/]+$/.test(url.pathname);

// Stems deleted during this worker's lifetime. A refresh that was already in
// flight when the delete arrived finishes its cache.put afterwards and puts
// the page back, so an offline read resurrects something that is gone.
const forgotten = new Set();
const FORGOTTEN_MAX = 100;

// "2026-07-19-title.md" → "2026-07-19-title". One item is several files.
function stemOf(url) {
  const found = new URL(url).pathname.match(/^\/(?:read|files)\/(.+)$/);
  if (!found) return "";
  const name = decodeURIComponent(found[1]);
  return name.replace(/\.[^.]*$/, "");
}

// The only way anything is written to a cache: checked before the put and
// again after it, because the delete can land between those two lines.
async function keep(cache, request, response) {
  const stem = stemOf(request.url);
  if (stem && forgotten.has(stem)) return;
  await cache.put(request, response);
  if (stem && forgotten.has(stem)) await cache.delete(request);
}

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  // A ?token= URL is answered by the network and never stored: the cache key
  // is the whole URL, so caching it would write the token into Cache Storage
  // and leave it there long after the cookie made it unnecessary.
  if (url.searchParams.has("token")) return;
  // ?download=1 is a file to save, not a page to read.
  if (url.searchParams.has("download")) return;

  if (url.pathname === "/") {
    event.respondWith(queueFirst(event.request));
  } else if (isShell(url)) {
    event.respondWith(networkFirst(event.request, SHELL_CACHE));
  } else if (isSaved(url)) {
    event.respondWith(cacheThenRefresh(event));
  }
});

async function networkFirst(request, cacheName) {
  try {
    const answer = await fetch(request);
    // Only successful responses: an unauthorized page must never be cached
    // as though it were the app. Awaited, because an unawaited put can still
    // be in flight when the browser decides to stop the worker.
    if (answer.ok) await keep(await caches.open(cacheName), request, answer.clone());
    else if (answer.status === 401) await forgetEverything();
    return answer;
  } catch (err) {
    const cached = await (await caches.open(cacheName)).match(request);
    if (!cached) throw err;
    return cached;
  }
}

// The queue itself. Same policy as the shell, except that what comes back
// from the cache is marked, because a list of items that is quietly hours
// old is worse than one that says so.
async function queueFirst(request) {
  try {
    const answer = await fetch(request);
    if (answer.ok) {
      await (await caches.open(SHELL_CACHE)).put(request, answer.clone());
    } else if (answer.status === 401) {
      await forgetEverything();
    }
    return answer;
  } catch (err) {
    const cached = await (await caches.open(SHELL_CACHE)).match(request);
    if (!cached) throw err;
    return marked(cached);
  }
}

async function cacheThenRefresh(event) {
  const cache = await caches.open(SAVED_CACHE);
  const cached = await cache.match(event.request);
  const fresh = fetch(event.request).then(async (answer) => {
    if (answer.ok) {
      await keep(cache, event.request, answer.clone());
      await trim(cache, SAVED_MAX);
    } else if (answer.status === 401) {
      await forgetEverything();
    } else if (answer.status === 404 || answer.status === 410) {
      // Deleted on the server. The cache must not go on answering for it —
      // and not for its siblings either, or an offline visit brings the
      // item back one file at a time.
      await forgetStem(stemOf(event.request.url));
    }
    return answer;
  });
  if (!cached) return fresh;
  event.waitUntil(fresh.catch(() => {}));
  return cached;
}

// One item is gone: drop every file cached under its stem, not just the one
// that happened to be asked for.
async function forgetStem(stem) {
  if (!stem) return;
  forgotten.add(stem);
  while (forgotten.size > FORGOTTEN_MAX) {
    forgotten.delete(forgotten.values().next().value);
  }
  for (const name of [SAVED_CACHE, SHELL_CACHE]) {
    const cache = await caches.open(name);
    for (const request of await cache.keys()) {
      if (stemOf(request.url) === stem) await cache.delete(request);
    }
  }
}

// The page is about to delete an item: clear it now rather than waiting for
// someone to ask for it again, which offline may be never.
self.addEventListener("message", (event) => {
  const { type, stem } = event.data || {};
  if (type !== "forget-stem" || !stem) return;
  event.waitUntil(forgetStem(stem));
});

// The token was changed or revoked: drop what was read under the old one.
// Only reachable while online — an offline device keeps whatever it cached
// until its site data is cleared, which is a property of browser storage,
// not something a server can revoke.
async function forgetEverything() {
  await Promise.all([caches.delete(SHELL_CACHE), caches.delete(SAVED_CACHE)]);
}

// Oldest fetch first — cache.keys() is insertion-ordered.
async function trim(cache, max) {
  const keys = await cache.keys();
  for (const key of keys.slice(0, keys.length - max)) await cache.delete(key);
}

// Saying "this came from the cache" means rebuilding the page around it:
// response bodies are read once and headers are immutable.
async function marked(response) {
  const body = await response.text();
  return new Response(body.replace(OFFLINE_MARKER, OFFLINE_BANNER), {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}
