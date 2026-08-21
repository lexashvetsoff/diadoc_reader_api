class RegistryParserError(Exception):
    """Base registry parser error."""


class UnsupportedRegistryLayoutError(RegistryParserError):
    """Raised when the table layout cannot be recognized safely."""


class UnsupportedDocumentTypeError(RegistryParserError):
    """The supplied PDF is not a certificate registry."""

