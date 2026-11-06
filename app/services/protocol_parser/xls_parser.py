import re
from datetime import datetime

from app.services.xls_reader import read_sheets
from app.services.xlsx_reader import decimal, date_value
from app.services.protocol_parser.models import ProtocolDocument, ProtocolProduct
from app.services.protocol_parser.exceptions import UnsupportedProtocolFormatError


_SERIES_DATE = re.compile(
    r'^\s*(?P<series>[^\s,;/]+)\s+(?:до|годен\s+до)\s+'
    r'(?P<date>\d{2}\.\d{2}\.(?:\d{2}|\d{4}))\s*$', re.I,
)


class XLSProtocolParser:
    """Read a BIFF8 table by its 1..20 numbered regulatory columns.

    Position is inferred from the numbered row; never assume fixed Excel
    letters, and never assign an XLS row to an UPD item by line alone.
    """

    def parse(self, source):
        for rows in read_sheets(source):
            for ix, (_, numbered) in enumerate(rows):
                mapping = {int(v): k for k, v in numbered.items()
                           if re.fullmatch(r'\d+', v) and 1 <= int(v) <= 20}
                if not all(n in mapping for n in (2,3,4,5,6,8)) or len(mapping) < 14:
                    continue
                products = []
                for _, vals in rows[ix+1:]:
                    def field(n):
                        return vals.get(mapping.get(n, ''), '').strip()
                    name = field(2)
                    raw_series = field(3)
                    if not name or not raw_series or 'подпись' in name.casefold():
                        continue
                    match = _SERIES_DATE.fullmatch(raw_series)
                    series = match.group('series') if match else raw_series
                    expiry = None
                    if match:
                        source_date = match.group('date')
                        try:
                            expiry = datetime.strptime(
                                source_date, '%d.%m.%Y' if len(source_date.rsplit('.',1)[-1])==4
                                else '%d.%m.%y'
                            ).strftime('%d.%m.%Y')
                        except ValueError:
                            pass
                    wholesale = next((v for n in (9,10,11)
                                      if (v := decimal(field(n))) is not None), None)
                    products.append(ProtocolProduct(
                        line_number=len(products)+1,
                        name=name, series=series,
                        manufacturer=field(4) or None,
                        manufacturer_regulated_price=decimal(field(5)),
                        manufacturer_actual_price_without_vat=decimal(field(6)),
                        wholesale_price=wholesale,
                        manufacturer_sale_date=date_value(field(8)),
                        expiration_date=expiry,
                    ))
                if products:
                    prefix=' '.join(' '.join(v.values()) for _,v in rows[:ix])
                    basis = re.search(
                        r'(?:накладной|счету-фактуре)\s*№\s*([^\s]+)\s+от\s+'
                        r'(\d{2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+(\d{4})',
                        prefix, re.I,
                    )
                    month_names=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']
                    basis_date=None
                    if basis:
                        basis_date=datetime(int(basis.group(4)),month_names.index(basis.group(3).lower())+1,int(basis.group(2))).date()
                    supplier=re.search(r'(?:ООО|АО|ОАО|ЗАО|OOO)\s*["«][^"»]+["»]',prefix,re.I)
                    return ProtocolDocument(
                        source_format='xls',
                        supplier=supplier.group(0) if supplier else None,
                        basis_number=basis.group(1) if basis else None,
                        basis_date=basis_date,
                        products=products,
                    )
        raise UnsupportedProtocolFormatError('Не обнаружена таблица протокола XLS с графами 1–20')
