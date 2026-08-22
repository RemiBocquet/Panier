/**
 * Panier — relais CORS pour l'import de recettes.
 *
 * Le navigateur ne peut pas lire marmiton.org directement (CORS).
 * Ce Worker récupère la page côté serveur et la renvoie avec les en-têtes CORS.
 *
 * Deux listes blanches cohabitent ici, et elles ne vont PAS dans le même sens :
 *   ALLOWED_HOSTS   — OÙ ce Worker a le droit d'aller chercher (les sites de recettes).
 *                     Sans elle, ce serait un proxy ouvert utilisable contre n'importe qui.
 *   ALLOWED_ORIGINS — QUI a le droit d'appeler ce Worker depuis un navigateur (PAN-4).
 */

const ALLOWED_HOSTS = [
  'marmiton.org', 'www.marmiton.org',
  'jow.fr', 'www.jow.fr', 'api.jow.fr', 'static.jow.fr',
  'jow.com', 'www.jow.com', 'app.jow.com',
  'cuisineaz.com', 'www.cuisineaz.com',
  '750g.com', 'www.750g.com',
];

/* PAN-4 — Qui a le droit d'appeler ce Worker depuis un navigateur.
   Auparavant : Access-Control-Allow-Origin: '*', c'est-à-dire n'importe quelle page web
   du monde, y compris sur /sync et /feedback.

   Portée réelle de cette liste, à connaître pour ne pas s'en croire mieux protégé qu'on
   ne l'est : CORS n'engage QUE les navigateurs. curl, un script ou un serveur ignorent
   complètement ces en-têtes. Ce qui est bloqué ici, c'est l'abus par ricochet — une page
   malveillante qui se sert du navigateur de ses visiteurs. En pratique la requête
   préalable (preflight) échoue, donc l'écriture sur /sync et l'envoi sur /feedback ne
   partent même pas.

   Ce qui protège réellement /sync reste l'imprévisibilité de l'identifiant de salon :
   125 bits tirés au sort depuis la correction PAN-2. */
const ALLOWED_ORIGINS = [
  'https://panier.remibocquet.fr',

  // TEMPORAIRE — ancien hébergement GitHub Pages, gardé le temps que les utilisateurs
  // migrent vers panier.remibocquet.fr. À RETIRER une fois la migration terminée.
  //
  // Deux choses à savoir sur cette ligne :
  //   1. Une origine, c'est schéma + hôte + port, jamais le chemin. L'application est
  //      servie sous /Panier/, mais autoriser cette origine autorise TOUTE page publiée
  //      sur remibocquet.github.io, y compris celles d'autres dépôts.
  //   2. Cette copie doit servir le même code corrigé que le Pi. Sinon elle continue
  //      d'exposer les failles PAN-1 à PAN-3, et ce Worker lui garde la porte ouverte.
  'https://remibocquet.github.io',

  // Mise au point en local contre ce relais : décommenter le temps des essais, et ne
  // pas déployer le Worker dans cet état.
  // 'http://localhost:8123',
];

/* L'en-tête Access-Control-Allow-Origin n'accepte pas de liste : sa valeur est soit '*',
   soit UNE origine. On renvoie donc celle de la requête, si elle figure dans la liste.

   Deux pièges évités ici :
   — on OMET l'en-tête quand l'origine ne convient pas, plutôt que de répondre 'null'.
     'null' n'est pas un refus : c'est une origine réelle, celle des iframes en bac à
     sable et des pages file:// — la renvoyer leur accorderait justement l'accès.
   — 'Vary: Origin' est indispensable : sans lui, un cache intermédiaire (celui de
     Cloudflare compris) peut servir à un site la réponse calculée pour un autre, ce qui
     annulerait tout le filtrage. Ce n'est pas théorique ici : deux origines sont
     autorisées pendant la migration, donc une même URL a bel et bien deux réponses
     valides selon l'appelant. */
function corsHeaders(request) {
  const origin = request.headers.get('Origin');
  const headers = {
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Accept-Language, x-jow-withmeta',
    'Access-Control-Max-Age': '86400',
    'Vary': 'Origin',
  };
  if (origin && ALLOWED_ORIGINS.includes(origin)) {
    headers['Access-Control-Allow-Origin'] = origin;
  }
  return headers;
}

/* json() dépend désormais de la requête en cours : on en fabrique une par requête,
   qu'on passe aux gestionnaires. Cela évite de retoucher leurs vingt appels. */
const jsonWith = (cors) => (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, 'Content-Type': 'application/json; charset=utf-8' },
  });

export default {
  async fetch(request, env) {
    const cors = corsHeaders(request);
    const json = jsonWith(cors);

    if (request.method === 'OPTIONS') {
      // Origine non autorisée : la réponse ne porte pas d'Access-Control-Allow-Origin,
      // le navigateur fait donc échouer la requête préalable et n'envoie jamais la vraie.
      return new Response(null, { status: 204, headers: cors });
    }

    // ---- Synchronisation entre appareils ----
    const path = new URL(request.url).pathname;
    if (path === '/sync' || path === '/sync/') {
      return handleSync(request, env, json);
    }

    // ---- Signaler un bug / proposer une idée ----
    if (path === '/feedback' || path === '/feedback/') {
      return handleFeedback(request, env, json);
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
        headers: { ...cors, 'Content-Type': ct, 'X-Relayed-From': host },
      });
    } catch (err) {
      return json({ error: 'Échec de récupération : ' + err.message }, 502);
    }
  },
};

const MAX_ITEMS = 500;          // par requête
const MAX_PAYLOAD = 64 * 1024;  // par enregistrement

const SYNCED_STORES = ['recipes', 'meals', 'shopping', 'stock'];

async function handleSync(request, env, json) {
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

/*
   Signaler un bug / proposer une idée
   
   Envoie le message par email via l'API Resend.
*/
const FEEDBACK_TO = 'panier.repas.courses@gmail.com';
const FEEDBACK_MAX_LEN = 5000;

async function handleFeedback(request, env, json) {
  if (request.method !== 'POST') return json({ error: 'Méthode non autorisée.' }, 405);
  if (!env || !env.RESEND_API_KEY) {
    return json({ error: "Envoi automatique non configuré côté serveur (clé RESEND_API_KEY absente)." }, 500);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'JSON invalide.' }, 400);
  }

  const kind = body && body.kind === 'idea' ? 'idea' : 'bug';
  const message = String((body && body.message) || '').trim().slice(0, FEEDBACK_MAX_LEN);
  if (!message) return json({ error: 'Message vide.' }, 400);
  const version = String((body && body.version) || '').slice(0, 40);
  const userAgent = String((body && body.userAgent) || '').slice(0, 300);

  const subject = '[Panier] ' + (kind === 'idea' ? 'Suggestion' : 'Bug signalé');
  const text = message
    + '\n\n—\nVersion : ' + version
    + '\nAppareil : ' + userAgent
    + '\nDate : ' + new Date().toISOString();

  try {
    const res = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + env.RESEND_API_KEY,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        from: 'Panier <onboarding@resend.dev>',
        to: env.FEEDBACK_TO || FEEDBACK_TO,
        subject,
        text,
      }),
    });
    if (!res.ok) {
      return json({ error: 'Échec de l’envoi (' + res.status + ').' }, 502);
    }
    return json({ ok: true });
  } catch (err) {
    return json({ error: 'Échec de l’envoi : ' + err.message }, 502);
  }
}
