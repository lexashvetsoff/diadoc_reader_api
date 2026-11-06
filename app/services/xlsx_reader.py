import re
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET


N = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def read_sheets(source):
    data = BytesIO(source) if isinstance(source, bytes) else Path(source)
    with ZipFile(data) as z:
        names = {name.casefold(): name for name in z.namelist()}
        key = names.get('xl/sharedstrings.xml')
        shared = []
        if key:
            root = ET.fromstring(z.read(key))
            shared = [''.join(t.text or '' for t in si.iter(N+'t')) for si in root.findall(N+'si')]
        sheets = []
        for path in sorted((n for n in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',n, re.I)), key=lambda x:int(re.search(r'sheet(\d+)',x,re.I).group(1))):
            root=ET.fromstring(z.read(path))
            rows=[]
            for row in root.findall('.//'+N+'sheetData/'+N+'row'):
                vals={}
                for cell in row.findall(N+'c'):
                    ref=cell.get('r','')
                    column=re.match(r'[A-Z]+',ref)
                    if not column:continue
                    val=cell.find(N+'v'); inline=cell.find(N+'is')
                    value=val.text if val is not None else ''.join(t.text or '' for t in inline.iter(N+'t')) if inline is not None else None
                    if value is None:continue
                    if cell.get('t')=='s':
                        value=shared[int(value)]
                    vals[column.group()]=str(value).strip()
                if vals:rows.append((int(row.get('r')),vals))
            sheets.append(rows)
        return sheets


def decimal(value):
    from decimal import Decimal, InvalidOperation
    if value is None:return None
    s=str(value).replace('\u00a0','').replace(' ','').replace(',','.').strip()
    if not s or s in {'----','-','—'}:return None
    try:return Decimal(s)
    except InvalidOperation:return None


def date_value(value):
    from datetime import date, datetime, timedelta
    if not value:return None
    try:
        if re.fullmatch(r'\d+(?:\.\d+)?',str(value)):
            return date(1899,12,30)+timedelta(days=int(float(value)))
        return datetime.strptime(str(value).strip(),'%d.%m.%Y').date()
    except (ValueError,OverflowError):return None
