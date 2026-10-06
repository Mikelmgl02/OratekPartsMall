"""Recover Excel-coerced identifiers using evidence in the uploaded workbook."""
import re
from collections import defaultdict
from datetime import date, datetime


def date_identifier(cell):
    return cell.data_type not in ('f', 'e') and cell.is_date and isinstance(cell.value, (date, datetime))


def description_text(cells, columns):
    cell = cells[columns['description']] if 'description' in columns else None
    return cell.value.strip().upper() if cell and cell.data_type == 's' and isinstance(cell.value, str) else ''


def recovery_evidence(sheet, columns):
    """Count distinct intact code shapes; repeated alternos are not independent evidence."""
    patterns = defaultdict(set)
    stems = defaultdict(set)
    date_cells = set()
    for row, cells in enumerate(sheet.iter_rows(min_row=2, max_col=20), 2):
        description = description_text(cells, columns)
        stem = description.split(' ', 1)[0]
        for field in ('sku', 'code'):
            if field not in columns:
                continue
            cell = cells[columns[field]]
            if date_identifier(cell):
                date_cells.add((row, field))
            elif isinstance(cell.value, str) and cell.data_type == 's':
                text = cell.value.strip().upper()
                match = re.fullmatch(r'(\d{1,4})-(\d{1,4})', text)
                if match:
                    shape = (field, match[1], len(match[2]))
                    patterns[shape].add(text)
                    if stem:
                        stems[(*shape, stem)].add(text)
    return patterns, stems, date_cells


def date_recovery(cell, *, row, column, field, description, evidence):
    value = cell.value
    mask = re.sub(r'\[[^\]]*\]', '', cell.number_format or '').lower().strip()
    month, day, year = value.month, value.day, value.year
    candidates = []

    def add(code):
        if code not in candidates:
            candidates.append(code)

    month_year = day == 1 and re.fullmatch(r'm{1,4}[-/ ]y{2,4}', mask) is not None
    if month_year:
        # Both 10-21 and 10-2021 can become the same Excel date. Keep
        # alternatives until descriptions or intact code families distinguish them.
        full = f'{month}-{year:04}'
        short = f'{month}-{year % 100:02}'
        if 1900 <= year <= 2099:
            add(short)
        add(full)
        add(short)
        add(f'{month:02}-{year:04}')
        add(f'{year:04}-{month}')
    elif re.fullmatch(r'd{1,2}[-/ ]m{1,4}', mask):
        add(f'{month}-{day}')
        add(f'{day}-{month}')
        add(f'{month:02}-{day:02}')
        add(f'{day:02}-{month:02}')
    else:
        for first, second in ((month, day), (day, month)):
            add(f'{first}-{second}-{year % 100:02}')
            add(f'{first}-{second}-{year % 100}')
            add(f'{first:02}-{second:02}-{year % 100:02}')
            add(f'{first}-{second}-{year:04}')
        add(f'{year:04}-{month:02}-{day:02}')

    automatic = False
    reason = 'Excel cambió el formato. Revisa el código sugerido o excluye esta fila aquí.'
    # A complete code repeated in the description is stronger than a format
    # guess. Never use incidental model years as evidence.
    tokens = set(re.findall(r'(?<![\w/-])\d{1,4}(?:-\d{1,4}){1,2}(?![\w/-])', description))
    exact = [code for code in candidates if code in tokens and (
        (month_year and year > 2099 and code.endswith(f'-{year:04}'))
        or re.search(rf'\b(?:SKU|CODIGO|CÓDIGO|REF(?:ERENCIA)?)\s*[:#]?\s*{re.escape(code)}\b', description)
    )]
    if len(exact) == 1:
        suggested = exact[0]
        automatic = True
        reason = 'El código completo aparece en la descripción del repuesto.'
    else:
        suggested = candidates[0]
        if month_year:
            patterns, stems, _ = evidence
            stem = description.split(' ', 1)[0]
            supported = []
            for code in candidates:
                match = re.fullmatch(r'(\d{1,4})-(\d{1,4})', code)
                if not match:
                    continue
                shape = (field, match[1], len(match[2]))
                # Require multiple distinct intact examples in the same part
                # family, or at least three throughout this workbook.
                if len(stems.get((*shape, stem), ())) >= 2 or len(patterns.get(shape, ())) >= 3:
                    supported.append(code)
            if len(supported) == 1:
                suggested = supported[0]
                automatic = True
                reason = 'Reconstruido con el patrón de códigos de texto intactos de este archivo.'
    return {'row': row, 'column': column, 'description': description,
            'excel_value': f'{value.isoformat()} ({cell.number_format})',
            'suggested_code': suggested, 'candidates': candidates, 'reason': reason,
            'automatic': automatic, 'resolved': automatic, 'skipped': False,
            'value': suggested if automatic else ''}
