'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { ArrowLeft, ArrowUpRight, BadgeCheck, BarChart3, Boxes, Building2, Check, ChevronRight, Copy, LoaderCircle, LogOut, MailPlus, Menu, PanelLeftClose, PanelLeftOpen, Plus, Repeat2, Search, ShieldCheck, Users, X } from 'lucide-react';
import BrandLogo from './brand-logo';
import Modal from './modal';
import AdminCatalogSection from './admin-catalog-section';
import AdminOEMSection from './admin-oem-section';
import AdminAnalytics from './admin-analytics';
import AdminTechnical from './admin-technical';
import { AppToolbar, usePageViewport } from './app-shell';
import { AdminSection, ManagedAccount, ManagedInvitation, ManagedRole, ManagedUser, Member, Page, SessionUser, request } from '@/lib/types';

type Row = ManagedAccount | ManagedUser | ManagedInvitation;
const base = '/api/management';
const permissionNames = { owner: 'Propietario', manager: 'Administrador', staff: 'Empleado' };
const formatDate = (date: string) => new Date(date).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'medium', timeStyle: 'short' });
const message = (error: unknown) => error instanceof Error ? error.message : 'No se pudo guardar el cambio. Inténtalo de nuevo.';

const SIDEBAR_KEY = 'motionpartes.admin.sidebar.collapsed';

