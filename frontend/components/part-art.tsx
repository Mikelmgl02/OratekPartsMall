'use client';
import { useId } from 'react';

export default function PartArt({ name, hero = false }: { name: string; hero?: boolean }) {
  const id = useId().replace(/:/g, '');
  const metal = `metal-${id}`, dark = `dark-${id}`, shadow = `shadow-${id}`;
  const lower = name.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  const type = lower.includes('disc') || hero ? 'disc' : (lower.includes('spark') || lower.includes('bujia')) ? 'spark' : (lower.includes('bearing') || lower.includes('rodamiento') || lower.includes('balinera')) ? 'bearing' : (lower.includes('pads') || lower.includes('pastilla')) ? 'pads' : lower.includes('air') && (lower.includes('filter') || lower.includes('filtro')) ? 'air' : (lower.includes('filter') || lower.includes('filtro')) ? 'filter' : 'generic';
  return <svg viewBox="0 0 320 230" className={`part-art ${hero ? 'hero-art' : ''}`} role="img" aria-label={hero ? 'Ilustración de un disco de freno ventilado' : `Ilustración de ${name}`}>
    <defs>
      <linearGradient id={metal} x1="0" y1="0" x2="1" y2="1"><stop stopColor="#fafcfb"/><stop offset=".32" stopColor="#c8ceca"/><stop offset=".58" stopColor="#f2f3ee"/><stop offset="1" stopColor="#7d8780"/></linearGradient>
      <linearGradient id={dark} x1="0" y1="0" x2="1" y2=".8"><stop stopColor="#677169"/><stop offset=".55" stopColor="#303b33"/><stop offset="1" stopColor="#17251d"/></linearGradient>
      <filter id={shadow} x="-50%" y="-50%" width="200%" height="200%"><feDropShadow dx="0" dy="14" stdDeviation="9" floodColor="#182d21" floodOpacity=".18"/></filter>
    </defs>
    <ellipse cx="160" cy="195" rx="95" ry="12" fill="#26372c" opacity=".07"/>
    <g filter={`url(#${shadow})`}>
      {type === 'generic' && <g transform="translate(75 47)"><path d="M0 38l85-35 85 35-85 36z" fill="#dce7c0" stroke="#839877" strokeWidth="2"/><path d="M0 38v90l85 35V74z" fill="#bacb9e" stroke="#839877" strokeWidth="2"/><path d="M170 38v90l-85 35V74z" fill="#97af80" stroke="#839877" strokeWidth="2"/><path d="M42 20l85 35v45l-18 8V63L25 27" fill="#eaf0d8" opacity=".65"/><path d="M23 106l35 14m-35-4l26 10" stroke="#788d6b" strokeWidth="3"/></g>}
      {type === 'disc' && <g transform="translate(160 110) rotate(-25) scale(1 .86)">
        <circle cy="12" r="94" fill="#69756d"/><circle r="94" fill={`url(#${metal})`} stroke="#e3e8df" strokeWidth="2"/>
        <circle r="85" fill="none" stroke="#88958b" strokeWidth="1"/><circle r="80" fill="none" stroke="#fff" opacity=".5"/>
        <circle r="73" fill="none" stroke="#a0aba1" strokeWidth="1"/><circle r="66" fill="none" stroke="#a4aea6"/>
        {Array.from({length: 24}, (_, i) => <g key={i} transform={`rotate(${i*15})`}><circle cx="0" cy="-77" r="2.5" fill="#626f65"/><circle cx="-10" cy="-62" r="2" fill="#626f65"/></g>)}
        <circle cy="7" r="47" fill="#616e63"/><circle r="47" fill={`url(#${metal})`} stroke="#78867b"/>
        <circle r="25" fill={`url(#${dark})`}/><circle r="20" fill={hero ? '#d5eb94' : '#edece4'}/>
        {Array.from({length: 5}, (_, i) => <circle key={i} transform={`rotate(${i*72})`} cy="-35" r="5" fill="#3c4b40"/>)}
      </g>}
      {type === 'filter' && <g transform="translate(99 39) rotate(12 61 77)"><path d="M0 19h122v110c0 32-122 32-122 0z" fill={`url(#${dark})`}/><ellipse cx="61" cy="19" rx="61" ry="20" fill={`url(#${metal})`}/><ellipse cx="61" cy="19" rx="23" ry="11" fill="#27392c"/><path d="M10 50h102v45H10z" fill="#dbe891"/><text x="61" y="72" textAnchor="middle" fontSize="8" fill="#25302a" fontWeight="800">FILTRO DE ACEITE</text><path d="M20 82h80" stroke="#66764e" strokeWidth="2"/><path d="M12 116q48 12 98 0" fill="none" stroke="#839484" opacity=".5"/></g>}
      {type === 'spark' && <g transform="translate(125 11) rotate(25 30 100)"><rect x="22" y="0" width="17" height="34" rx="5" fill={`url(#${metal})`}/><path d="M17 31h27v91H17z" fill="#f8f7ee" stroke="#c7cabd"/>{Array.from({length: 7},(_,i)=><rect key={i} x="12" y={36+i*8} width="36" height="5" rx="2" fill="#e2e3d8"/>)}<path d="M10 109h40l8 15-8 16H10l-8-16z" fill={`url(#${metal})`} stroke="#697a6c"/><rect x="17" y="139" width="26" height="48" fill={`url(#${metal})`}/>{Array.from({length: 8},(_,i)=><path key={i} d={`M17 ${141+i*6}h26`} stroke="#748278" strokeWidth="2"/>)}<path d="M20 187v9h20v-14" fill="none" stroke="#414f43" strokeWidth="5"/><rect x="28" y="181" width="5" height="13" fill="#d7dace"/></g>}
      {type === 'bearing' && <g transform="translate(160 108) rotate(-20) scale(1 .83)"><circle cy="11" r="86" fill="#55615a"/><circle r="86" fill={`url(#${metal})`} stroke="#97a296"/><circle r="65" fill={`url(#${dark})`}/>{Array.from({length: 13},(_,i)=><circle key={i} transform={`rotate(${i*360/13})`} cy="-56" r="11" fill={`url(#${metal})`} stroke="#536456"/>)}<circle r="44" fill={`url(#${metal})`} stroke="#728475" strokeWidth="2"/><circle r="28" fill="#edece4" stroke="#56675b" strokeWidth="6"/></g>}
      {type === 'pads' && <g transform="translate(45 50) rotate(-12 100 70)">{[0,1].map(i=><g key={i} transform={`translate(${i*58} ${i*52})`}><path d="M0 31l21-27h109l18 27v46H0z" fill={`url(#${dark})`} stroke="#8f998e" strokeWidth="4"/><path d="M12 34l20-19h87l17 19v29H12z" fill="#858c77"/><path d="M74 15v48" stroke="#354535" strokeWidth="3"/><rect x="-9" y="38" width="11" height="21" fill={`url(#${metal})`}/><rect x="148" y="38" width="11" height="21" fill={`url(#${metal})`}/></g>)}</g>}
      {type === 'air' && <g transform="translate(55 40) rotate(-12 100 70)"><path d="M0 0h205v146H0z" fill="#32483b" stroke="#778f6f" strokeWidth="8"/><rect x="10" y="10" width="185" height="126" fill="#d4cbaa"/>{Array.from({length: 22},(_,i)=><g key={i}><path d={`M${16+i*8} 13v121`} stroke="#f0eacf" strokeWidth="4"/><path d={`M${19+i*8} 13v121`} stroke="#a29673" strokeWidth="1"/></g>)}</g>}
    </g>
  </svg>;
}
