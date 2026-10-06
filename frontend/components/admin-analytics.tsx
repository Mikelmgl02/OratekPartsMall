'use client';

import { useEffect, useState } from 'react';
import { Activity, BarChart3, Clock3, Eye, LoaderCircle, RefreshCw, Search, Send, Users } from 'lucide-react';
import StockStatus from './stock-status';
import { request, type CatalogAvailability } from '@/lib/types';

type Daily = { date: string; visits: number; searches: number; views: number; submissions: number };
type Summary = { visits: number; active_users: number; returning_users: number; searches: number; zero_result_searches: number;
  part_views: number; basket_additions: number; submissions: number; supplier_requests: number; requested_units: number;
  reviewed_requests: number; pending_requests: number; pending_over_24h: number; average_review_seconds: number | null };
type Data = { period: { days: number; from: string; to: string }; usage_started_at: string | null; summary: Summary; daily: Daily[];
  top_searches: { term: string; searches: number; users: number; zero_results: number }[];
  missing_searches: { term: string; searches: number; users: number }[];
  top_parts: { id: string; sku: string; description: string; category: string; subcategory: string; views: number;
    basket_additions: number; basket_units: number; requested_units: number; requests: number; availability: CatalogAvailability | null }[];
  suppliers: { id: string; name: string; received: number; reviewed: number; pending: number; pending_over_24h: number;
    average_review_seconds: number | null; oldest_pending_seconds: number | null }[];
  visitors: { username: string; visits: number; searches: number; views: number; last_seen_at: string }[] };
const number = (value: number) => value.toLocaleString('es-PA');
const date = (value: string) => new Date(value).toLocaleDateString('es-PA', { timeZone: 'America/Panama', day: 'numeric', month: 'short' });
const duration = (value: number | null) => value === null ? 'Sin revisiones' : value < 60 ? '< 1 min' : value < 3600 ? `${Math.round(value / 60)} min` : value < 86400 ? `${(value / 3600).toLocaleString('es-PA', { maximumFractionDigits: 1 })} h` : `${(value / 86400).toLocaleString('es-PA', { maximumFractionDigits: 1 })} días`;

