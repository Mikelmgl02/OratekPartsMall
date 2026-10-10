import type { NextConfig } from 'next';
// The build's id (scripts/build-with-grid-license.mjs sets it; a dev server has none): version-skew reloads, and the update notice's check.
const deployment = process.env.NEXT_DEPLOYMENT_ID || '';
const config: NextConfig = {
  output: 'standalone',
  poweredByHeader: false,
  ...(deployment ? { deploymentId: deployment } : {}),
  env: { NEXT_PUBLIC_DEPLOYMENT_ID: deployment },
  async headers() {
    return [{ source: '/:path*', headers: [
      { key: 'X-Content-Type-Options', value: 'nosniff' },
      { key: 'X-Frame-Options', value: 'SAMEORIGIN' },
      { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
    ] }];
  },
};
export default config;
