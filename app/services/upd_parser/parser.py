from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET

from app.services.upd_parser.models import UPDDocument, UPDProduct
from app.services.upd_parser.name_parser import ProductNameParser


class UPDParserError(Exception): pass
class UPDXMLParseError(UPDParserError): pass
class UPDStructureError(UPDParserError): pass


class UPDParser:
    def __init__(self):
        self.name_parser = ProductNameParser()

    def parse(self, source: str | Path | bytes) -> UPDDocument:
        root = self._load(source)
        doc = root.find('.//Документ')
        if doc is None:
            raise UPDStructureError("Элемент 'Документ' не найден")
        
        invoice = doc.find('./СвСчФакт')
        table = doc.find('./ТаблСчФакт')
        if table is None:
            raise UPDStructureError("Элемент 'ТаблСчФакт' не найден")

        return UPDDocument(
            document_number=invoice.get('НомерДок') if invoice is not None else None,
            document_data=self._date(invoice.get('ДатаДок') if invoice is not None else None),
            products=tuple(self._product(x) for x in table.findall('./СведТов'))
        )

    def _product(self, node: ET.Element) -> UPDProduct:
        raw_line = node.get('НомСтр')
        if not raw_line:
            raise UPDStructureError('У строки товара отсутствует НомСтр')
        try:
            line = int(raw_line)
        except ValueError as e:
            raise UPDStructureError(f'Некорректный НомСтр: {raw_line!r}') from e

        parsed_name = self.name_parser.parse(node.get('НаимТов'))
        info_fields = self._info_fields(node)
        extra = node.find('./ДопСведТов')
        vat_node = node.find('./СумНал/СумНал')
        vat_text = vat_node.text.strip() if vat_node is not None and vat_node.text else None

        return UPDProduct(
            line_number=line,
            raw_line_number=raw_line,
            raw_name=parsed_name.raw_name,
            name=parsed_name.name,
            product_code=extra.get('КодТов') if extra is not None else None,
            unit=node.get('НаимЕдИзм'),
            okei=node.get('ОКЕИ_Тов'),
            gtin=info_fields.get('GTIN') or self._gtin(node),
            quantity=self._decimal(node.get('КолТов')),
            price=self._decimal(node.get('ЦенаТов')),
            amount_without_vat=self._decimal(node.get('СтТовБезНДС')),
            vat_rate=node.get('НалСт'),
            vat_amount=self._decimal(vat_text),
            amount_with_vat=self._decimal(node.get('СтТовУчНал')),
            series=(
                info_fields.get('НОМЕРСЕРИИ')
                or info_fields.get('СЕРИЯ')
                or parsed_name.series
            ),
            expiration_date=self._expiration(
                info_fields.get('СРОКГОДНОСТИ'),
                parsed_name.expiration_date,
            ),
        )

    @staticmethod
    def _load(source: str | Path | bytes) -> ET.Element:
        try:
            if isinstance(source, bytes):
                return ET.fromstring(source)
            return ET.parse(Path(source)).getroot()
        except (ET.ParseError, OSError, UnicodeError) as e:
            raise UPDXMLParseError(f'Не удалось прочитать XML: {e}') from e

    @staticmethod
    def _info_fields(node: ET.Element) -> dict[str, str]:
        """Считывание структурированных дополнительных полей, прикрепленных к строке товара UPD.

        Поставщики могут хранить серию, срок годности и GTIN в поле «ИнфПолФХЖ2», а не в поле «НаимТов». 
        Структурированные поля более авторитетны, чем текстовое извлечение, поэтому при слиянии они используются в первую очередь.
        """
        result: dict[str, str] = {}
        for info in node.findall('./ИнфПолФХЖ2'):
            key = (info.get('Идентиф') or '').strip().upper().replace(' ', '')
            value = (info.get('Значен') or '').strip()
            if key and value:
                result[key] = value
        return result

    @classmethod
    def _expiration(cls, structured: str | None, fallback):
        if structured:
            # Предпочтительнее указывать полную дату, если она доступна; 
            # месяц-год сохраняется дословно, а не придумывается неизвестный день.
            try:
                return datetime.strptime(structured, '%d.%m.%Y').date()
            except ValueError:
                if __import__('re').fullmatch(r'\d{2}\.\d{4}', structured):
                    return structured
                raise UPDStructureError(
                    f'Некорректный СрокГодности: {structured!r}'
                )
        return fallback

    @staticmethod
    def _gtin(node: ET.Element) -> str | None:
        for info in node.findall('./ИнфПолФХЖ2'):
            if (info.get('Идентиф') or '').strip().upper() == 'GTIN':
                value = info.get('Значен')
                return value.strip() if value else None
        return None

    @staticmethod
    def _decimal(value: str | None) -> Decimal | None:
        if value is None or not value.strip(): return None
        try:
            return Decimal(value.strip().replace(',', '.'))
        except InvalidOperation as e:
            raise UPDStructureError(f'Некорректное числовое значение: {value!r}') from e

    @staticmethod
    def _date(value: str | None):
        if value is None or not value.strip(): return None
        try:
            return datetime.strptime(value.strip(), '%d.%m.%Y').date()
        except ValueError as e:
            raise UPDStructureError(f'Некорректная дата: {value!r}') from e
