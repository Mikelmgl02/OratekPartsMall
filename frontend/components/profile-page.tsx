'use client';
import { type FormEvent, useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { ArrowLeft, Bell, Building2, Check, KeyRound, LoaderCircle, UserRound } from 'lucide-react';
import AuthModal from './auth-modal';
import { EmailSwitches, PushSwitch, WhatsAppSwitches } from './inbox';
import { Account, request, roleLabel } from '@/lib/types';

// Mi perfil (/perfil): the person's own names and email, their password, how they get notices (the bell's switches) and the accounts they
// belong to. Changing the email or the password asks for the current password; a new password signs out their other devices.
export type ProfileUser = { id: number; username: string; first_name: string; last_name: string; email: string; is_superuser: boolean };
const permissionNames = { owner: 'Propietario', manager: 'Administrador', staff: 'Empleado' };
const failure = (error: unknown) => error instanceof Error && !(error instanceof TypeError) ? error.message : 'Se perdió la conexión. Inténtalo de nuevo.';

type Session = { authenticated: boolean; user?: ProfileUser; accounts?: { results: Account[] } };

// Reads the session in the browser, like the rest of the storefront: signed out, it offers to sign in; signed in, the profile.
export default function ProfileScreen() {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState(false);
  const load = useCallback(() => {
    setError(false);
    request<Session>('/api/session').then(setSession).catch(() => setError(true));
  }, []);
  useEffect(() => { load(); }, [load]);
  if (error) return <ProfileAccess error onRetry={load}/>;
  if (!session) return <main className="section-container profile-page"><p className="loading"><LoaderCircle size={18} className="spin"/>Cargando tu perfil…</p></main>;
  if (!session.authenticated || !session.user) return <ProfileAccess onRetry={load}/>;
  return <ProfilePage user={session.user} accounts={session.accounts?.results ?? []}/>;
}

function ProfileAccess({ error = false, onRetry }: { error?: boolean; onRetry: () => void }) {
  const [authOpen, setAuthOpen] = useState(false);
  return <main className="section-container empty-state admin-access">
    <UserRound size={38}/><span className="eyebrow">MI PERFIL</span>
    <h1>{error ? 'No pudimos cargar tu perfil.' : 'Inicia sesión para ver tu perfil.'}</h1>
    <p>{error ? 'Inténtalo de nuevo en unos momentos.' : 'Aquí cambias tus datos, tu contraseña y cómo te avisamos.'}</p>
    {error ? <button className="button primary" onClick={onRetry}>Intentar de nuevo</button> : <button className="button primary" onClick={() => setAuthOpen(true)}>Iniciar sesión</button>}
    <Link className="button soft" href="/">Volver al catálogo</Link>
    {authOpen && <AuthModal onClose={() => setAuthOpen(false)} onSuccess={async () => { setAuthOpen(false); onRetry(); }}/>}
  </main>;
}

function ProfilePage({ user: initial, accounts }: { user: ProfileUser; accounts: Account[] }) {
  const [user, setUser] = useState(initial);
  const fullName = [user.first_name, user.last_name].filter(Boolean).join(' ');
  const initials = (fullName || user.username).split(/\s+/).map(word => word[0]).join('').slice(0, 2).toUpperCase();
  return <main className="section-container profile-page" aria-label="Mi perfil">
    <Link className="basket-page-back" href="/"><ArrowLeft size={16}/>Volver a MotionPartes</Link>
    <header className="profile-heading">
      <span className="profile-avatar" aria-hidden="true">{initials}</span>
      <div><span className="eyebrow">MI PERFIL</span><h1>{fullName || user.username}</h1><p>@{user.username}{user.is_superuser && ' · Superusuario'}</p></div>
    </header>
    <div className="profile-grid">
      <ProfileDetails user={user} onSaved={setUser}/>
      <PasswordForm/>
      <section className="profile-card" aria-labelledby="profile-notices">
        <h2 id="profile-notices"><Bell size={18}/>Avisos</h2>
        <p className="profile-note">Elige cómo te avisamos de mensajes nuevos y de los cambios en tus órdenes. La campana de MotionPartes siempre los muestra.</p>
        <div className="profile-notices">
          <PushSwitch/>
          <EmailSwitches key={user.email}/>
          <WhatsAppSwitches/>
        </div>
      </section>
      <section className="profile-card" aria-labelledby="profile-accounts">
        <h2 id="profile-accounts"><Building2 size={18}/>Tus cuentas</h2>
        {accounts.length ? <ul className="profile-accounts">{accounts.map(account => <li key={account.id}>
          <strong>{account.name}</strong>
          <span>{account.roles.map(role => roleLabel[role] || role).join(' · ')}</span>
          {account.permission && <small>{permissionNames[account.permission]}</small>}
        </li>)}</ul> : <p className="profile-note">Todavía no perteneces a ninguna cuenta.</p>}
        <p className="profile-note">Las cuentas y sus permisos los asigna un administrador de MotionPartes.</p>
      </section>
    </div>
  </main>;
}

function ProfileDetails({ user, onSaved }: { user: ProfileUser; onSaved: (user: ProfileUser) => void }) {
  const [firstName, setFirstName] = useState(user.first_name);
  const [lastName, setLastName] = useState(user.last_name);
  const [email, setEmail] = useState(user.email);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const emailChanged = email.trim().toLowerCase() !== (user.email || '').toLowerCase();
  const changed = emailChanged || firstName !== user.first_name || lastName !== user.last_name;
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError(''); setSaved(false);
    try {
      const next = await request<ProfileUser>('/api/session/profile', { method: 'PATCH', body: JSON.stringify({ first_name: firstName.trim(), last_name: lastName.trim(),
        ...(emailChanged ? { email: email.trim(), current_password: password } : {}) }) });
      onSaved(next); setFirstName(next.first_name); setLastName(next.last_name); setEmail(next.email); setPassword(''); setSaved(true);
    } catch (caught) { setError(failure(caught)); }
    finally { setBusy(false); }
  }
  return <section className="profile-card" aria-labelledby="profile-details">
    <h2 id="profile-details"><UserRound size={18}/>Tus datos</h2>
    <form className="stack-form" onSubmit={submit}>
      <div className="form-row">
        <label>Nombre<input name="first_name" autoComplete="given-name" maxLength={150} value={firstName} disabled={busy} onChange={event => { setFirstName(event.target.value); setSaved(false); }}/></label>
        <label>Apellido<input name="last_name" autoComplete="family-name" maxLength={150} value={lastName} disabled={busy} onChange={event => { setLastName(event.target.value); setSaved(false); }}/></label>
      </div>
      <label>Usuario<input value={user.username} readOnly aria-describedby="profile-username-note"/></label>
      <small id="profile-username-note" className="profile-note">Con tu usuario inicias sesión; no se puede cambiar.</small>
      <label>Correo electrónico<input name="email" type="email" autoComplete="email" required value={email} disabled={busy} onChange={event => { setEmail(event.target.value); setSaved(false); }}/></label>
      {emailChanged && <label>Contraseña actual<input name="current_password" type="password" autoComplete="current-password" required value={password} disabled={busy}
        onChange={event => setPassword(event.target.value)} aria-describedby="profile-email-note"/></label>}
      {emailChanged && <small id="profile-email-note" className="profile-note">Para cambiar tu correo, confirma tu contraseña. Ahí te enviaremos los avisos por correo.</small>}
      {error && <p className="notice error" role="alert">{error}</p>}
      {saved && <p className="notice success" role="status"><Check size={15}/>Tus datos se guardaron.</p>}
      <div><button type="submit" className="button primary" disabled={busy || !changed || (emailChanged && !password)}>{busy && <LoaderCircle size={15} className="spin"/>}Guardar cambios</button></div>
    </form>
  </section>;
}

