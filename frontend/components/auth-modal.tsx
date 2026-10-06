'use client';
import { useState } from 'react';
import { ArrowUpRight, KeyRound, LoaderCircle } from 'lucide-react';
import Modal from './modal';
import { request } from '@/lib/types';
export default function AuthModal({ onClose, onSuccess, initialMode = 'login' }: { onClose: () => void; onSuccess: () => Promise<void>; initialMode?: 'login' | 'signup' }) {
  const [mode, setMode] = useState<'login' | 'signup'>(initialMode);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setSuccess(''); setBusy(true);
    const fields = Object.fromEntries(new FormData(event.currentTarget));
    try {
      if (mode === 'signup') {
        await request('/api/signup', { method: 'POST', body: JSON.stringify(fields) });
        setSuccess('Tu registro está completo. Tu administrador configurará tu cuenta antes de que puedas consultar el inventario real.');
        setMode('login');
      } else {
        await request('/api/session', { method: 'POST', body: JSON.stringify(fields) });
        await onSuccess();
        onClose();
      }
    } catch (error) { setError(error instanceof Error ? error.message : 'No se pudo iniciar sesión.'); }
    finally { setBusy(false); }
  }
  return <Modal title={mode === 'login' ? 'Bienvenido a MotionPartes' : 'Acepta tu invitación'} onClose={onClose}>
    <div className="auth-intro"><span className="round-icon"><KeyRound size={24}/></span><p>{mode === 'login' ? 'Inicia sesión para consultar existencias reales y gestionar tus repuestos.' : 'Utiliza el código de invitación que te compartió tu administrador.'}</p></div>
    <div className="segmented"><button className={mode === 'login' ? 'selected' : ''} onClick={() => {setMode('login'); setError('');}}>Iniciar sesión</button><button className={mode === 'signup' ? 'selected' : ''} onClick={() => {setMode('signup'); setError('');}}>Tengo una invitación</button></div>
    <form onSubmit={submit} className="stack-form">
      {mode === 'signup' && <><label>Código de invitación<input name="token" required placeholder="Tu código de invitación" autoComplete="off"/></label><label>Correo electrónico<input type="email" name="email" required autoComplete="email" placeholder="tu@empresa.com"/></label></>}
      <label>Usuario<input name="username" required maxLength={150} autoComplete="username" placeholder="Tu usuario"/></label>
      <label>Contraseña<input type="password" name="password" required minLength={mode === 'signup' ? 8 : undefined} autoComplete={mode === 'signup' ? 'new-password' : 'current-password'} placeholder="Introduce tu contraseña"/></label>
      {error && <div className="notice error" role="alert">{error}</div>}
      {success && <div className="notice success" role="status">{success}</div>}
      <button className="button primary full" disabled={busy}>{busy ? <LoaderCircle className="spin" size={18}/> : null}{mode === 'login' ? 'Iniciar sesión' : 'Crear cuenta'}<ArrowUpRight size={18}/></button>
    </form>
    <p className="form-footnote">El acceso es por invitación. Tu administrador asigna los roles de tu cuenta.</p>
  </Modal>;
}
