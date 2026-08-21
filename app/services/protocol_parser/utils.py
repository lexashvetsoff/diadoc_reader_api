import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def clean_text(value) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


def decimal_or_none(value) -> Decimal | None:
    value = clean_text(value)
    if not value or value in {"-", "—", "–"}:
        return None

    value = value.replace("%", "").replace("\xa0", " ").strip()
    value = value.replace(" ", "").replace(",", ".")

    if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
        return None

    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def date_or_none(value) -> date | None:
    value = clean_text(value)
    if not value or value in {"-", "—", "–"}:
        return None

    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def normalize_header(value) -> str:
    value = clean_text(value) or ""
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[<>\d]+", " ", value)
    value = re.sub(r"[^a-zа-я%]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()