function PasswordForm() {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [repeat, setRepeat] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const mismatch = !!repeat && next !== repeat;
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (next !== repeat) return;
    setBusy(true); setError(''); setDone(false);
    try {
      await request('/api/session/password', { method: 'POST', body: JSON.stringify({ current_password: current, new_password: next }) });
      setCurrent(''); setNext(''); setRepeat(''); setDone(true);
    } catch (caught) { setError(failure(caught)); }
    finally { setBusy(false); }
  }
  return <section className="profile-card" aria-labelledby="profile-password">
    <h2 id="profile-password"><KeyRound size={18}/>Contraseña</h2>
    <form className="stack-form" onSubmit={submit}>
      <label>Contraseña actual<input type="password" autoComplete="current-password" required value={current} disabled={busy} onChange={event => { setCurrent(event.target.value); setDone(false); }}/></label>
      <label>Nueva contraseña<input type="password" autoComplete="new-password" required minLength={8} value={next} disabled={busy} onChange={event => { setNext(event.target.value); setDone(false); }}/></label>
      <label>Repite la nueva contraseña<input type="password" autoComplete="new-password" required value={repeat} disabled={busy} aria-invalid={mismatch} onChange={event => setRepeat(event.target.value)}/></label>
      {mismatch && <small className="profile-note warning">Las dos contraseñas nuevas no coinciden.</small>}
      <small className="profile-note">Al menos 8 caracteres, no solo números ni parecida a tu usuario. Al cambiarla cerramos tu sesión en tus otros dispositivos.</small>
      {error && <p className="notice error" role="alert">{error}</p>}
      {done && <p className="notice success" role="status"><Check size={15}/>Tu contraseña cambió. Las sesiones de tus otros dispositivos se cerraron.</p>}
      <div><button type="submit" className="button primary" disabled={busy || !current || !next || mismatch || !repeat}>{busy && <LoaderCircle size={15} className="spin"/>}Cambiar contraseña</button></div>
    </form>
  </section>;
}
