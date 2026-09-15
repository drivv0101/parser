"""Minimal XLSX writer: text stays text, even when a name begins with '='."""
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape


def workbook(data):
    rows = [["Материал", "Потребность", "Единица", "Магазин", "Товар", "Упаковок", "Остаток", "Стоимость, ₽", "Статус", "Ссылка"]]
    for line in data["lines"]:
        best = line["best"] or {}
        rows.append([line["query"], line["qty"], line["qty_unit"], best.get("store", ""), best.get("name", ""),
                     best.get("packages", ""), best.get("surplus", ""), best.get("line_total", ""),
                     "Рассчитано" if line["status"] == "ok" else "Требуется проверка", best.get("url", "")])
    rows.extend([["Товары", data["goods_total"]], ["Доставка", data["delivery_total"]],
                 ["Итого", data["optimal_total"]], ["Не учтено позиций", len(data["unresolved"])]] )
    def cell(value):
        if isinstance(value, (float, int)):
            return f'<c><v>{value}</v></c>'
        text = ''.join(c for c in str(value) if ord(c) >= 32 or c in "\t\n\r")
        return '<c t="inlineStr"><is><t xml:space="preserve">' + escape(text) + '</t></is></c>'
    sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cols><col min="1" max="1" width="35" customWidth="1"/><col min="2" max="10" width="22" customWidth="1"/></cols><sheetData>'
    sheet += ''.join('<row>' + ''.join(cell(v) for v in row) + '</row>' for row in rows)
    sheet += '</sheetData></worksheet>'
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Смета" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return output.getvalue()