export default function AdminWorkspace({ user, section = 'accounts' }: { user: SessionUser; section?: AdminSection }) {
  const router = useRouter();
  const viewport = usePageViewport();
  const tab = section;
  const isAccessSection = ['accounts', 'users', 'invitations'].includes(tab);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Desktop only: the side menu folds into an icon rail so wide grids get the whole window. Remembered in this browser.
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => { try { setCollapsed(localStorage.getItem(SIDEBAR_KEY) === '1'); } catch { /* storage blocked: start expanded */ } }, []);
  function toggleSidebar() {
    setCollapsed(value => { try { localStorage.setItem(SIDEBAR_KEY, value ? '0' : '1'); } catch { /* not remembered */ } return !value; });
  }
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [loadedData, setLoadedData] = useState<{ section: AdminSection; page: Page<Row> } | null>(null);
  const data = loadedData?.section === tab ? loadedData.page : null;
  const [roles, setRoles] = useState<ManagedRole[]>([]);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [accountEditor, setAccountEditor] = useState<ManagedAccount | null | undefined>();
  const [userEditor, setUserEditor] = useState<ManagedUser | null>(null);
  const [memberEditor, setMemberEditor] = useState<ManagedAccount | null>(null);
  const [assignUser, setAssignUser] = useState<ManagedUser | null>(null);
  const [inviting, setInviting] = useState(false);
  const [invitation, setInvitation] = useState<ManagedInvitation | null>(null);
  const [revoke, setRevoke] = useState<ManagedInvitation | null>(null);

  useEffect(() => {
    let cancelled = false;
    request<ManagedRole[]>(`${base}/roles`).then(result => { if (!cancelled) setRoles(result); }).catch(error => { if (!cancelled) setError(message(error)); });
    return () => { cancelled = true; };
  }, [revision]);
  useEffect(() => { setSearch(''); setQuery(''); setPage(1); setNotice(''); setSidebarOpen(false); viewport.current?.scrollTo({ top: 0, behavior: 'instant' }); }, [tab, viewport]);
  useEffect(() => {
    const timer = setTimeout(() => { setQuery(search.trim()); setPage(1); }, 300);
    return () => clearTimeout(timer);
  }, [search]);
  useEffect(() => {
    if (!isAccessSection) return;
    let cancelled = false; setLoadedData(null); setError('');
    request<Page<Row>>(`${base}/${tab}?search=${encodeURIComponent(query)}&page=${page}`).then(result => { if (!cancelled) setLoadedData({ section: tab, page: result }); }).catch(error => { if (!cancelled) setError(message(error)); });
    return () => { cancelled = true; };
  }, [tab, query, page, revision, isAccessSection]);
  function saved(text: string) { setRevision(value => value + 1); setNotice(text); }
  async function logout() {
    try { await request('/api/session', { method: 'DELETE' }); router.replace('/'); router.refresh(); }
    catch (error) { setError(message(error)); }
  }
  async function manageAccount(id: string) {
    try { const account = await request<ManagedAccount>(`${base}/accounts/${id}`); setUserEditor(null); setMemberEditor(account); }
    catch (error) { setError(message(error)); }
  }
  const heading = { inventory: 'Inventario', alternates: 'Alternos', oem: 'OEM', accounts: 'Cuentas', users: 'Usuarios', invitations: 'Invitaciones', analytics: 'Estadísticas', templates: 'Plantillas técnicas', applications: 'Aplicaciones vehiculares' }[tab];
  const navigation = [
    { key: 'inventory', slug: 'inventario', text: 'Inventario', Icon: Boxes, group: 'Catálogo' },
    { key: 'alternates', slug: 'alternos', text: 'Alternos', Icon: Repeat2, group: 'Catálogo' },
    { key: 'oem', slug: 'oem', text: 'OEM', Icon: BadgeCheck, group: 'Catálogo' },
    { key: 'templates', slug: 'plantillas', text: 'Plantillas técnicas', Icon: Boxes, group: 'Catálogo' },
    { key: 'applications', slug: 'aplicaciones', text: 'Aplicaciones vehiculares', Icon: Repeat2, group: 'Catálogo' },
    { key: 'accounts', slug: 'cuentas', text: 'Cuentas', Icon: Building2, group: 'Accesos' },
    { key: 'users', slug: 'usuarios', text: 'Usuarios', Icon: Users, group: 'Accesos' },
    { key: 'invitations', slug: 'invitaciones', text: 'Invitaciones', Icon: MailPlus, group: 'Accesos' },
    { key: 'analytics', slug: 'estadisticas', text: 'Estadísticas', Icon: BarChart3, group: 'Actividad' },
  ];
  return <>
    <AppToolbar><header className="header"><div className="header-inner admin-header">
      <Link className="wordmark" aria-label="Inicio de MotionPartes" href="/"><BrandLogo/></Link>
      <div className="admin-header-actions"><Link className="button soft small" href="/"><ArrowLeft size={15}/>Volver al catálogo</Link><button className="icon-button" aria-label="Cerrar sesión" title="Cerrar sesión" onClick={logout}><LogOut size={19}/></button></div>
    </div></header></AppToolbar>
    <div className={`admin-layout section-container${collapsed ? ' is-collapsed' : ''}`}>
      <button className="admin-menu-toggle" aria-expanded={sidebarOpen} aria-controls="admin-side-menu" onClick={() => setSidebarOpen(!sidebarOpen)}><Menu size={18}/><span>Menú administrativo</span><small>{heading}</small></button>
      <aside id="admin-side-menu" className={`admin-sidebar ${sidebarOpen ? 'is-open' : ''}`}>
        <div className="admin-sidebar-title"><ShieldCheck size={21}/><div><strong>Administración</strong><span>MotionPartes</span></div><button type="button" className="admin-sidebar-collapse" aria-label={collapsed ? 'Mostrar menú lateral' : 'Ocultar menú lateral'} title={collapsed ? 'Mostrar menú lateral' : 'Ocultar menú lateral'} onClick={toggleSidebar}>{collapsed ? <PanelLeftOpen size={16}/> : <PanelLeftClose size={16}/>}</button></div>
        <nav aria-label="Gestión administrativa">{['Catálogo', 'Accesos', 'Actividad'].map(group => <div className="admin-nav-group" key={group}><span>{group}</span>{navigation.filter(item => item.group === group).map(({ key, slug, text, Icon }) => <Link key={key} href={`/administracion?seccion=${slug}`} aria-current={tab === key ? 'page' : undefined} className={tab === key ? 'selected' : ''} title={collapsed ? text : undefined} onClick={() => setSidebarOpen(false)}><Icon size={18}/><span>{text}</span>{tab === key && <ChevronRight size={14}/>}</Link>)}</div>)}</nav>
        <div className="admin-sidebar-user"><span className="live-dot"/><div><strong>{user.username}</strong><span>Superusuario</span></div></div>
      </aside>
      <main className="workspace admin-workspace">
        {/* The side menu already names the area and the signed-in superuser; the page keeps its title for assistive technology. */}
        <h1 className="sr-only">Administración</h1>
      {notice && <div className="notice success admin-notice" role="status"><Check size={16}/>{notice}<button className="icon-button" aria-label="Cerrar notificación" onClick={() => setNotice('')}><X size={15}/></button></div>}
      {isAccessSection ? <><section className="inventory-panel admin-panel" aria-label={heading}>
        <div className="admin-toolbar"><div><h2>{heading}</h2><p>{tab === 'accounts' ? 'Tipos de cuenta, estado y empleados con acceso.' : tab === 'users' ? 'Perfiles registrados y sus cuentas asignadas.' : 'Acceso por invitación para nuevos usuarios.'}</p></div><button className="button primary" onClick={() => { if (tab === 'accounts') setAccountEditor(null); else setInviting(true); }}>{tab === 'accounts' ? <Plus size={17}/> : <MailPlus size={17}/>} {tab === 'accounts' ? 'Crear cuenta' : 'Invitar usuario'}</button></div>
        <div className="catalog-search admin-search"><Search size={19}/><label className="sr-only" htmlFor="admin-search">Buscar {heading.toLowerCase()}</label><input autoComplete="off" id="admin-search" value={search} onChange={event => setSearch(event.target.value)} placeholder={tab === 'accounts' ? 'Buscar por nombre de cuenta…' : tab === 'users' ? 'Buscar por usuario, nombre o correo…' : 'Buscar por correo electrónico…'}/></div>
        {error && <div className="notice error" role="alert">{error}<button onClick={() => setRevision(value => value + 1)}>Intentar de nuevo</button></div>}
        {!data && !error && <div className="loading"><LoaderCircle className="spin" size={20}/>Cargando {heading.toLowerCase()}…</div>}
        {data && !data.results.length && <div className="empty-state"><Users size={30}/><h3>{query ? 'No encontramos resultados.' : tab === 'accounts' ? 'Crea la primera cuenta.' : tab === 'invitations' ? 'Invita a tu próximo usuario.' : 'Todavía no hay usuarios.'}</h3><p>{query ? 'Prueba con otra búsqueda.' : tab === 'accounts' ? 'Asigna sus tipos de cliente o proveedor y agrega sus empleados.' : 'Los usuarios nuevos se registran mediante una invitación.'}</p></div>}
        {!!data?.results.length && <div className="table-scroll"><table>
          <thead>{tab === 'accounts' ? <tr><th>Cuenta</th><th>Tipos de cuenta</th><th>Estado</th><th>Usuarios</th><th>Acciones</th></tr> : tab === 'users' ? <tr><th>Usuario</th><th>Correo electrónico</th><th>Estado</th><th>Cuentas</th><th>Acciones</th></tr> : <tr><th>Correo electrónico</th><th>Estado</th><th>Vence</th><th>Acciones</th></tr>}</thead>
          <tbody>{data?.results.map(row => {
            if (tab === 'accounts') { const account = row as ManagedAccount; return <tr key={account.id}><td><strong>{account.name}</strong></td><td><div className="admin-role-list">{account.roles.length ? account.roles.map(code => <span key={code}>{roles.find(role => role.code === code)?.name || code}</span>) : <span>Sin tipos asignados</span>}</div></td><td><span className={`status ${account.active ? 'matched' : ''}`}>{account.active ? 'Activa' : 'Inactiva'}</span></td><td>{account.member_count}</td><td><div className="admin-row-actions"><button className="button soft small" aria-label={`Editar cuenta ${account.name}`} onClick={() => setAccountEditor(account)}>Editar</button><button className="button text small" aria-label={`Gestionar usuarios de ${account.name}`} onClick={() => setMemberEditor(account)}><Users size={14}/>Usuarios</button></div></td></tr>; }
            if (tab === 'users') { const managed = row as ManagedUser; return <tr key={managed.id}><td><strong>{managed.username}</strong><span>{[managed.first_name, managed.last_name].filter(Boolean).join(' ') || 'Sin nombre completo'}{managed.is_superuser ? ' · Superusuario' : ''}</span></td><td>{managed.email}</td><td><span className={`status ${managed.is_active ? 'matched' : ''}`}>{managed.is_active ? 'Activo' : 'Inactivo'}</span></td><td><div className="admin-role-list">{managed.memberships.length ? managed.memberships.map(member => <span key={member.id}>{member.account_name}</span>) : <span>Sin cuenta asignada</span>}</div></td><td><div className="admin-row-actions"><button className="button soft small" aria-label={`Editar usuario ${managed.username}`} onClick={() => setUserEditor(managed)}>Editar</button><button className="button text small" aria-label={`Asignar cuenta a ${managed.username}`} onClick={() => setAssignUser(managed)}><Plus size={14}/>Asignar cuenta</button></div></td></tr>; }
            const invite = row as ManagedInvitation; return <tr key={invite.id}><td><strong>{invite.email}</strong></td><td><span className={`status ${invite.status === 'accepted' ? 'matched' : invite.status === 'expired' ? 'review' : ''}`}>{{pending: 'Pendiente', accepted: 'Aceptada', expired: 'Vencida'}[invite.status]}</span></td><td>{formatDate(invite.expires_at)}</td><td>{invite.status === 'pending' && <div className="admin-row-actions"><button className="button soft small" aria-label={`Ver código para ${invite.email}`} onClick={() => setInvitation(invite)}>Ver código</button><button className="button text small danger-text" aria-label={`Revocar invitación de ${invite.email}`} onClick={() => setRevoke(invite)}>Revocar</button></div>}</td></tr>;
          })}</tbody>
        </table></div>}
        {data && <div className="pagination"><span>{data.count} {data.count === 1 ? 'resultado' : 'resultados'}</span><div><button className="button soft small" disabled={!data.previous} onClick={() => setPage(page - 1)}>Anterior</button><span>Página {page}</span><button className="button soft small" disabled={!data.next} onClick={() => setPage(page + 1)}>Siguiente</button></div></div>}
      </section>
      <p className="admin-footnote">Las cuentas pueden tener varios tipos de cliente o proveedor. Cada usuario puede acceder a varias cuentas con su propio permiso.</p>
      </> : tab === 'analytics' ? <AdminAnalytics/> : tab === 'oem' ? <AdminOEMSection/> : tab === 'templates' || tab === 'applications' ? <AdminTechnical key={tab} applications={tab === 'applications'}/> : <AdminCatalogSection key={tab} section={tab as 'inventory' | 'alternates'}/>}
      </main>
    </div>
    {accountEditor !== undefined && <AccountEditor account={accountEditor} roles={roles} onClose={() => setAccountEditor(undefined)} onSaved={() => { setAccountEditor(undefined); saved('Cuenta guardada.'); }}/>}
    {userEditor && <UserEditor user={userEditor} onClose={() => setUserEditor(null)} onSaved={() => { setUserEditor(null); saved('Usuario actualizado.'); }} onManage={manageAccount}/>}
    {memberEditor && <AccountMembers account={memberEditor} onClose={() => setMemberEditor(null)} onSaved={() => saved('Accesos de la cuenta actualizados.')}/>}
    {assignUser && <AssignAccount user={assignUser} onClose={() => setAssignUser(null)} onSaved={() => { setAssignUser(null); saved('Cuenta asignada al usuario.'); }}/>}
    {inviting && <InvitationForm onClose={() => setInviting(false)} onSaved={invite => { setInviting(false); setInvitation(invite); saved('Invitación creada.'); }}/>}
    {invitation && <InvitationCode invitation={invitation} onClose={() => setInvitation(null)}/>}
    {revoke && <RevokeInvitation invitation={revoke} onClose={() => setRevoke(null)} onSaved={() => { setRevoke(null); saved('Invitación revocada.'); }}/>}
  </>;
}

