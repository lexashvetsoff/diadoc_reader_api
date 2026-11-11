from pathlib import Path
import xml.etree.ElementTree as ET

from app.services.protocol_parser.models import ProtocolDocument, ProtocolProduct
from app.services.protocol_parser.utils import date_or_none, decimal_or_none, clean_text
from app.services.protocol_parser.exceptions import ProtocolParserError, UnsupportedProtocolFormatError
from app.services.batch_expiry import split_batch_expiry


class XMLProtocolParser:
    """Parser for XML documents with Формат="ПротЦен"."""

    def parse(self, source: str | Path | bytes) -> ProtocolDocument:
        root = self._load(source)

        if (root.get('Формат') or '').strip() != 'ПротЦен':
            raise UnsupportedProtocolFormatError(
                f"XML Формат={root.get('Формат')!r}, ожидался 'ПротЦен'"
            )

        document = root.find('.//Документ')
        if document is None:
            raise ProtocolParserError('В XML не найден элемент Документ')

        supplier = document.find('./Стороны/Поставщик')
        buyer = document.find('./Стороны/Покупатель')

        basis = None
        for candidate in document.findall('./СписокОснование/Основание'):
            if (candidate.get('Тип') or '').upper() == 'УПД':
                basis = candidate
                break
        if basis is None:
            basis = document.find('./СписокОснование/Основание')

        products = [
            self._parse_row(row)
            for row in document.findall('./СписокСтрТабл/СтрТабл')
        ]

        return ProtocolDocument(
            document_number=clean_text(document.get('Номер')),
            document_date=date_or_none(document.get('Дата')),
            supplier=clean_text(supplier.get('Название')) if supplier is not None else None,
            buyer=clean_text(buyer.get('Название')) if buyer is not None else None,
            basis_number=clean_text(basis.get('Номер')) if basis is not None else None,
            basis_date=date_or_none(basis.get('Дата')) if basis is not None else None,
            source_format='xml',
            products=products
        )

    def _parse_row(self, row: ET.Element) -> ProtocolProduct:
        wholesale_price = self._final_wholesale_price(row)
        series, expiration = split_batch_expiry(clean_text(row.get("Серия")))

        raw_line = clean_text(row.get('ПорНомер'))
        line_number = int(raw_line) if raw_line and raw_line.isdigit() else None

        return ProtocolProduct(
            line_number=line_number,
            name=clean_text(row.get('Название')),
            product_code=clean_text(row.get('Код')),
            gtin=clean_text(row.get("GTIN")),
            series=series,
            expiration_date=expiration,
            manufacturer=clean_text(row.get('Производитель')),
            manufacturer_actual_price_without_vat=decimal_or_none(row.get('ЦенаПроизвФактБезНДС')),
            manufacturer_regulated_price=decimal_or_none(row.get('ЦенаПроизвРег')),
            quantity=decimal_or_none(row.get('Кол_во')),
            wholesale_price=wholesale_price,
            manufacturer_sale_date=date_or_none(row.get('Дата'))
        )

    @staticmethod
    def _final_wholesale_price(row: ET.Element):
        candidates = []
        for node in row.findall('./СписокЦенаОпт/ЦенаОпт'):
            price = decimal_or_none(node.get('Цена'))
            if price is None:
                continue
            raw_order = clean_text(node.get('ПорНомер'))
            order = int(raw_order) if raw_order and raw_order.isdigit() else 0
            candidates.append((order, price))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0])
        return candidates[-1][1]

    @staticmethod
    def _load(source):
        try:
            if isinstance(source, bytes):
                return ET.fromstring(source)
            return ET.parse(Path(source)).getroot()
        except (ET.ParseError, OSError, UnicodeError) as e:
            raise ProtocolParserError(f'Не удалось прочитать XML: {e}') from e
