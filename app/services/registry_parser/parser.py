from enum import Enum
from pathlib import Path

from app.services.registry_parser.models import RegistryDocument
from app.services.registry_parser.pdf_parser import PDFRegistryParser
from app.services.registry_parser.xml_parser import XMLRegistryParser
from app.services.registry_parser.exceptions import RegistryParserError


class RegistrySourceFormat(str, Enum):
    PDF = 'pdf'
    XML = 'xml'


class RegistryParser:
    """Единый фасад для документов реестра PDF и XML. 

    Example::

        parser = RegistryParser()
        document = parser.parse('registry.pdf')
        document = parser.parse('registry.xml')

    Возвращаемая модель всегда — RegistryDocument. 
    При обнаружении формата используется содержимое файла, а не только расширение имени файла.
    """

    def __init__(self, *, allow_pdf_fallback: bool = True) -> None:
        self.pdf_parser = PDFRegistryParser(allow_fallback=allow_pdf_fallback)
        self.xml_parser = XMLRegistryParser()

    def parse(self, source: str | Path | bytes) -> RegistryDocument:
        format_ = self.detect_format(source)
        if format_ == RegistrySourceFormat.PDF:
            return self.pdf_parser.parse(source)
        if format_ == RegistrySourceFormat.XML:
            return self.xml_parser.parse(source)
        raise RegistryParserError('Неподдерживаемый формат реестра')

    @staticmethod
    def detect_format(source: str | Path | bytes) -> RegistrySourceFormat:
        if isinstance(source, bytes):
            head = source[:1024]
        else:
            path = Path(source)
            try:
                with path.open('rb') as file:
                    head = file.read(1024)
            except OSError as e:
                raise RegistryParserError(f'Не удалось открыть файл: {e}') from e

        stripped = head.lstrip(b'\xef\xbb\xbf\x00\t\r\n ')
        if stripped.startswith(b'%PDF-'):
            return RegistrySourceFormat.PDF
        if stripped.startswith(b'<?xml') or stripped.startswith(b'<'):
            return RegistrySourceFormat.XML

        raise RegistryParserError('Не удалось определить формат: ожидается PDF или XML')
