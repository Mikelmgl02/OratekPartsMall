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
    // ?seccion=oem was the OEM section (Biblioteca OEM and Sufijos) until the inventory refactor's S10: its links open Inventario,
    // where Sufijos is a tool and library numbers are References (the Referencia tab).
    const sections: Record<string, AdminSection> = { inventario: 'inventory', alternos: 'alternates', oem: 'inventory', cuentas: 'accounts', usuarios: 'users', invitaciones: 'invitations', estadisticas: 'analytics', acuerdos: 'deals', plantillas: 'templates', aplicaciones: 'applications', casos: 'cases', marcas: 'brands', grupos: 'grupos' };
    const selected = (await searchParams).seccion;
    const section = typeof selected === 'string' && Object.hasOwn(sections, selected) ? sections[selected] : 'accounts';
    return <AdminWorkspace user={profile.data} section={section}/>;
  } catch { return <AdminAccess signedIn error/>; }
}
