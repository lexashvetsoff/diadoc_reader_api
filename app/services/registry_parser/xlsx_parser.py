import re
from app.services.xlsx_reader import read_sheets, decimal
from app.services.registry_parser.exceptions import RegistryParserError
from app.services.registry_parser.models import RegistryDocument, RegistryProduct


class XLSXRegistryParser:
    """A column-labelled certificate registry, independent of absolute row numbers."""
    def parse(self, source):
        return self.parse_sheets(read_sheets(source), source_format='xlsx')

    def parse_sheets(self, sheets, *, source_format='xlsx'):
        for rows in sheets:
            for ix,(rowno,cols) in enumerate(rows):
                headings={col:re.sub(r'\s+',' ',val).casefold() for col,val in cols.items()}
                def col(label):return next((c for c,t in headings.items() if label in t),None)
                name=col('наименование и краткая') or col('наименование товара')
                series=col('серия'); expiry=col('срок годности'); producer=col('завод-изготовитель') or col('производитель')
                qty=col('кол-во') or col('количество')
                if not(name and series and expiry):continue
                number=next((c for c,t in headings.items() if t in {'№','№ п/п','номер'}),None)
                products=[]
                for _,c in rows[ix+1:]:
                    label=c.get(name,'').strip(); batch=c.get(series,'').strip()
                    if not label or not batch:continue
                    if number and not re.fullmatch(r'\d+',c.get(number,'')):continue
                    if not number and not c.get(expiry):continue
                    products.append(RegistryProduct(line_number=int(c[number]) if number else len(products)+1,
                        name=label,series=re.sub(r'\s+МДЛП$','',batch,flags=re.I),
                        manufacturer=c.get(producer) if producer else None,
                        quantity=decimal(c.get(qty)) if qty else None,
                        expiration_date=c.get(expiry) or None))
                if products:
                    text=' '.join(' '.join(c.values()) for _,c in rows[:ix])
                    m=re.search(r'(?:накладной|сч[её]ту-фактуре|сопроводительный документ)\s*№\s*([^\s]+)\s*от\s*(\d{2}\.\d{2}\.(?:\d{2}|\d{4}))',text,re.I)
                    supplier=re.search(r'(?:Поставщик|Грузоотправитель):\s*(.*?)(?=Покупатель:|Грузополучатель:|$)',text,re.I)
                    buyer=re.search(r'(?:Покупатель|Грузополучатель):\s*(.*?)(?=Поставщик:|Грузоотправитель:|$)',text,re.I)
                    invoice_date=m.group(2) if m else None
                    if invoice_date and len(invoice_date.rsplit('.', 1)[-1]) == 2:
                        from datetime import datetime
                        invoice_date=datetime.strptime(invoice_date, '%d.%m.%y').strftime('%d.%m.%Y')
                    return RegistryDocument(source_layout=f'{source_format}:column_headers',layout_confidence=1.0,layout_source=source_format,
                        supplier=supplier.group(1).split(',')[0].strip() if supplier else None,
                        buyer=buyer.group(1).split(',')[0].strip() if buyer else None,
                        invoice_number=m.group(1) if m else None,invoice_date=invoice_date,products=products)
        raise RegistryParserError(f'В {source_format.upper()}-реестре не найдена таблица с наименованием, серией и сроком годности')
