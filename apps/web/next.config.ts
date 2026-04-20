import type { NextConfig } from "next";

// The self-hosted GoTrue v2.188.1 CORS middleware doesn't whitelist the
// `apikey` header that supabase-js sends on every call, so direct
// browser→:9999 requests fail preflight. Proxy GoTrue through the Next
// dev server instead — same-origin, no CORS. `NEXT_PUBLIC_GOTRUE_URL`
// in the browser points at `/gotrue`; we rewrite it here.
const GOTRUE_UPSTREAM = process.env["GOTRUE_UPSTREAM_URL"] ?? "http://localhost:9999";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  typedRoutes: true,
  // Webpack's default dev watcher opens one fd per file under the
  // project root, which blows past Node's fd budget under the Claude
  // sandbox (seen as "Watchpack EMFILE: too many open files" spam).
  // We don't need to watch third-party packages, build output, caches,
  // infra, or Python app sources — the web app only cares about its
  // own src tree.
  webpack(config, { dev }) {
    if (dev) {
      config.watchOptions = {
        ...config.watchOptions,
        ignored: [
          "**/node_modules/**",
          "**/.git/**",
          "**/.next/**",
          "**/.venv/**",
          "**/.uv-cache/**",
          "**/.ruff_cache/**",
          "**/.pytest_cache/**",
          "**/.pnpm-store/**",
          "**/.model-cache/**",
          "**/apps/api/**",
          "**/apps/agent/**",
          "**/infra/**",
          "**/docs/**",
          "**/tests/**",
          "**/.logs/**",
        ],
      };
    }
    return config;
  },
  async rewrites() {
    // supabase-ssr hardcodes the `/auth/v1/` prefix (from hosted Supabase
    // where Kong sits in front). Self-hosted GoTrue exposes routes at
    // the root, so strip that prefix during the proxy hop.
    return [
      { source: "/gotrue/auth/v1/:path*", destination: `${GOTRUE_UPSTREAM}/:path*` },
      { source: "/gotrue/:path*", destination: `${GOTRUE_UPSTREAM}/:path*` },
    ];
  },
};

export default nextConfig;
