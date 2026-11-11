from app.services.xls_reader import read_sheets
from app.services.registry_parser.xlsx_parser import XLSXRegistryParser


class XLSRegistryParser(XLSXRegistryParser):
    def parse(self, source):
        return self.parse_sheets(read_sheets(source), source_format='xls')
