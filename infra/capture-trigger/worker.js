// Cloudflare Worker: dispatches capture.yml every 15 minutes (D17), because GitHub's `schedule`
// fired only 5 of ~126 ticks on 2026-11-05/06. It has no fetch handler, so nothing can call it
// over HTTP. GITHUB_DISPATCH_TOKEN is a Worker secret, never stored in this repo.
const URL =
  "https://api.github.com/repos/paulattard4/adatta/actions/workflows/capture.yml/dispatches";

async function dispatch(token) {
  const response = await fetch(URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "adatta-capture-trigger",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  // Throwing marks the invocation as failed in the Worker's logs. Status only: no token.
  if (response.status !== 204) throw new Error(`dispatch failed: HTTP ${response.status}`);
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(dispatch(env.GITHUB_DISPATCH_TOKEN));
  },
};
