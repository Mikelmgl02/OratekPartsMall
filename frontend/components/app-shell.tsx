'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import BrandLogo from './brand-logo';

type ShellContext = { toolbarHost: HTMLDivElement | null; viewport: RefObject<HTMLDivElement | null> };
const Context = createContext<ShellContext | null>(null);

export default function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const viewport = useRef<HTMLDivElement>(null);
  const [toolbarHost, setToolbarHost] = useState<HTMLDivElement | null>(null);
  const value = useMemo(() => ({ toolbarHost, viewport }), [toolbarHost]);

  useEffect(() => { viewport.current?.scrollTo({ top: 0, behavior: 'instant' }); }, [pathname]);

  return <Context.Provider value={value}><div className="app-shell">
    <div ref={setToolbarHost} className="app-toolbar">
      <header className="header app-default-toolbar"><div className="header-inner">
        <Link className="wordmark" aria-label="Inicio de MotionPartes" href="/"><BrandLogo/></Link>
      </div></header>
    </div>
    <div ref={viewport} className="app-viewport" data-testid="page-viewport">{children}</div>
  </div></Context.Provider>;
}

function useShell() {
  const shell = useContext(Context);
  if (!shell) throw new Error('The page must be rendered inside AppShell.');
  return shell;
}

export function AppToolbar({ children }: { children: ReactNode }) {
  const { toolbarHost } = useShell();
  return toolbarHost ? createPortal(<div className="app-toolbar-custom">{children}</div>, toolbarHost) : null;
}

export function usePageViewport() { return useShell().viewport; }
