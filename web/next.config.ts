import type { NextConfig } from "next";

// Local only (docs/DASHBOARD_TASKS.md): the browser talks to Next, which proxies /api to the
// read-only FastAPI service on 127.0.0.1:8000.
const API = process.env.ADATTA_API ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};

export default config;
