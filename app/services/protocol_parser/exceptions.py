class ProtocolParserError(Exception):
    """Base protocol parser error."""


class UnsupportedProtocolFormatError(ProtocolParserError):
    """Input is neither a supported protocol XML nor a supported protocol PDF."""


class UnsupportedProtocolLayoutError(ProtocolParserError):
    """PDF is a protocol, but its table layout could not be normalized."""
