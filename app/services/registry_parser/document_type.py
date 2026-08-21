import re
from enum import Enum


class DocumentType(str, Enum):
    REGISTRY = "registry"
    SPECIFICATION = "specification"
    PRICE_PROTOCOL = "price_protocol"
    UNKNOWN = "unknown"


class DocumentTypeDetector:
    """Легковесный семантический детектор, основанный на словаре заголовка документа."""

    REGISTRY_MARKERS = (
        "реестр сертификатов",
        "реестр деклараций",
        "сертификаты номенклатуры",
        "реестр id-номеров",
        "документ качества",
    )

    SPECIFICATION_MARKERS = (
        "спецификация №",
        "спецификация no",
        "форма д-1-спец",
    )

    PRICE_PROTOCOL_MARKERS = (
        "протокол согласования цен",
        "протокол согласования отпускных цен",
        "протокол цен",
    )

    def detect(self, text: str) -> DocumentType:
        norm = self._norm(text)

        if any(marker in norm for marker in self.SPECIFICATION_MARKERS):
            return DocumentType.SPECIFICATION

        if any(marker in norm for marker in self.PRICE_PROTOCOL_MARKERS):
            return DocumentType.PRICE_PROTOCOL

        if any(marker in norm for marker in self.REGISTRY_MARKERS):
            return DocumentType.REGISTRY

        # Структурный резервный вариант для документов, аналогичных документам реестра.
        registry_columns = (
            "серия",
            "производитель",
            "срок годности",
        )
        if sum(1 for x in registry_columns if x in norm) >= 2 and (
            "сертификат" in norm or "декларац" in norm
        ):
            return DocumentType.REGISTRY

        return DocumentType.UNKNOWN

    @staticmethod
    def _norm(text: str) -> str:
        text = (text or None).lower().replace("ё", "е")
        text = re.sub(r"\s+", " ", text)
        return text.strip()
