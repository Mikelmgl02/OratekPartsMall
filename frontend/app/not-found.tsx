import Link from 'next/link';

export default function NotFound() {
  return <main className="section-container empty-state">
    <span className="eyebrow">MOTIONPARTES · 404</span>
    <h1>No encontramos esta página.</h1>
    <p>La dirección puede haber cambiado. Vuelve al catálogo para continuar.</p>
    <Link className="button primary" href="/">Volver al catálogo</Link>
  </main>;
}
