import type { MetadataRoute } from 'next';

// Makes MotionPartes installable (Agregar a pantalla de inicio): on iPhone and iPad, browser push notifications only work in the
// installed app (iOS 16.4+).
export default function manifest(): MetadataRoute.Manifest {
  return {
    id: '/',
    name: 'MotionPartes',
    short_name: 'MotionPartes',
    description: 'Encuentra repuestos, consulta proveedores con existencias y sigue tus órdenes y conversaciones.',
    lang: 'es',
    start_url: '/',
    scope: '/',
    display: 'standalone',
    background_color: '#f8f9f5',
    theme_color: '#ffffff',
    icons: [
      { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/icons/maskable-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
  };
}