export default function AdminAnalytics() {
  const [days, setDays] = useState(30);
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<Data>();
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [metric, setMetric] = useState<'visits' | 'searches' | 'views' | 'submissions'>('visits');
  useEffect(() => {
    const controller = new AbortController(); setBusy(true); setError('');
    request<Data>(`/api/management/analytics?days=${days}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setData(result); })
      .catch(error => { if (!controller.signal.aborted) setError(error.message); })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [days, revision]);
  const s = data?.summary;
  const max = Math.max(1, ...(data?.daily.map(day => day[metric]) || []));
  return <section className="analytics-dashboard" aria-label="Estadísticas" aria-busy={busy}>
    <div className="analytics-heading"><div><h2><BarChart3 size={21}/>Estadísticas</h2><p>Lo que buscan tus clientes y cómo responden los proveedores.</p></div>
      <div className="analytics-filters"><label>Período<select aria-label="Período de estadísticas" value={days} onChange={event => setDays(Number(event.target.value))}><option value={7}>Últimos 7 días</option><option value={30}>Últimos 30 días</option><option value={90}>Últimos 90 días</option></select></label><button className="button soft small" disabled={busy} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} className={busy ? 'spin' : ''}/>Actualizar</button></div>
    </div>
    {error && <div className="notice error" role="alert">{error}<button onClick={() => setRevision(value => value + 1)}>Reintentar</button></div>}
    {!data && busy && <div className="loading" role="status"><LoaderCircle className="spin" size={20}/>Cargando estadísticas…</div>}
    {data && s && <div className={busy ? 'analytics-content updating' : 'analytics-content'}>
      <p className="analytics-period">{date(data.period.from)} – {date(data.period.to)} · Horario de Panamá{busy && <span role="status">Actualizando…</span>}</p>
      <div className="analytics-kpis">
        {[{ title: 'Visitas', value: number(s.visits), note: 'Una visita agrupa la actividad hasta 30 min de inactividad.', Icon: Activity },
          { title: 'Usuarios activos', value: number(s.active_users), note: `${number(s.returning_users)} regresaron · ${s.active_users ? (s.visits / s.active_users).toLocaleString('es-PA', { maximumFractionDigits: 1 }) : '0'} visitas por usuario`, Icon: Users },
          { title: 'Búsquedas', value: number(s.searches), note: `${number(s.zero_result_searches)} sin resultados${s.searches ? ` · ${Math.round(s.zero_result_searches / s.searches * 100)}%` : ''}`, Icon: Search },
          { title: 'Vistas de repuestos', value: number(s.part_views), note: `${number(s.basket_additions)} veces se agregó un artículo a la cesta`, Icon: Eye },
          { title: 'Solicitudes enviadas', value: number(s.submissions), note: `${number(s.supplier_requests)} solicitudes individuales a proveedores`, Icon: Send },
          { title: 'Unidades solicitadas', value: number(s.requested_units), note: 'Cantidades enviadas, sin reservar existencias.', Icon: BarChart3 },
          { title: 'Tiempo hasta revisar', value: duration(s.average_review_seconds), note: `Promedio de ${number(s.reviewed_requests)} solicitudes revisadas`, Icon: Clock3 },
          { title: 'Pendientes del período', value: number(s.pending_requests), note: `${number(s.pending_over_24h)} llevan más de 24 horas`, Icon: Clock3 },
        ].map(({ title, value, note, Icon }) => <article className="analytics-kpi" key={title}><span><Icon size={16}/>{title}</span><strong>{value}</strong><small>{note}</small></article>)}
      </div>
      <section className="analytics-panel"><div className="analytics-panel-heading"><div><h3>Actividad diaria</h3><p>Compara visitas, búsquedas y solicitudes para detectar cambios en la demanda.</p></div><label className="sr-only" htmlFor="analytics-metric">Actividad del gráfico</label><select id="analytics-metric" value={metric} onChange={event => setMetric(event.target.value as typeof metric)}><option value="visits">Visitas</option><option value="searches">Búsquedas</option><option value="views">Vistas de repuestos</option><option value="submissions">Solicitudes enviadas</option></select></div>
        <div className="analytics-chart" role="img" aria-label={`Actividad diaria: ${number(data.daily.reduce((total, day) => total + day[metric], 0))} ${ {visits:'visitas', searches:'búsquedas', views:'vistas', submissions:'solicitudes'}[metric] } en el período`}>
          {data.daily.map(day => <div key={day.date} title={`${date(`${day.date}T12:00:00-05:00`)}: ${number(day[metric])}`}><span style={{ height: `${day[metric] / max * 100}%` }} className={day[metric] ? '' : 'zero'}/></div>)}
        </div><div className="analytics-chart-dates"><span>{date(data.period.from)}</span><span>{date(data.period.to)}</span></div>
      </section>
      {!data.usage_started_at && <p className="notice">Aún no hay navegación registrada. Las visitas de superusuarios y el catálogo de ejemplo se excluyen; las solicitudes existentes sí aparecen.</p>}
      <div className="analytics-columns">
        <section className="analytics-panel"><div className="analytics-panel-heading"><div><h3>Búsquedas más frecuentes</h3><p>Qué códigos, nombres y aplicaciones interesan a los usuarios.</p></div></div>{data.top_searches.length ? <div className="table-scroll"><table><thead><tr><th>Búsqueda</th><th>Veces</th><th>Usuarios</th><th>Sin resultados</th></tr></thead><tbody>{data.top_searches.map(row => <tr key={row.term}><td><strong className="analytics-term">{row.term}</strong></td><td>{number(row.searches)}</td><td>{number(row.users)}</td><td>{number(row.zero_results)}</td></tr>)}</tbody></table></div> : <Empty>Las búsquedas completadas aparecerán aquí.</Empty>}</section>
        <section className="analytics-panel"><div className="analytics-panel-heading"><div><h3>Oportunidades para el catálogo</h3><p>Búsquedas sin resultados: revisa nuevos productos, alternos o descripciones.</p></div></div>{data.missing_searches.length ? <div className="table-scroll"><table><thead><tr><th>Búsqueda sin resultados</th><th>Veces</th><th>Usuarios</th></tr></thead><tbody>{data.missing_searches.map(row => <tr key={row.term}><td><strong className="analytics-term">{row.term}</strong></td><td>{number(row.searches)}</td><td>{number(row.users)}</td></tr>)}</tbody></table></div> : <Empty>Sin búsquedas fallidas registradas en este período.</Empty>}</section>
      </div>
      <section className="analytics-panel"><div className="analytics-panel-heading"><div><h3>Repuestos más vistos y solicitados</h3><p>Ordenados por vistas y luego unidades solicitadas. La disponibilidad corresponde al stock actual.</p></div></div>{data.top_parts.length ? <div className="table-scroll"><table><thead><tr><th>SKU / Descripción</th><th>Vistas</th><th>Unidades agregadas*</th><th>Unidades solicitadas</th><th>Disponibilidad actual</th></tr></thead><tbody>{data.top_parts.map(row => <tr key={row.id}><td><strong>{row.sku}</strong><span>{row.description || 'DESCRIPCIÓN PENDIENTE'}</span><span>{[row.category, row.subcategory].filter(Boolean).join(' / ') || 'SIN CLASIFICAR'}</span></td><td>{number(row.views)}</td><td>{number(row.basket_units)}</td><td>{number(row.requested_units)}</td><td><StockStatus availability={row.availability || undefined}/></td></tr>)}</tbody></table></div> : <Empty>Los repuestos consultados o solicitados aparecerán aquí.</Empty>}<p className="analytics-footnote">* Unidades agregadas durante el período; no es el saldo actual de las cestas. Los SKU unificados conservan sus estadísticas.</p></section>
      <section className="analytics-panel"><div className="analytics-panel-heading"><div><h3>Respuesta de los proveedores</h3><p>Solicitudes recibidas durante el período y su estado actual. Revisar una solicitud todavía no confirma una orden.</p></div></div>{data.suppliers.length ? <div className="table-scroll"><table><thead><tr><th>Proveedor</th><th>Recibidas</th><th>Revisadas</th><th>Pendientes</th><th>Pendientes &gt; 24 h</th><th>Tiempo promedio</th><th>Espera más antigua</th></tr></thead><tbody>{data.suppliers.map(row => <tr key={row.id}><td><strong>{row.name}</strong></td><td>{number(row.received)}</td><td>{number(row.reviewed)} <small>({Math.round(row.reviewed / row.received * 100)}%)</small></td><td>{number(row.pending)}</td><td className={row.pending_over_24h ? 'analytics-attention' : ''}>{number(row.pending_over_24h)}</td><td>{duration(row.average_review_seconds)}</td><td>{row.oldest_pending_seconds === null ? '—' : duration(row.oldest_pending_seconds)}</td></tr>)}</tbody></table></div> : <Empty>La primera solicitud enviada iniciará las estadísticas de respuesta.</Empty>}</section>
      <section className="analytics-panel"><div className="analytics-panel-heading"><div><h3>Frecuencia de visitas</h3><p>Usuarios con más visitas durante el período, incluyendo empleados de cada cuenta.</p></div></div>{data.visitors.length ? <div className="table-scroll"><table><thead><tr><th>Usuario</th><th>Visitas</th><th>Búsquedas</th><th>Vistas de repuestos</th><th>Última actividad</th></tr></thead><tbody>{data.visitors.map(row => <tr key={row.username}><td><strong>{row.username}</strong></td><td>{number(row.visits)}</td><td>{number(row.searches)}</td><td>{number(row.views)}</td><td>{new Date(row.last_seen_at).toLocaleString('es-PA', { timeZone: 'America/Panama', dateStyle: 'short', timeStyle: 'short' })}</td></tr>)}</tbody></table></div> : <Empty>Las visitas de usuarios registrados aparecerán aquí.</Empty>}</section>
      <p className="analytics-footnote">Solo superusuarios. Estadísticas internas de usuarios conectados; no se almacenan direcciones IP ni se usan rastreadores externos. Las solicitudes usan registros existentes; las búsquedas y visitas se recopilan desde esta función{data.usage_started_at ? ` (${date(data.usage_started_at)})` : ''}. El tiempo de revisión se calcula desde el envío hasta la primera revisión y excluye solicitudes pendientes.</p>
    </div>}
  </section>;
}

function Empty({ children }: { children: React.ReactNode }) { return <p className="analytics-empty">{children}</p>; }
