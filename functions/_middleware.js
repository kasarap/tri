// functions/_middleware.js
// Password-gates the whole site with plain HTTP Basic Auth — the browser's
// native prompt, no custom login page or cookie logic needed. Only the
// password field matters; type anything in the username field.

export async function onRequest({ request, env, next }) {
  if (!env.SITE_PASSWORD) {
    return new Response("SITE_PASSWORD not configured.", { status: 500 });
  }

  const auth = request.headers.get("Authorization") || "";
  const [, encoded] = auth.split(" ");
  const decoded = encoded ? atob(encoded) : "";
  const [, password] = decoded.split(":");

  if (password === env.SITE_PASSWORD) {
    return next();
  }

  return new Response("Password required.", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="tri.jonmercado.com"' },
  });
}
