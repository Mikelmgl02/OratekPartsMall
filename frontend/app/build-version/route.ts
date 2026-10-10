// The build this server runs, for the update notice of pages left open (lib/update-check.ts). Outside /api/ on purpose: page specs treat any
// /api/ request they did not expect as an error.
export function GET() {
  return Response.json({ deployment: process.env.NEXT_PUBLIC_DEPLOYMENT_ID || '' }, { headers: { 'Cache-Control': 'no-store' } });
}
