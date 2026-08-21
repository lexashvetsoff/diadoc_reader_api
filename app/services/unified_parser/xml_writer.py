from pathlib import Path
from datetime import date
from decimal import Decimal
import xml.etree.ElementTree as ET

from app.services.unified_parser.models import MergeResult, UnifiedDocument


FIELD_MAP = (
    ("НомерТовара", "line_number"),
    ("НаименованиеТовара", "name"),
    ("КодТов", "product_code"),
    ("НаимЕдИзм", "unit"),
    ("ОКЕИ_Тов", "okei"),
    ("Количество", "quantity"),
    ("Цена", "price"),
    ("СумБезНДС", "amount_without_vat"),
    ("СтавкаНДС", "vat_rate"),
    ("СумНДС", "vat_amount"),
    ("СумУчНДС", "amount_with_vat"),
    ("GTIN", "gtin"),
    ("Серия", "series"),
    ("Производитель", "manufacturer"),
    ("ЦенаПроизвФактБезНДС", "manufacturer_actual_price_without_vat"),
    ("ЦенаПроизвРег", "manufacturer_regulated_price"),
    ("ОптоваяЦена", "wholesale_price"),
    ("СрокГодности", "expiration_date"),
    ("ДатаРеализацииПроизв", "manufacturer_sale_date"),
)


class FinalXMLWriter:
    def to_bytes(self, document: UnifiedDocument | MergeResult) -> bytes:
        if isinstance(document, MergeResult):
            document = document.document

        root = ET.Element('ИтоговыйДокумент')
        if document.upd_number:
            root.set('НомерУПД', document.upd_number)
        if document.upd_date:
            root.set('ДатаУПД', self._value(document.upd_date))

        products_node = ET.SubElement(root, 'Товары')

        for product in document.products:
            row = ET.SubElement(products_node, 'Товар')
            for xml_name, attr_name in FIELD_MAP:
                node = ET.SubElement(row, xml_name)
                value = getattr(product, attr_name)
                if value is not None:
                    node.text = self._value(value)

        try:
            ET.indent(root, space="  ")
        except AttributeError:
            pass

        return ET.tostring(
            root,
            encoding='utf-8',
            xml_declaration=True,
            short_empty_elements=True
        )

    def write(self, document: UnifiedDocument | MergeResult, target: str | Path) -> Path:
        path = Path(target)
        path.write_bytes(self.to_bytes(document))
        return path

    @staticmethod
    def _value(value) -> str:
        if isinstance(value, date):
            return value.strftime('%d.%m.%Y')
        if isinstance(value, Decimal):
            return format(value, 'f')
        return str(value)
