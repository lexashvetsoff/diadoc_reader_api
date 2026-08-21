from pathlib import Path

from app.services.unified_parser.models import MergeResult
from app.services.unified_parser.xml_writer import FinalXMLWriter
from app.services.unified_parser.merger import UnifierDocumentBuilder


class DeliveryDocumentAssembler:
    """Parse + match + merge + XML generation"""

    def __init__(self):
        self.builder = UnifierDocumentBuilder()
        self.writer = FinalXMLWriter()

    def merge(self, upd, registry=None, protocol=None) -> MergeResult:
        return self.builder.build(upd, registry=registry, protocol=protocol)

    def build_xml(self, upd, registry=None, protocol=None) -> bytes:
        result = self.merge(upd, registry=registry, protocol=protocol)
        return self.writer.to_bytes(result)

    def write_xml(self, target: str | Path, upd, registry=None, protocol=None) -> MergeResult:
        result = self.merge(upd, registry=registry, protocol=protocol)
        self.writer.write(result, target)
        return result
