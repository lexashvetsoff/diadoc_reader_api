import re
from app.services.xlsx_reader import read_sheets, decimal, date_value
from app.services.protocol_parser.models import ProtocolDocument, ProtocolProduct
from app.services.protocol_parser.exceptions import UnsupportedProtocolFormatError


class XLSXProtocolParser:
    """Regulated 20-column price protocol, identified by semantic columns 1..20."""
    def parse(self, source):
        for rows in read_sheets(source):
            for ix,(number_row,cols) in enumerate(rows):
                mapping={int(v):k for k,v in cols.items() if re.fullmatch(r'\d+',v) and 1<=int(v)<=20}
                if not all(n in mapping for n in (2,3,4,5,6,8)):continue
                # Some suppliers duplicate column 1 (row index + INN), ignored.
                # A row of logical numbered headers must include most of the 20 fields.
                if len(mapping)<14:continue
                products=[]
                for _,vals in rows[ix+1:]:
                    name=(vals.get(mapping[2]) or '').strip()
                    series=(vals.get(mapping[3]) or '').strip()
                    if not(name and series):continue
                    # Ignore signature/legal notes and non-product rows.
                    if 'подпись' in name.casefold():continue
                    def field(n):return vals.get(mapping.get(n,''))
                    # Wholesale selling price: first meaningful nonempty among
                    # three taxation categories in columns 9-11.
                    wholesale=next((decimal(field(n)) for n in (9,10,11) if decimal(field(n)) is not None),None)
                    products.append(ProtocolProduct(line_number=len(products)+1,name=name,series=series,
                        manufacturer=field(4),manufacturer_regulated_price=decimal(field(5)),
                        manufacturer_actual_price_without_vat=decimal(field(6)),
                        wholesale_price=wholesale,manufacturer_sale_date=date_value(field(8))))
                if products:
                    prefix=' '.join(' '.join(v.values()) for _,v in rows[:ix])
                    found=re.search(r'к\s+(?:расходной\s+)?накладной\s*№\s*([^\s]+)\s*от\s*(\d{2}\.\d{2}\.\d{4})',prefix,re.I)
                    supplier=re.search(r'(?:ООО|АО|ОАО|ЗАО)\s*["«][^"»]+["»]',prefix,re.I)
                    return ProtocolDocument(source_format='xlsx',supplier=supplier.group(0) if supplier else None,
                        basis_number=found.group(1) if found else None,
                        basis_date=date_value(found.group(2)) if found else None,
                        products=products)
        raise UnsupportedProtocolFormatError('Не обнаружена таблица протокола XLSX с графами 1–20')
