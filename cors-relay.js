/**
 * Panier — relais CORS pour l'import de recettes.
 *
 * Le navigateur ne peut pas lire marmiton.org directement (CORS).
 * Ce Worker récupère la page côté serveur et la renvoie avec les en-têtes CORS.
 *
 * Déploiement :
 *   1. dash.cloudflare.com → Workers → Create Worker (ou `wrangler deploy`)
 *   2. Colle ce fichier, déploie.
 *   3. Copie l'URL du Worker (ex. https://panier-relay.xxx.workers.dev)
 *   4. Dans l'app : Réglages → "Relais d'import" → colle l'URL.
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
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
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

    try {
      const upstream = await fetch(parsed.toString(), {
        headers: {
          'User-Agent':
            'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Mobile Safari/537.36',
          'Accept': 'text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8',
          'Accept-Language': 'fr-FR,fr;q=0.9',
        },
        cf: { cacheTtl: 300, cacheEverything: true },
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
