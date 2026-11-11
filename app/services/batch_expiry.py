import re
from datetime import datetime


_BATCH_EXPIRY = re.compile(
    r"^\s*(?P<series>[\w/.-]+)\s*[,;]\s*(?P<expiry>\d{2}\.\d{2}\.(?:\d{4}|\d{2}))\s*$",
    re.UNICODE,
)


def split_batch_expiry(value: str | None) -> tuple[str | None, str | None]:
    if not value or not value.strip():
        return None, None
    cleaned = value.strip()
    match = _BATCH_EXPIRY.fullmatch(cleaned)
    if match is None:
        return cleaned, None
    expiry = match.group('expiry')
    fmt = '%d.%m.%Y' if len(expiry.rsplit('.', 1)[-1]) == 4 else '%d.%m.%y'
    try:
        parsed = datetime.strptime(expiry, fmt).date()
    except ValueError:
        # Never discard an unrecognized value or invent a valid expiration.
        return cleaned, None
    return match.group('series'), parsed.strftime('%d.%m.%Y')
