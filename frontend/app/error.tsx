'use client';

export default function ErrorPage({ reset }: { reset: () => void }) {
  return <main className="section-container empty-state">
    <span className="eyebrow">MOTIONPARTES</span>
    <h1>No pudimos cargar esta página.</h1>
    <p>Inténtalo de nuevo para continuar.</p>
    <button className="button primary" onClick={reset}>Intentar de nuevo</button>
  </main>;
}
