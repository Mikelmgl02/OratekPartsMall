'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import AuthModal from './auth-modal';

export default function AdminAccess({ signedIn, error = false }: { signedIn: boolean; error?: boolean }) {
  const [authOpen, setAuthOpen] = useState(false);
  const router = useRouter();
  return <main className="section-container empty-state admin-access">
    <ShieldCheck size={38}/><span className="eyebrow">ADMINISTRACIÓN DE MOTIONPARTES</span>
    <h1>{error ? 'No pudimos comprobar tu acceso.' : 'Acceso exclusivo para superusuarios.'}</h1>
    <p>{error ? 'Inténtalo de nuevo en unos momentos.' : signedIn ? 'Tu usuario no tiene permiso para administrar cuentas y usuarios.' : 'Inicia sesión con un usuario autorizado para continuar.'}</p>
    {error ? <button className="button primary" onClick={() => router.refresh()}>Intentar de nuevo</button> : !signedIn && <button className="button primary" onClick={() => setAuthOpen(true)}>Iniciar sesión</button>}
    <Link className="button soft" href="/">Volver al catálogo</Link>
    {authOpen && <AuthModal onClose={() => setAuthOpen(false)} onSuccess={async () => router.refresh()}/>}
  </main>;
}