function AccountEditor({ account, roles, onClose, onSaved }: { account: ManagedAccount | null; roles: ManagedRole[]; onClose: () => void; onSaved: () => void }) {
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState(account?.roles ?? []);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setBusy(true);
    const form = new FormData(event.currentTarget);
    try { await request(`${base}/accounts${account ? `/${account.id}` : ''}`, { method: account ? 'PATCH' : 'POST', body: JSON.stringify({ name: form.get('name'), active: form.has('active'), roles: selected }) }); onSaved(); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <Modal title={account ? `Editar cuenta · ${account.name}` : 'Crear cuenta'} onClose={onClose}><form autoComplete="off" className="stack-form" onSubmit={submit}>
    <label>Nombre de la cuenta<input autoComplete="off" name="name" defaultValue={account?.name} required maxLength={200} placeholder="Ej.: Repuestos Central"/></label>
    <fieldset className="admin-role-options"><legend>Tipos de cuenta</legend><p>Selecciona todos los tipos que correspondan.</p>{roles.map(role => <label key={role.code}><input autoComplete="off" type="checkbox" checked={selected.includes(role.code)} onChange={event => setSelected(event.target.checked ? [...selected, role.code] : selected.filter(code => code !== role.code))}/><span>{role.name}<small>{role.capability === 'supplier' ? 'Puede publicar inventario' : 'Puede solicitar cotizaciones'}</small></span></label>)}{!roles.length && <p>No se pudieron cargar los tipos de cuenta. Cierra esta ventana e inténtalo de nuevo.</p>}</fieldset>
    <label className="admin-checkbox"><input autoComplete="off" name="active" type="checkbox" defaultChecked={account?.active ?? true}/>Cuenta activa</label><p className="form-footnote">Una cuenta inactiva deja de estar disponible para sus usuarios y sus existencias dejan de aparecer a los clientes.</p>
    <FormError error={error}/><SaveButton busy={busy} disabled={!roles.length}>Guardar cuenta</SaveButton>
  </form></Modal>;
}

function UserEditor({ user, onClose, onSaved, onManage }: { user: ManagedUser; onClose: () => void; onSaved: () => void; onManage: (id: string) => void }) {
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setBusy(true); const form = new FormData(event.currentTarget);
    try { await request(`${base}/users/${user.id}`, { method: 'PATCH', body: JSON.stringify({ email: form.get('email'), first_name: form.get('first_name'), last_name: form.get('last_name'), is_active: user.is_superuser || form.has('is_active') }) }); onSaved(); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <Modal title={`Editar usuario · ${user.username}`} onClose={onClose}><form autoComplete="off" className="stack-form" onSubmit={submit}>
    <div className="form-row"><label>Nombre<input autoComplete="off" name="first_name" defaultValue={user.first_name} maxLength={150}/></label><label>Apellido<input autoComplete="off" name="last_name" defaultValue={user.last_name} maxLength={150}/></label></div><label>Correo electrónico<input autoComplete="off" name="email" type="email" required defaultValue={user.email}/></label>
    <label className="admin-checkbox"><input autoComplete="off" type="checkbox" name="is_active" defaultChecked={user.is_active} disabled={user.is_superuser}/>Usuario activo</label><p className="form-footnote">{user.is_superuser ? 'Este usuario es superusuario y debe mantenerse activo.' : 'Al desactivarlo se bloqueará su acceso a todas sus cuentas.'}</p>
    {user.memberships.length > 0 && <div className="admin-user-accounts"><h3>Cuentas asignadas</h3>{user.memberships.map(member => <div key={member.id}><span>{member.account_name}<small>{member.permission_label}</small></span><button type="button" className="button text small" onClick={() => onManage(member.account)}>Gestionar</button></div>)}</div>}
    <FormError error={error}/><SaveButton busy={busy}>Guardar usuario</SaveButton>
  </form></Modal>;
}

function useChoices<T>(endpoint: string, search: string) {
  const [choices, setChoices] = useState<Page<T> | null>(null); const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false; setChoices(null); setError('');
    const timer = setTimeout(() => { request<Page<T>>(`${base}/${endpoint}?search=${encodeURIComponent(search)}`).then(result => { if (!cancelled) setChoices(result); }).catch(error => { if (!cancelled) setError(message(error)); }); }, 250);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [endpoint, search]);
  return { choices, error };
}
function PermissionSelect({ value, onChange, disabled = false, name = 'permission', label = 'Permiso en la cuenta' }: { value?: Member['permission']; onChange?: (value: Member['permission']) => void; disabled?: boolean; name?: string; label?: string }) {
  return <label>{label}<select aria-label={label} name={name} value={value} defaultValue={value ? undefined : 'staff'} disabled={disabled} onChange={event => onChange?.(event.target.value as Member['permission'])}>{Object.entries(permissionNames).map(([code, text]) => <option key={code} value={code}>{text}</option>)}</select></label>;
}
function AccountMembers({ account: initial, onClose, onSaved }: { account: ManagedAccount; onClose: () => void; onSaved: () => void }) {
  const [account, setAccount] = useState(initial); const [search, setSearch] = useState(''); const [selected, setSelected] = useState('');
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [removing, setRemoving] = useState<number | null>(null);
  const { choices, error: choiceError } = useChoices<ManagedUser>('users', search);
  const available = choices?.results.filter(user => !account.members.some(member => member.user === user.id)) ?? [];
  async function mutate(path: string, method: string, body?: object) {
    setError(''); setBusy(true);
    try { await request(`${base}/${path}`, { method, body: body ? JSON.stringify(body) : undefined }); setAccount(await request<ManagedAccount>(`${base}/accounts/${account.id}`)); setSelected(''); setRemoving(null); onSaved(); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <Modal title={`Usuarios de ${account.name}`} onClose={onClose} wide><p className="modal-description">Cada empleado inicia sesión con su propio usuario. Asigna aquí su acceso a esta cuenta.</p><div className="admin-member-list">{!account.members.length && <p className="form-footnote">Esta cuenta todavía no tiene usuarios.</p>}{account.members.map(member => <div className="admin-member" key={member.id}><div><strong>{member.username}</strong><small>{member.email}{!member.user_active ? ' · Inactivo' : ''}</small></div><PermissionSelect value={member.permission} label={`Permiso de ${member.username}`} disabled={busy} onChange={permission => mutate(`memberships/${member.id}`, 'PATCH', { permission })}/>{removing === member.id ? <div className="admin-row-actions"><button className="button soft small danger-text" disabled={busy} onClick={() => mutate(`memberships/${member.id}`, 'DELETE')}>Confirmar retiro</button><button className="button text small" onClick={() => setRemoving(null)}>Cancelar</button></div> : <button className="button text small danger-text" disabled={busy} aria-label={`Retirar acceso de ${member.username}`} onClick={() => setRemoving(member.id)}>Retirar acceso</button>}</div>)}</div>
    <form autoComplete="off" className="stack-form admin-add-member" onSubmit={event => { event.preventDefault(); const form = new FormData(event.currentTarget); mutate('memberships', 'POST', { user: Number(selected), account: account.id, permission: form.get('permission') }); }}>
      <h3>Agregar un usuario registrado</h3><label>Buscar usuario<input autoComplete="off" value={search} onChange={event => { setSearch(event.target.value); setSelected(''); }} placeholder="Nombre de usuario o correo electrónico"/></label>
      <label>Usuario para agregar<select required value={selected} onChange={event => setSelected(event.target.value)} disabled={!choices || busy}><option value="">{!choices ? 'Cargando usuarios…' : 'Selecciona un usuario'}</option>{available.map(user => <option key={user.id} value={user.id}>{user.username} · {user.email}</option>)}</select></label>{choices?.next && <p className="form-footnote">Hay más usuarios. Afina la búsqueda para encontrar al que necesitas.</p>}
      <PermissionSelect/><FormError error={error || choiceError}/><SaveButton busy={busy} disabled={!selected}>Agregar a la cuenta</SaveButton>
    </form></Modal>;
}
function AssignAccount({ user, onClose, onSaved }: { user: ManagedUser; onClose: () => void; onSaved: () => void }) {
  const [search, setSearch] = useState(''); const [selected, setSelected] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const { choices, error: choiceError } = useChoices<ManagedAccount>('accounts', search);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(''); const form = new FormData(event.currentTarget);
    try { await request(`${base}/memberships`, { method: 'POST', body: JSON.stringify({ user: user.id, account: selected, permission: form.get('permission') }) }); onSaved(); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <Modal title={`Asignar cuenta · ${user.username}`} onClose={onClose}><form autoComplete="off" className="stack-form" onSubmit={submit}><label>Buscar cuenta<input autoComplete="off" value={search} onChange={event => { setSearch(event.target.value); setSelected(''); }} placeholder="Nombre de la cuenta"/></label><label>Cuenta para asignar<select required value={selected} onChange={event => setSelected(event.target.value)} disabled={!choices || busy}><option value="">{!choices ? 'Cargando cuentas…' : 'Selecciona una cuenta'}</option>{choices?.results.filter(account => !user.memberships.some(member => member.account === account.id)).map(account => <option key={account.id} value={account.id}>{account.name}{!account.active ? ' · Inactiva' : ''}</option>)}</select></label>{choices?.next && <p className="form-footnote">Hay más cuentas. Afina la búsqueda para encontrar la que necesitas.</p>}<PermissionSelect/><FormError error={error || choiceError}/><SaveButton busy={busy} disabled={!selected}>Asignar cuenta</SaveButton></form></Modal>;
}
function InvitationForm({ onClose, onSaved }: { onClose: () => void; onSaved: (invite: ManagedInvitation) => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(''); const form = new FormData(event.currentTarget);
    try { onSaved(await request<ManagedInvitation>(`${base}/invitations`, { method: 'POST', body: JSON.stringify({ email: form.get('email'), days: Number(form.get('days')) }) })); }
    catch (error) { setError(message(error)); } finally { setBusy(false); }
  }
  return <Modal title="Invitar a un usuario" onClose={onClose}><p className="modal-description">Crea un código de registro para un nuevo usuario. Después podrás asignarle sus cuentas y permisos.</p><form autoComplete="off" className="stack-form" onSubmit={submit}><label>Correo electrónico del invitado<input autoComplete="off" name="email" type="email" required placeholder="usuario@empresa.com"/></label><label>Vigencia en días<input autoComplete="off" name="days" type="number" min={1} max={30} step={1} defaultValue={7} required/></label><FormError error={error}/><SaveButton busy={busy}>Crear invitación</SaveButton></form></Modal>;
}
function InvitationCode({ invitation, onClose }: { invitation: ManagedInvitation; onClose: () => void }) {
  const [copied, setCopied] = useState(false); const [error, setError] = useState('');
  async function copy() { try { await navigator.clipboard.writeText(invitation.token); setCopied(true); } catch { setError('Selecciona y copia el código manualmente.'); } }
  return <Modal title="Código de invitación" onClose={onClose}><p className="modal-description">Comparte este código con <strong>{invitation.email}</strong> para que se registre en MotionPartes.</p><label className="admin-code-label">Código de registro<input autoComplete="off" value={invitation.token} readOnly onFocus={event => event.target.select()}/></label><p className="form-footnote">Vence el {formatDate(invitation.expires_at)}. Después del registro, asigna al usuario sus cuentas.</p><FormError error={error}/><button className="button primary full" onClick={copy}>{copied ? <Check size={17}/> : <Copy size={17}/>} {copied ? 'Código copiado' : 'Copiar código'}</button></Modal>;
}
function RevokeInvitation({ invitation, onClose, onSaved }: { invitation: ManagedInvitation; onClose: () => void; onSaved: () => void }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function revoke() { setBusy(true); setError(''); try { await request(`${base}/invitations/${invitation.id}/revoke`, { method: 'PATCH', body: '{}' }); onSaved(); } catch (error) { setError(message(error)); } finally { setBusy(false); } }
  return <Modal title="Revocar invitación" onClose={onClose}><p className="modal-description">El código de <strong>{invitation.email}</strong> dejará de permitir el registro. Puedes crear una nueva invitación más adelante.</p><FormError error={error}/><button className="button dark full" disabled={busy} onClick={revoke}>{busy && <LoaderCircle size={16} className="spin"/>}Revocar invitación</button></Modal>;
}
function FormError({ error }: { error: string }) { return error ? <div className="notice error" role="alert">{error}</div> : null; }
function SaveButton({ busy, disabled = false, children }: { busy: boolean; disabled?: boolean; children: React.ReactNode }) { return <button className="button primary full" disabled={busy || disabled}>{busy && <LoaderCircle className="spin" size={17}/>} {children}<ArrowUpRight size={16}/></button>; }
