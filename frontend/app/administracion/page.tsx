import AdminWorkspace from '@/components/admin-workspace';
import AdminAccess from '@/components/admin-access';
import { django, sessionToken } from '@/lib/server-api';
import { AdminSection } from '@/lib/types';

export const metadata = { title: 'Administración — MotionPartes' };

export default async function AdministrationPage({ searchParams }: { searchParams: Promise<{ seccion?: string }> }) {
  const token = await sessionToken();
  if (!token) return <AdminAccess signedIn={false}/>;
  try {
    const profile = await django('auth/me/', {}, token);
    if (profile.status === 401) return <AdminAccess signedIn={false}/>;
    if (profile.status !== 200) return <AdminAccess signedIn error/>;
    if (!profile.data.is_superuser) return <AdminAccess signedIn/>;
    const sections: Record<string, AdminSection> = { inventario: 'inventory', alternos: 'alternates', cuentas: 'accounts', usuarios: 'users', invitaciones: 'invitations', estadisticas: 'analytics', plantillas: 'templates', aplicaciones: 'applications' };
    const selected = (await searchParams).seccion;
    const section = typeof selected === 'string' && Object.hasOwn(sections, selected) ? sections[selected] : 'accounts';
    return <AdminWorkspace user={profile.data} section={section}/>;
  } catch { return <AdminAccess signedIn error/>; }
}
