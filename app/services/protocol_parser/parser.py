from pathlib import Path

from app.services.protocol_parser.models import ProtocolDocument
from app.services.protocol_parser.pdf_parser import PDFProtocolParser
from app.services.protocol_parser.xml_parser import XMLProtocolParser
from app.services.protocol_parser.xls_parser import XLSProtocolParser
from app.services.protocol_parser.xlsx_parser import XLSXProtocolParser
from app.services.protocol_parser.exceptions import UnsupportedProtocolFormatError


class ProtocolParser:
    """Единый интерфейс для протоколов цен XML и PDF."""

    PDF_MAGIC = b"%PDF"
    XML_PREFIXES = (b"<?xml", b"<")

    def __init__(self):
        self.pdf_parser = PDFProtocolParser()
        self.xml_parser = XMLProtocolParser()
        self.xlsx_parser = XLSXProtocolParser()
        self.xls_parser = XLSProtocolParser()

    def parse(self, source: str | Path | bytes) -> ProtocolDocument:
        raw_prefix = self._prefix(source)

        if raw_prefix.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            return self.xls_parser.parse(source)

        if raw_prefix.startswith(b"PK\x03\x04"):
            return self.xlsx_parser.parse(source)

        if raw_prefix.startswith(self.PDF_MAGIC):
            return self.pdf_parser.parse(source)

        stripped = raw_prefix.lstrip()
        if stripped.startswith(self.XML_PREFIXES):
            return self.xml_parser.parse(source)

        raise UnsupportedProtocolFormatError(
            'Не удалось определить формат протокола: ожидается PDF, XML, XLSX или XLS'
        )

    @staticmethod
    def _prefix(source) -> bytes:
        if isinstance(source, bytes):
            return source[:256]

        with Path(source).open('rb') as fh:
            return fh.read(256)


def parse_protocol(source: str | Path | bytes) -> ProtocolDocument:
    return ProtocolParser().parse(source)
