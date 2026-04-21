import type { NextConfig } from "next";

// Next doesn't re-export webpack's `Configuration` type (and we don't
// depend on the `webpack` package directly). Describe just the shape
// we mutate here — `watchOptions` — so typos on that path surface
// instead of being silently swallowed by an implicit `any`.
type MutableWebpackConfig = {
  watchOptions?: {
    ignored?: string[] | RegExp | string | (string | RegExp)[];
    aggregateTimeout?: number;
    poll?: number | boolean;
  };
};

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
  webpack(config: MutableWebpackConfig, { dev }: { dev: boolean }) {
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
    //
    // Explicit endpoint allowlist (NOT a catch-all): `/admin/*` and any
    // future GoTrue surface we haven't audited must NOT be reachable
    // through the browser. The supabase-js v2 client only needs the
    // endpoints enumerated below for auth flows we actually support.
    const GOTRUE_ENDPOINTS = [
      "token", // sign-in + refresh
      "user", // current user
      "logout",
      "signup",
      "recover", // password reset
      "reauthenticate",
      "verify", // email / phone verify
      "otp",
      "resend",
      "magiclink",
      "authorize", // oauth start
      "callback", // oauth callback
      "settings", // public GoTrue config (used at boot by supabase-js)
      "health",
    ];
    const rules = GOTRUE_ENDPOINTS.flatMap((endpoint) => [
      // Supabase client path.
      { source: `/gotrue/auth/v1/${endpoint}`, destination: `${GOTRUE_UPSTREAM}/${endpoint}` },
      {
        source: `/gotrue/auth/v1/${endpoint}/:sub*`,
        destination: `${GOTRUE_UPSTREAM}/${endpoint}/:sub*`,
      },
      // Bare path (direct calls, tests).
      { source: `/gotrue/${endpoint}`, destination: `${GOTRUE_UPSTREAM}/${endpoint}` },
      {
        source: `/gotrue/${endpoint}/:sub*`,
        destination: `${GOTRUE_UPSTREAM}/${endpoint}/:sub*`,
      },
    ]);
    return rules;
  },
};

export default nextConfig;
