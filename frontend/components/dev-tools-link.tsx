'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Network } from 'lucide-react';

// Diagramas (app/diagramas) is dev only: the link shows where that page answers (a dev server, or MOTIONPARTES_DEV_TOOLS=1), asked
// once per visit; anywhere else the page is a 404 and the link never shows.
let available: Promise<boolean> | null = null;

export default function DevToolsLink() {
  const [shown, setShown] = useState(false);
  const pathname = usePathname();
  useEffect(() => {
    available ??= fetch('/diagramas', { method: 'HEAD', cache: 'no-store' }).then(response => response.ok).catch(() => false);
    let current = true;
    void available.then(ok => { if (current) setShown(ok); });
    return () => { current = false; };
  }, []);
  if (!shown || pathname?.startsWith('/diagramas')) return null;
  return <Link className="dev-tools-link" href="/diagramas" title="Diagramas de modelos (solo desarrollo)"><Network size={14}/>Diagramas</Link>;
}
