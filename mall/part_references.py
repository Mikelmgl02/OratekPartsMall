"""Supplier declarations are matching evidence, never new catalog aliases."""
import re
from rest_framework import serializers


def normalize_references(value):
    if not isinstance(value, list) or len(value) > 100:
        raise serializers.ValidationError('Ingresa una lista de hasta 100 referencias.')
    result = {}
    for row in value:
        if not isinstance(row, dict) or set(row) - {'code', 'brand', 'ref_type'}:
            raise serializers.ValidationError('Cada referencia debe contener code y, opcionalmente, brand y ref_type (oem, company o unknown).')
        code, brand = row.get('code'), row.get('brand', '')
        if not isinstance(code, str) or not isinstance(brand, str):
            raise serializers.ValidationError('Los códigos y fabricantes deben ser texto.')
        code, brand = code.strip().upper(), brand.strip().upper()
        if not code or len(code) > 120 or len(brand) > 120:
            raise serializers.ValidationError('Cada código es obligatorio y admite hasta 120 caracteres; el fabricante también admite 120.')
        kind = row.get('ref_type')
        if kind is not None and kind not in ['unknown', 'oem', 'company']:
            raise serializers.ValidationError('ref_type debe ser oem, company o unknown.')
        if kind == 'company' and not brand:
            raise serializers.ValidationError('Una referencia de empresa requiere brand, por ejemplo FEBEST.')
        data = {'brand': brand, 'code': code, **({'ref_type': kind} if kind is not None else {})}
        previous = result.get((brand, code))
        if previous and previous.get('ref_type', 'unknown') != data.get('ref_type', 'unknown'):
            raise serializers.ValidationError('La misma referencia contiene tipos contradictorios.')
        result[brand, code] = data
    return [result[key] for key in sorted(result)]


def parse_reference_cell(value):
    """Excel: HYUNDAI:51712-1R000; KIA:51712-0U000; RN11002V."""
    rows = []
    for token in re.split(r'[;\n|]+', value):
        token = token.strip()
        if not token:
            continue
        prefix, sep, rest = token.partition(':')
        kind = {'OEM': 'oem', 'COMPANY': 'company', 'EMPRESA': 'company'}.get(prefix.upper())
        if kind and sep:
            brand, sep, code = rest.partition(':')
            rows.append({'brand': brand if sep else '', 'code': code if sep else brand, 'ref_type': kind})
        else:
            rows.append({'brand': prefix if sep else '', 'code': rest if sep else prefix})
    return normalize_references(rows)


def format_references(rows):
    def text(row):
        value = f'{row["brand"]}:{row["code"]}' if row['brand'] else row['code']
        prefix = {'oem': 'OEM:', 'company': 'COMPANY:'}.get(row.get('ref_type'), '')
        return prefix + value
    return '; '.join(text(row) for row in rows)
