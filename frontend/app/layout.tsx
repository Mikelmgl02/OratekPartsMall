import type { Metadata, Viewport } from 'next';
import AppShell from '@/components/app-shell';
import DevToolsLink from '@/components/dev-tools-link';
import '@fontsource/dm-sans/400.css';
import '@fontsource/dm-sans/500.css';
import '@fontsource/dm-sans/600.css';
import '@fontsource/dm-sans/700.css';
import '@fontsource/manrope/500.css';
import '@fontsource/manrope/600.css';
import '@fontsource/manrope/700.css';
import '@fontsource/manrope/800.css';
import './globals.css';
export const metadata: Metadata = {
  title: 'MotionPartes — Encuentra tu repuesto. Elige tu proveedor.',
  description: 'Encuentra repuestos, consulta proveedores con existencias y prepara solicitudes de cotización privadas en MotionPartes, por Oratek.',
  // Added to the home screen, it opens as its own app (where iPhone allows browser push notifications).
  appleWebApp: { capable: true, title: 'MotionPartes', statusBarStyle: 'default' },
};
export const viewport: Viewport = { themeColor: '#ffffff' };
export default function Layout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="es"><body><AppShell>{children}</AppShell><DevToolsLink/></body></html>;
}
