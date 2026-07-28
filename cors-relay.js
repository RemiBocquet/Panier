/**
 * Panier — relais CORS pour l'import de recettes.
 *
 * Le navigateur ne peut pas lire marmiton.org directement (CORS).
 * Ce Worker récupère la page côté serveur et la renvoie avec les en-têtes CORS.
 *
 * Usage : GET https://<worker>/?url=<url-encodée>
 */

const ALLOWED_HOSTS = [
  'marmiton.org', 'www.marmiton.org',
  'jow.fr', 'www.jow.fr', 'api.jow.fr', 'static.jow.fr',
  'jow.com', 'www.jow.com', 'app.jow.com',
  'cuisineaz.com', 'www.cuisineaz.com',
  '750g.com', 'www.750g.com',
];

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Accept-Language, x-jow-withmeta',
  'Access-Control-Max-Age': '86400',
};

export default {
  async fetch(request, env) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }

    // ---- Synchronisation entre appareils ----
    const path = new URL(request.url).pathname;
    if (path === '/sync' || path === '/sync/') {
      return handleSync(request, env);
    }

    const { searchParams } = new URL(request.url);
    const target = searchParams.get('url');
    if (!target) return json({ error: 'Paramètre ?url= manquant.' }, 400);

    let parsed;
    try {
      parsed = new URL(target);
    } catch {
      return json({ error: 'URL invalide.' }, 400);
    }

    if (parsed.protocol !== 'https:') {
      return json({ error: 'Seul https est autorisé.' }, 400);
    }

    const host = parsed.hostname.toLowerCase();
    const ok = ALLOWED_HOSTS.some((h) => host === h || host.endsWith('.' + h));
    if (!ok) return json({ error: `Domaine non autorisé : ${host}` }, 403);

    // La recherche Jow (quicksearch) est un POST : on relaie la méthode et le corps.
    const isPost = request.method === 'POST';
    const headers = {
      'User-Agent':
        'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Mobile Safari/537.36',
      'Accept': 'text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8',
      'Accept-Language': 'fr-FR,fr;q=0.9',
    };
    if (isPost) {
      headers['Content-Type'] = 'application/json';
      headers['Accept'] = 'application/json';
      headers['x-jow-withmeta'] = '1';
    }

    try {
      const upstream = await fetch(parsed.toString(), {
        method: isPost ? 'POST' : 'GET',
        headers,
        body: isPost ? (await request.text()) || '{}' : undefined,
        cf: isPost ? {} : { cacheTtl: 300, cacheEverything: true },
      });

      const ct = upstream.headers.get('content-type') || 'text/html; charset=utf-8';
      const body = await upstream.text();

      return new Response(body, {
        status: upstream.status,
        headers: { ...CORS, 'Content-Type': ct, 'X-Relayed-From': host },
      });
    } catch (err) {
      return json({ error: 'Échec de récupération : ' + err.message }, 502);
    }
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...CORS, 'Content-Type': 'application/json; charset=utf-8' },
  });
}

const MAX_ITEMS = 500;          // par requête
const MAX_PAYLOAD = 64 * 1024;  // par enregistrement

const SYNCED_STORES = ['recipes', 'meals', 'shopping', 'stock'];

async function handleSync(request, env) {
  if (!env || !env.DB) {
    return json({ error: "Base D1 non liée. Ajoute une liaison nommée 'DB' au Worker." }, 500);
  }

  const url = new URL(request.url);
  const room = (url.searchParams.get('room') || '').toLowerCase();

  if (!/^[a-f0-9]{32}$/.test(room)) {
    return json({ error: 'Paramètre room invalide.' }, 400);
  }

  if (request.method === 'GET') {
    const since = parseInt(url.searchParams.get('since') || '0', 10) || 0;
    const { results } = await env.DB
      .prepare('SELECT id, store, updated_at, deleted, payload FROM items WHERE room = ? AND updated_at >= ? ORDER BY updated_at ASC LIMIT ?')
      .bind(room, since, MAX_ITEMS)
      .all();

    const items = (results || []).map((r) => ({
      id: r.id,
      store: r.store,
      updatedAt: r.updated_at,
      deleted: !!r.deleted,
      payload: r.payload,
    }));
    const now = items.length ? items[items.length - 1].updatedAt : since;
    return json({ now, items, more: items.length === MAX_ITEMS });
  }

  if (request.method === 'POST') {
    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: 'JSON invalide.' }, 400);
    }
    const items = Array.isArray(body && body.items) ? body.items : [];
    if (!items.length) return json({ ok: true, written: 0 });
    if (items.length > MAX_ITEMS) return json({ error: 'Trop d\'éléments.' }, 413);

    const stmt = env.DB.prepare(
      `INSERT INTO items (room, id, store, updated_at, deleted, payload)
       VALUES (?, ?, ?, ?, ?, ?)
       ON CONFLICT(room, id) DO UPDATE SET
         store      = excluded.store,
         updated_at = excluded.updated_at,
         deleted    = excluded.deleted,
         payload    = excluded.payload
       WHERE excluded.updated_at > items.updated_at`
    );

    const batch = [];
    for (const it of items) {
      if (!it || typeof it.id !== 'string' || !SYNCED_STORES.includes(it.store)) continue;
      const ts = parseInt(it.updatedAt, 10);
      if (!Number.isFinite(ts) || ts <= 0) continue;
      const payload = it.deleted ? null : String(it.payload || '');
      if (payload && payload.length > MAX_PAYLOAD) continue;
      batch.push(stmt.bind(room, it.id, it.store, ts, it.deleted ? 1 : 0, payload));
    }
    if (batch.length) await env.DB.batch(batch);
    return json({ ok: true, written: batch.length });
  }

  return json({ error: 'Méthode non autorisée.' }, 405);
}
