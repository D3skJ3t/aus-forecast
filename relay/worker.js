// Relay for the app's "Refresh now" button, run on Cloudflare Workers (free plan).
// The app can't start a GitHub Actions run itself, because that needs a GitHub token and the app is a public
// page. This relay keeps the token as a secret (GITHUB_TOKEN) and does only two things:
//   POST /refresh?loc=<id>  start the scraper for that one featured location, unless its figures are under
//                           FRESH_MINUTES old, a refresh for it is already running, or MAX_RUNNING are running
//   GET  /data?loc=<id>     that location's data file straight from the repository (no CDN delay), and
//                           whether a refresh for it is still running
// Token: a fine-grained GitHub token for D3skJ3t/aus-forecast only, with Actions read and write and
// Contents read-only.
const REPO = "D3skJ3t/aus-forecast";
const WORKFLOW = "scrape.yml";
const ORIGIN = "https://d3skj3t.github.io";
const FRESH_MINUTES = 30;
const MAX_RUNNING = 3;

export default {
  async fetch(req, env) {
    const origin = req.headers.get("Origin") || "";
    const allow = origin === ORIGIN || /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin) ? origin : ORIGIN;
    const cors = { "Access-Control-Allow-Origin": allow, "Access-Control-Allow-Methods": "GET, POST, OPTIONS", Vary: "Origin" };
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    const reply = (body, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "no-store" } });

    const url = new URL(req.url);
    const loc = url.searchParams.get("loc") || "";
    const route = req.method + " " + url.pathname;
    if (route !== "POST /refresh" && route !== "GET /data") return reply({ error: "not found" }, 404);
    if (!/^[a-z0-9-]{1,40}$/.test(loc)) return reply({ error: "unknown location" }, 400);
    if (!env.GITHUB_TOKEN) return reply({ error: "relay has no token" }, 500);

    const gh = (path, init = {}) =>
      fetch("https://api.github.com/repos/" + REPO + path, {
        ...init,
        headers: {
          Authorization: "Bearer " + env.GITHUB_TOKEN,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "aus-forecast-refresh",
          ...init.headers,
        },
      });

    // The location's data file; a featured location always has one, so a missing file means a made-up id
    const [file, list] = await Promise.all([
      gh("/contents/data/" + loc + ".json?ref=main", { headers: { Accept: "application/vnd.github.raw+json" } }),
      gh("/actions/workflows/" + WORKFLOW + "/runs?event=workflow_dispatch&per_page=30"),
    ]);
    if (file.status === 404) return reply({ error: "unknown location" }, 404);
    if (!file.ok) return reply({ error: "GitHub answered " + file.status }, 502);
    let data = null;
    try { data = JSON.parse(await file.text()); } catch (e) { return reply({ error: "data file unreadable" }, 502); }
    const runs = list.ok ? (await list.json()).workflow_runs.filter((r) => r.status !== "completed") : [];
    const running = runs.some((r) => r.display_title === "Refresh " + loc);

    if (route === "GET /data") return reply({ running, data });

    const generated = data.generated || null;
    const age = generated ? (Date.now() - Date.parse(generated)) / 60000 : Infinity;
    if (running) return reply({ status: "running", generated });
    if (age < FRESH_MINUTES) return reply({ status: "fresh", generated });
    if (runs.length >= MAX_RUNNING) return reply({ status: "busy", generated }, 429);
    const go = await gh("/actions/workflows/" + WORKFLOW + "/dispatches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "main", inputs: { location: loc } }),
    });
    if (go.status !== 204) return reply({ error: "GitHub answered " + go.status }, 502);
    return reply({ status: "started", generated });
  },
};
