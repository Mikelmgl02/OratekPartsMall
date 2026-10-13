import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import Link from 'next/link';
import { notFound } from 'next/navigation';

// Diagramas: the team's model diagrams (frontend/diagrams/*.html), for development only. Outside a dev server it exists only where the
// server runs with MOTIONPARTES_DEV_TOOLS=1 (the local docker stack's .env); everywhere else it is a 404 like any unknown page.
export const metadata = { title: 'Diagramas — MotionPartes', robots: { index: false, follow: false } };
export const dynamic = 'force-dynamic';

const folder = path.join(process.cwd(), 'diagrams');
const enabled = () => process.env.NODE_ENV !== 'production' || process.env.MOTIONPARTES_DEV_TOOLS === '1';
const titleOf = (file: string) => file.replace(/\.html$/, '').replace(/-/g, ' ').replace(/^./, letter => letter.toUpperCase());

export default async function Diagrams({ searchParams }: { searchParams: Promise<{ d?: string }> }) {
  if (!enabled()) notFound();
  const files = (await readdir(folder).catch(() => [] as string[])).filter(file => /^[a-z0-9-]+\.html$/.test(file)).sort();
  const { d } = await searchParams;
  const current = files.find(file => file === `${d}.html`) ?? files[0];
  const source = current ? await readFile(path.join(folder, current), 'utf8') : '';
  return <div className="diagrams-page">
    <aside className="diagrams-list" aria-label="Diagramas">
      <span className="eyebrow">MOTIONPARTES · SOLO DESARROLLO</span>
      <h1>Diagramas</h1>
      <p>Archivos en <code>frontend/diagrams/</code>. Agrega uno y aparece aquí.</p>
      <nav><ul>{files.map(file => <li key={file}><Link href={`/diagramas?d=${file.replace(/\.html$/, '')}`} aria-current={file === current ? 'page' : undefined}>{titleOf(file)}</Link></li>)}</ul></nav>
      <Link className="button soft small" href="/">Volver al catálogo</Link>
    </aside>
    <main className="diagrams-view">
      {current ? <iframe title={titleOf(current)} srcDoc={source} sandbox=""/> : <p>Aún no hay diagramas en <code>frontend/diagrams/</code>.</p>}
    </main>
  </div>;
}
