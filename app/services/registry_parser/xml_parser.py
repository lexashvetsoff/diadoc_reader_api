from pathlib import Path
from typing import Iterable
from decimal import Decimal, InvalidOperation
from defusedxml import ElementTree as ET

from app.services.registry_parser.models import RegistryDocument, RegistryProduct
from app.services.registry_parser.exceptions import RegistryParserError, UnsupportedDocumentTypeError


def _local_name(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


def _children(node, name: str):
    return [child for child in list(node) if _local_name(child.tag) == name]


def _first_child(node, name: str):
    for child in list(node):
        if _local_name(child.tag) == name:
            return child
    return None


class XMLRegistryParser:
    """Анализ XML-реестров сертификатов/серий и преобразование их в общую модель RegistryDocument.

    Поддерживаемые семейства форматов, указанные в предоставленных файлах:
        * ЭДОРеестрСертификатов
        * ЭДОРеестрСерий

    Строки продуктов намеренно считываются только из ``ТаблДок``. Некоторые документы
    также содержат ``ТаблДетал`` с дублирующимися коммерческими строками, которые не содержат
    данных о партии/сроке действия/производителе.
    """

    SUPPORTED_FORMATS = {
        'ЭДОРеестрСертификатов',
        'ЭДОРеестрСерий',
    }

    def parse(self, source: str | Path | bytes) -> RegistryDocument:
        root = self._load_root(source)
        document = self._find_document(root)

        format_name = (root.get('Формат') or '').strip()
        document_name = (document.get('Название') or '').strip()

        if format_name not in self.SUPPORTED_FORMATS and 'реестр' not in document_name.lower():
            raise UnsupportedDocumentTypeError(
                f'XML имеет формат {format_name!r} и не распознан как registry'
            )

        rows = list(self._registry_rows(document))
        if not rows:
            raise RegistryParserError('В XML-реестре не найдено строк ТаблДок/СтрТабл')

        products = [self._parse_row(row) for row in rows]

        return RegistryDocument(
            source_layout=f"xml:{format_name or 'registry'}:ТаблДок",
            layout_confidence=1.0,
            layout_source='xml',
            supplier=self._party_name(document, 'Поставщик'),
            buyer=(self._party_name(document, 'Покупатель') or self._party_name(document, 'Грузополучатель')),
            invoice_number=self._clean(document.get('Номер')),
            invoice_date=self._clean(document.get('Дата')),
            products=products
        )

    def _load_root(self, source: str | Path | bytes):
        try:
            if isinstance(source, bytes):
                return ET.fromstring(source)
            return ET.parse(str(Path(source))).getroot()
        except Exception as e:
            raise RegistryParserError(f"Не удалось прочитать XML-реестр: {e}") from e

    def _parse_row(self, row) -> RegistryProduct:
        params = self._parameters(row)
        manufacturer = self._manufacturer(row, params)

        product_code = self._clean(row.get('НомНомер'))
        if not product_code:
            identifier = self._clean(row.get('Идентификатор'))
            name = self._clean(row.get('Название'))
            # В некоторых файлах ЭДОРеестрСерий полное название продукта помещается в Идентификатор. 
            # Это не код продукта.
            if identifier and identifier != name:
                product_code = identifier

        return RegistryProduct(
            line_number=self._int(row.get('ПорНомер')),
            name=self._clean(row.get('Название')),
            product_code=product_code,
            series=self._clean(row.get('Серия')),
            manufacturer=manufacturer,
            quantity=self._decimal(row.get('Кол_во')),
            expiration_date=self._clean(row.get('Срок'))
        )

    def _manufacturer(self, row, params: dict[str, str]) -> str | None:
        for child in list(row):
            if _local_name(child.tag) == 'Производитель':
                value = self._clean(child.get('Название'))
                if value:
                    return value
        return self._clean(params.get('производитель'))

    def _party_name(self, document, tag_name: str) -> str | None:
        party = _first_child(document, tag_name)
        if party is None:
            return None

        direct = self._clean(party.get('Название'))
        if direct:
            return direct

        for node in party.iter():
            if _local_name(node.tag) in {'СвЮЛ', 'СвФЛ'}:
                value = self._clean(node.get('Название'))
                if value:
                    return value
        return None

    @staticmethod
    def _find_document(root):
        if _local_name(root.tag) == 'Документ':
            return root
        for node in root.iter():
            if _local_name(node.tag) == 'Документ':
                return node
        raise RegistryParserError("Элемент 'Документ' не найден в XML-реестре")

    @staticmethod
    def _registry_rows(document) -> Iterable:
        # Не читать все СтрТабл рекурсивно: ТаблобДетал может дублировать ТаблДок.
        for child in list(document):
            if _local_name(child.tag) != 'ТаблДок':
                continue
            for row in list(child):
                if _local_name(row.tag) == 'СтрТабл':
                    yield row

    @staticmethod
    def _parameters(row) -> dict[str, str]:
        result: dict[str, str] = {}
        for child in list(row):
            if _local_name(child.tag) != 'Параметр':
                continue
            name = (child.get('Имя') or '').strip()
            if name:
                result[name.casefold()] = (child.get('Значение') or '').strip()
        return result

    @staticmethod
    def _clean(value) -> str | None:
        if value is None:
            return None
        value = ' '.join(str(value).split()).strip()
        return value or None

    @staticmethod
    def _decimal(value) -> Decimal | None:
        if value is None or not str(value).strip():
            return None
        try:
            return Decimal(str(value).strip().replace(',', '.'))
        except InvalidOperation as e:
            raise RegistryParserError(f'Некорректное количество в XML: {value!r}') from e

    @staticmethod
    def _int(value) -> int | None:
        if value is None or not str(value).strip():
            return None
        try:
            return int(str(value).strip())
        except ValueError as e:
            raise RegistryParserError(f'Некорректный ПорНомер в XML: {value!r}') from e
