// Minimal XLSX fixtures keep real multipart-upload tests portable.
function crc32(bytes: Buffer) {
  let value = 0xffffffff;
  for (const byte of bytes) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0);
  }
  return (value ^ 0xffffffff) >>> 0;
}
function zip(entries: [string, string][]) {
  const local: Buffer[] = [], directory: Buffer[] = []; let offset = 0;
  for (const [path, content] of entries) {
    const name = Buffer.from(path), data = Buffer.from(content), checksum = crc32(data);
    const header = Buffer.alloc(30);
    header.writeUInt32LE(0x04034b50); header.writeUInt16LE(20, 4); header.writeUInt32LE(checksum, 14);
    header.writeUInt32LE(data.length, 18); header.writeUInt32LE(data.length, 22); header.writeUInt16LE(name.length, 26);
    const record = Buffer.alloc(46);
    record.writeUInt32LE(0x02014b50); record.writeUInt16LE(20, 4); record.writeUInt16LE(20, 6);
    record.writeUInt32LE(checksum, 16); record.writeUInt32LE(data.length, 20); record.writeUInt32LE(data.length, 24);
    record.writeUInt16LE(name.length, 28); record.writeUInt32LE(offset, 42);
    local.push(header, name, data); directory.push(record, name); offset += header.length + name.length + data.length;
  }
  const central = Buffer.concat(directory), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(central.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, central, end]);
}
const escape = (value: string) => value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
type DateCell = { date: Date; numberFormat: string };
export type ExcelFixtureCell = string | number | DateCell;

export function excelDate(year: number, month: number, day: number, numberFormat = 'd-mmm'): DateCell {
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(0, 0, 0, 0);
  return { date, numberFormat };
}

function excelDateSerial(date: Date) {
  const milliseconds = date.getTime();
  if (!Number.isFinite(milliseconds)) throw new Error('Excel date fixtures must contain valid dates.');
  let serial = (milliseconds - Date.UTC(1899, 11, 30)) / 86400000;
  // Excel's 1900 date system contains a fictitious February 29.
  if (serial > 0 && serial < 61) serial--;
  return serial;
}

export function excelFixture(rows: ExcelFixtureCell[][]) {
  const data = [['SKU', 'NOMBRE', 'DESCRIPCION', 'CODIGO_ALTERNO', 'MARCA_ALTERNO', 'ACTIVO'], ...rows];
  const dateFormats = [...new Set(rows.flatMap(row => row.filter((cell): cell is DateCell => typeof cell === 'object').map(cell => cell.numberFormat)))];
  const xmlRows = data.map((row, index) => `<row r="${index + 1}">${row.map((cell, column) => {
    const reference = `${String.fromCharCode(65 + column)}${index + 1}`;
    if (typeof cell === 'number') {
      if (!Number.isFinite(cell)) throw new Error('Excel numeric fixtures must contain finite values.');
      return `<c r="${reference}" t="n"><v>${cell}</v></c>`;
    }
    if (typeof cell === 'object') return `<c r="${reference}" t="n" s="${dateFormats.indexOf(cell.numberFormat) + 1}"><v>${excelDateSerial(cell.date)}</v></c>`;
    return `<c r="${reference}" t="inlineStr"><is><t xml:space="preserve">${escape(cell)}</t></is></c>`;
  }).join('')}</row>`).join('');
  const dateStyles = dateFormats.map((format, index) => `<numFmt numFmtId="${164 + index}" formatCode="${escape(format)}"/>`).join('');
  const cellStyles = dateFormats.map((_, index) => `<xf numFmtId="${164 + index}" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>`).join('');
  const styles = `<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="${dateFormats.length}">${dateStyles}</numFmts><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="${dateFormats.length + 1}"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>${cellStyles}</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`;
  return zip([
    ['[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'],
    ['_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'],
    ['xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Inventario" sheetId="1" r:id="rId1"/></sheets></workbook>'],
    ['xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'],
    ['xl/worksheets/sheet1.xml', `<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>${xmlRows}</sheetData></worksheet>`],
    ['xl/styles.xml', styles],
  ]);
}
