"""Legacy-merge audit, read-only (S0.8 of the inventory refactor): the matching worker's automatic SKU merges the new rules would not make.

    python -m mall.refactor.s0_merge_audit --report DIR

Every source of the CatalogMerge rows the matching service made (1,388 merges, 2,378 sources) goes through rule 3 again: its code,
read with the suffix table as D4 leaves it (applied in memory while S0.1 is pending), strips its trailing TAGs while each is
auto-eligible, and what is left must be the SKU the worker merged into (a rename since does not count). Then the 3c guards compare
it with the target as the worker saw them (the MatchingDecision evidence, else today's SKU and descriptions): the same description
head noun, and no spec_conflict (identical texts never contradict each other). A source fails on the first of: lifecycle chain, unknown chain, reverted token (one of the 45 D4
tokens), variant chain, TAG not auto-eligible, base differs, head differs, spec conflict. A merge with a failing source is one the
new rules would not make; it joined different parts when two of its sources (lifecycle copies aside) carry different variant words,
like 04111-22152-AS with 04111-22152-M. DIR gets summary.json, resumen.txt, merges.csv (each such merge, with its state today),
sources.csv (each failing source) and different-parts.csv (the merges for OD18). It reads in one read-only transaction and writes
nothing to the database; the owner decides what to separate (OD18) with Separar SKU, origin automatic.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter

SERVICE_ACCOUNT = 'motionpartes-matching-service'  # mall.catalog_split.SERVICE_ACCOUNT
REASONS = {'lifecycle': 'Cadena de ciclo de vida (ANULADO): la regla 3 nunca la quita', 'unknown': 'Cadena con sufijos desconocidos',
           'reverted': 'Palabra de posición o medida revertida por D4 (-L, -RH, -AS, -M…)', 'variant': 'Cadena con otra variante',
           'tag': 'Etiqueta que no se puede quitar sola', 'base': 'La base no es el SKU destino',
           'head': 'El sustantivo de la descripción no coincide', 'spec': 'Las especificaciones se contradicen (spec_conflict)'}
# The plan's read-only simulation (2026-10-13, D4 in memory, before the GMB fix).
EXPECTED = {'merges': 1388, 'sources': 2378, 'failing_merges': 217, 'failing_sources': 265, 'different_parts': 34,
            'reasons': {'lifecycle': 190, 'reverted': 59, 'unknown': 11, 'variant': 4, 'base': 1}}


def compact(code):
    return re.sub(r'[^A-Z0-9]', '', (code or '').upper())


def audit_table():
    """(the suffix table after D4, tokens D4 still has to revert in the database)."""
    from .. import catalog_suffixes as cs
    from .s0_suffix_revert import plan, reverted_rows
    rows, state = cs._db_rows(), cs.table_state()
    if not rows:
        return cs.SuffixTable.from_fixture(), 0
    entries = plan()
    return (cs.SuffixTable(reverted_rows(rows, entries), source='db', version=state and state[0]),
            sum(e['status'] == 'revert' for e in entries))


def judge(sku, description, target_sku, target_description, table, d4):
    """Rule 3 and the 3c guards on one source: its reason ('' when the new rules make the merge too), chain, chain class, D4 tokens
    and variant words."""
    from ..catalog_suffixes import description_head, strip_tags
    from ..matching_engine import description as clean, spec_conflict
    head = description_head(description)
    base, chain, klass = strip_tags(sku, head=head, table=table)
    variants = sorted({c['entry'].canon for c in chain if c['entry'].cls == 'VARIANT' and c['entry'].kind != 'alt_oem_tail'})
    reverted = [token for token in variants if token in d4]
    target_head = description_head(target_description)
    same_text = clean(description) == clean(target_description)
    if klass == 'lifecycle':
        reason = 'lifecycle'
    elif klass == 'unknown':
        reason = 'unknown'
    elif klass == 'variant':
        reason = 'reverted' if reverted else 'variant'
    elif not all(c['entry'].auto_eligible for c in chain):
        reason = 'tag'
    elif compact(base) != compact(target_sku):
        reason = 'base'
    elif head[0] and target_head[0] and head != target_head:
        reason = 'head'
    elif not same_text and spec_conflict(description, target_description):
        reason = 'spec'
    else:
        reason = ''
    return {'reason': reason, 'chain': [c['tok'] for c in chain], 'class': klass, 'reverted': reverted,
            'variants': variants if klass != 'lifecycle' else [],
            # today's spec_conflict on one identical text (LH/RH both named, S0.2 fixes it): set aside, counted apart
            'same_text_conflict': not reason and same_text and bool(description) and spec_conflict(description, target_description)}


def state(source_id, target, parts):
    """What became of a merged source: still under the target (or the target's canonical SKU), restored, elsewhere or gone."""
    part = parts.get(source_id)
    if part is None:
        return 'no existe'
    if not part.merged_into_id:
        return 'separado' if part.active else 'retirado'
    return 'unificado' if part.merged_into_id in (target.pk, target.merged_into_id) else 'unificado en otro SKU'


def audit():
    """Every automatic merge judged source by source; read-only."""
    from ..matching_models import MatchingDecision
    from ..models import CatalogMerge, Part
    from .s0_suffix_revert import TOKENS
    table, pending = audit_table()
    d4 = set(TOKENS)
    merges = list(CatalogMerge.objects.filter(actor__username=SERVICE_ACCOUNT).select_related('target').order_by('created_at', 'pk'))
    ids = [str(m.pk) for m in merges]
    evidence = {str(d.evidence.get('merge_id')): d.evidence for d in
                MatchingDecision.objects.filter(evidence__merge_id__in=ids).only('evidence')} if ids else {}
    source_ids = {str(s.get('id')) for m in merges for s in m.sources}
    parts = {str(p.pk): p for p in Part.objects.filter(pk__in=source_ids).only('id', 'sku', 'description', 'active', 'merged_into')}
    rows = []
    for merge in merges:
        ev = evidence.get(str(merge.pk), {})
        # the target as the worker merged into it: a rename since (an AUTO_RENAME_BASE apply) never reads as "base differs"
        target_sku = (ev.get('target') or {}).get('sku') or merge.target.sku
        target_description = (ev.get('target') or {}).get('description') or merge.target.description
        seen = {str(s.get('id')): s for s in ev.get('sources') or []}
        sources = []
        for source in merge.sources:
            pk = str(source.get('id'))
            part = parts.get(pk)
            description = (seen.get(pk) or {}).get('description') or (part.description if part else '')
            sources.append({'id': pk, 'sku': source.get('sku', ''), 'description': description, 'state': state(pk, merge.target, parts),
                            **judge(source.get('sku', ''), description, target_sku, target_description, table, d4)})
        failing = [s for s in sources if s['reason']]
        words = {tuple(s['variants']) for s in sources if s['class'] != 'lifecycle'}
        rows.append({'merge': merge, 'method': ev.get('method', ''), 'target_sku': target_sku, 'target_description': target_description,
                     'sources': sources, 'failing': failing, 'different_parts': bool(failing) and len(words) >= 2})
    return {'rows': rows, 'pending_d4': pending, 'table_version': table.version}


def target_state(target):
    if target.merged_into_id:
        return 'unificado'
    return 'activo' if target.active else 'retirado'


def merge_state(row):
    states = {s['state'] for s in row['failing']}
    return states.pop() if len(states) == 1 else 'mixto'


def summarize(result):
    rows = result['rows']
    failing = [r for r in rows if r['failing']]
    reasons = Counter(s['reason'] for r in rows for s in r['failing'])
    return {'merges': len(rows), 'sources': sum(len(r['sources']) for r in rows), 'failing_merges': len(failing),
            'failing_sources': sum(len(r['failing']) for r in failing), 'different_parts': sum(r['different_parts'] for r in rows),
            'reasons': {reason: reasons[reason] for reason in REASONS if reasons[reason]},
            'merges_by_reason': {reason: sum(any(s['reason'] == reason for s in r['failing']) for r in failing) for reason in REASONS
                                 if reasons[reason]},
            'same_text_spec_conflicts': sum(s['same_text_conflict'] for r in rows for s in r['sources']),
            'failing_targets_created': sum(bool(r['merge'].summary.get('target_created')) for r in failing),
            'state_today': dict(Counter(merge_state(r) for r in failing)),
            'different_parts_state_today': dict(Counter(merge_state(r) for r in failing if r['different_parts'])),
            'pending_d4_tokens': result['pending_d4'], 'suffix_table_version': result['table_version']}


def write_report(directory, result, summary):
    os.makedirs(directory, exist_ok=True)

    def table(name, header, lines):
        with open(os.path.join(directory, name), 'w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            writer.writerows(lines)

    failing = [r for r in result['rows'] if r['failing']]
    table('merges.csv', ['id_union', 'fecha', 'metodo', 'sku_destino', 'sku_destino_hoy', 'destino_creado', 'destino_hoy', 'fuentes',
                         'fuentes_que_fallan', 'motivos', 'piezas_distintas', 'estado_hoy', 'skus_que_fallan', 'otras_fuentes', 'descripcion_destino'],
          [[r['merge'].pk, r['merge'].created_at.isoformat(), r['method'], r['target_sku'], r['merge'].target.sku,
            'sí' if r['merge'].summary.get('target_created') else 'no', target_state(r['merge'].target), len(r['sources']), len(r['failing']),
            ' | '.join(sorted({s['reason'] for s in r['failing']})),
            'sí' if r['different_parts'] else 'no', merge_state(r), ' '.join(s['sku'] for s in r['failing']),
            ' '.join(s['sku'] for s in r['sources'] if not s['reason']), r['target_description']] for r in failing])
    table('sources.csv', ['id_union', 'sku_destino', 'sku_fuente', 'id_fuente', 'motivo', 'detalle', 'cadena', 'clase_cadena', 'palabras_d4',
                          'variantes', 'estado_hoy', 'descripcion_fuente', 'descripcion_destino'],
          [[r['merge'].pk, r['target_sku'], s['sku'], s['id'], s['reason'], REASONS[s['reason']], '-'.join(s['chain']), s['class'],
            ' '.join(s['reverted']), ' '.join(s['variants']), s['state'], s['description'], r['target_description']]
           for r in failing for s in r['failing']])
    table('different-parts.csv', ['id_union', 'sku_destino', 'destino_creado', 'estado_hoy', 'fuentes_con_variantes', 'descripcion_destino'],
          [[r['merge'].pk, r['target_sku'], 'sí' if r['merge'].summary.get('target_created') else 'no', merge_state(r),
            ' | '.join(f"{s['sku']} ({' '.join(s['variants']) or 'sin variante'})" for s in r['sources'] if s['class'] != 'lifecycle'),
            r['target_description']] for r in failing if r['different_parts']])
    with open(os.path.join(directory, 'summary.json'), 'w', encoding='utf-8') as handle:
        json.dump({'summary': summary, 'expected': EXPECTED}, handle, ensure_ascii=False, indent=2)
    with open(os.path.join(directory, 'resumen.txt'), 'w', encoding='utf-8') as handle:
        handle.write(render(summary))


def render(summary):
    e = EXPECTED
    lines = [f"Auditoría de uniones automáticas (S0.8) · tabla de sufijos v{summary['suffix_table_version']} · "
             + ('D4 aplicada en memoria: faltan %d sufijos por revertir en la base' % summary['pending_d4_tokens']
                if summary['pending_d4_tokens'] else 'D4 ya aplicada en la base'),
             f"Uniones automáticas: {summary['merges']} (plan {e['merges']}) · fuentes: {summary['sources']} (plan {e['sources']})",
             f"Las reglas nuevas no harían {summary['failing_merges']} uniones (plan ≈{e['failing_merges']}): {summary['failing_sources']} fuentes "
             f"(plan {e['failing_sources']})",
             f"Unieron piezas distintas: {summary['different_parts']} (plan {e['different_parts']})",
             'Fuentes por motivo (uniones):']
    for reason, count in summary['reasons'].items():
        planned = e['reasons'].get(reason)
        merges = summary['merges_by_reason'][reason]
        lines.append(f"  {REASONS[reason]}: {count} ({merges} {'unión' if merges == 1 else 'uniones'})"
                     + (f' · plan {planned}' if planned is not None else ''))
    lines.append(f"Destino creado por el servicio: {summary['failing_targets_created']} de {summary['failing_merges']}")
    if summary['same_text_spec_conflicts']:
        lines.append(f"Aparte: spec_conflict de hoy marca {summary['same_text_spec_conflicts']} fuentes cuya descripción es idéntica a la del "
                     'destino (nombran LH y RH a la vez; S0.2 lo corrige); un texto no se contradice a sí mismo, así que no cuentan.')
    lines.append('Estado hoy: ' + ', '.join(f'{k} {v}' for k, v in sorted(summary['state_today'].items())))
    lines.append('Piezas distintas, estado hoy: ' + ', '.join(f'{k} {v}' for k, v in sorted(summary['different_parts_state_today'].items())))
    lines.append('Siguiente: el dueño decide qué separar (OD18) con Separar SKU (Unificados, origen automático); lo que quede '
                 'pasa a un lote legacy-auto-merge en S6.')
    return '\n'.join(lines) + '\n'


def run(report_dir=None, stdout=None):
    from django.db import connection, transaction
    stdout = stdout or sys.stdout
    with transaction.atomic():
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
        result = audit()
        summary = summarize(result)
    if report_dir:
        write_report(report_dir, result, summary)
    stdout.write(render(summary))
    return summary


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Audita, sin escribir nada, las uniones automáticas de SKU que las reglas nuevas no harían.')
    parser.add_argument('--report', help='Carpeta donde escribir el informe (CSV, summary.json y resumen.txt).')
    parser.add_argument('--json', action='store_true', help='Imprime también el resumen en JSON.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    stdout = stdout or sys.stdout
    summary = run(options.report, stdout=stdout)
    if options.json:
        stdout.write(json.dumps(summary, ensure_ascii=False) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
