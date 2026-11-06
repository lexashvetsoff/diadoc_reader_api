import re
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(slots=True, frozen=True)
class ParsedProductName:
    raw_name: str
    name: str
    series: str | None = None
    expiration_date: date | str | None = None


class ProductNameParser:
    """Извлечение серии/срока действия, встроенных в название продукта."""
    _NESTED_AUX_MONTH_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?P<series>[^()]+?)\s*
            \(
                \s*(?P<aux>[^,()]+?)\s*,\s*
                (?P<month>\d{2}\.\d{4})
                \s*
            \)
            \s*
            (?:
                \(\s*2\s*D\s*[-–—]?\s*код\s*\)
            )?
            \s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )
    
    _PAREN_SERIES_FROM_TO_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?P<series>[^()]+?)\s+
            от\s+(?P<from_date>\d{2}\.\d{2}\.(?:\d{2}|\d{4}))\s+
            до\s+(?P<date>\d{2}\.\d{2}\.(?:\d{2}|\d{4}))
            \s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _NESTED_TECH_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?P<series>[^()]+?)\s*
            \(\s*(?P<date>\d{2}\.\d{2}\.\d{4})\s*\)
            \s*
            (?:
                \(\s*2\s*D\s*[-–—]?\s*код\s*\)
            )?
            \s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _TRAILING_SERIES_DATE_PATTERN = re.compile(
        r"""
        ,\s*
        (?P<series>[A-Za-zА-Яа-я0-9()\-]{3,32})
        \s*,\s*
        (?P<date>\d{2}\.\d{2}\.(?:\d{2}|\d{4}))
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _TRAILING_COMMA_SERIES_PATTERN = re.compile(
        r"""
        ,\s*
        (?P<series>[A-Za-zА-Яа-я0-9()\-]{5,16})
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _PAREN_SERIES_TO_DATE_AUX_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?P<series>[A-Za-zА-Яа-я0-9()\-]+)
            \s+до\s+
            (?P<date>\d{2}\.\d{2}\.\d{4})
            \s*
            \(\s*(?P<aux>[A-Za-zА-Яа-я0-9\-]+)\s*\)
            \s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _PAREN_SERIES_MONTH_YEAR_PATTERN = re.compile(
        r"""
        \s*\(
            \s*(?P<series>[A-Za-zА-Яа-я0-9-]{3,32})
            \s+(?:годен\s+до|срок\s+годности)\s*
            (?P<month>0[1-9]|1[0-2])\s*[/.]\s*(?P<year>\d{4})
            \s*
        \)\s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _PAREN_SERIES_TO_DATE_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?P<series>[A-Za-zА-Яа-я0-9()\-]+)
            \s+до\s+
            (?P<date>\d{2}\.\d{2}\.\d{4})
            \s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _PAREN_SERIES_ONLY_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?:серия|сер\.?)\s+
            (?P<series>[A-Za-zА-Яа-я0-9()\-]+)
            \s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _SERIES_ONLY_PATTERN = re.compile(
        r"""
        \s+
        (?:серия|сер\.?)\s+
        (?P<series>[A-Za-zА-Яа-я0-9()\-]+)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _PAREN_SERIES_DATE_COUNTRY_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?P<series>[^,()]+?)\s*,
            \s*(?P<date>\d{2}\.\d{2}\.\d{4})\s*,
            \s*(?P<country>[^()]+?)\s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _PAREN_PATTERN = re.compile(
        r"\(\s*(?P<series>[^(),]+?)\s*,\s*"
        r"(?P<date>\d{2}\.\d{2}\.(?:\d{4}|\d{2}))\s*\)\s*$",
        re.IGNORECASE,
    )

    _PAREN_SERIES_SEMICOLON_TO_DATE_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?:серия|сер\.?)\s+
            (?P<series>[A-Za-zА-Яа-я0-9()\-/]+)
            \s*;\s*
            до\s+
            (?P<date>\d{2}\.\d{2}\.\d{4})
            \s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _PAREN_TEXT_NO_SEPARATOR_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?:серия|сер\.?)\s+
            (?P<series>[A-Za-zА-Яа-я0-9()\-]+)
            \s+
            (?:
                годен\s+до
                |
                срок\s+годности
            )
            \s*
            (?P<date>\d{2}\.\d{2}\.\d{4})
            \s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _TEXT_SERIES_COMMA_TO_DATE_PATTERN = re.compile(
        r"""
        \s*,?\s+
        (?:серия|сер\.?)\s+
        (?P<series>[A-Za-zА-Яа-я0-9()\-]+)
        \s*[,;]\s*
        до\s+
        (?P<date>\d{2}\.\d{2}\.(?:\d{2}|\d{4}))
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _PAREN_TEXT_PATTERN = re.compile(
        r"""
        \s*
        \(
            \s*(?:серия|сер\.?)\s+
            (?P<series>.+?)
            \s*[,;]\s*
            (?:
                годен\s+до
                |
                срок\s+годности
            )
            \s*
            (?P<date>\d{2}\.\d{2}\.\d{4})
            \s*
        \)
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    _TEXT_PATTERN = re.compile(
        r"""
        \s*,?\s*
        (?:серия|сер\.?)\s*
        (?P<series>[^,;]+?)
        \s*[,;]\s*
        (?:
            годен\s+до
            |
            срок\s+годности
        )
        \s*
        (?P<date>\d{2}\.\d{2}\.\d{4})
        \s*$
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    def parse(self, raw_name: str | None) -> ParsedProductName:
        raw = (raw_name or "").strip()

        match = self._TEXT_SERIES_COMMA_TO_DATE_PATTERN.search(raw)
        if match:
            raw_expiry = match.group("date")
            date_format = "%d.%m.%Y" if len(raw_expiry.rsplit(".", 1)[-1]) == 4 else "%d.%m.%y"
            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=match.group("series"),
                expiration_date=datetime.strptime(raw_expiry, date_format).date(),
            )

        # Форма "месяц-год" обрабатывается отдельно, поскольку преобразование
        # "03.2029" в дату привело бы к созданию неизвестного дня.
        match = self._PAREN_SERIES_FROM_TO_PATTERN.search(raw)
        if match:
            raw_expiry = match.group("date")
            date_format = "%d.%m.%Y" if len(raw_expiry.rsplit(".", 1)[-1]) == 4 else "%d.%m.%y"
            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=re.sub(r"(?i)^(?:серия|сер\.?)\s*[:№-]?\s*", "", match.group("series")).strip(" ,;") or None,
                expiration_date=datetime.strptime(raw_expiry, date_format).date(),
            )

        # Форма месяца-года обрабатывается отдельно, поскольку преобразование 
        # "03.2029" для даты сфабриковал бы неизвестный день.
        match = self._NESTED_AUX_MONTH_PATTERN.search(raw)
        if match:
            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=match.group("series").strip(" ,;") or None,
                expiration_date=match.group("month"),
            )

        match = self._PAREN_SERIES_MONTH_YEAR_PATTERN.search(raw)
        if match:
            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=match.group("series"),
                expiration_date=f"{match.group('month')}.{match.group('year')}",
            )

        match = self._PAREN_SERIES_TO_DATE_AUX_PATTERN.search(raw)
        if match:
            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=match.group("series").strip(" ,;") or None,
                expiration_date=datetime.strptime(
                    match.group("date"), "%d.%m.%Y"
                ).date(),
            )

        match = self._PAREN_SERIES_TO_DATE_PATTERN.search(raw)
        if match:
            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=match.group("series").strip(" ,;") or None,
                expiration_date=datetime.strptime(
                    match.group("date"), "%d.%m.%Y"
                ).date(),
            )

        match = self._PAREN_SERIES_ONLY_PATTERN.search(raw)
        if match:
            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=match.group("series").strip(" ,;") or None,
                expiration_date=None,
            )

        match = self._SERIES_ONLY_PATTERN.search(raw)
        if match:
            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=match.group("series").strip(" ,;") or None,
                expiration_date=None,
            )

        match = self._TRAILING_SERIES_DATE_PATTERN.search(raw)
        if match:
            raw_expiry = match.group("date")
            date_format = "%d.%m.%Y" if len(raw_expiry.rsplit(".", 1)[-1]) == 4 else "%d.%m.%y"
            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=match.group("series").strip(" ,;") or None,
                expiration_date=datetime.strptime(raw_expiry, date_format).date(),
            )

        match = self._TRAILING_COMMA_SERIES_PATTERN.search(raw)
        if match:
            series = match.group("series").strip(" ,;")
            if any(ch.isdigit() for ch in series):
                return ParsedProductName(
                    raw_name=raw,
                    name=raw[:match.start()].rstrip(" ,;"),
                    series=series or None,
                    expiration_date=None,
                )

        for pattern in (
            self._NESTED_TECH_PATTERN,
            self._PAREN_SERIES_DATE_COUNTRY_PATTERN,
            self._PAREN_SERIES_SEMICOLON_TO_DATE_PATTERN,
            self._PAREN_TEXT_NO_SEPARATOR_PATTERN,
            self._PAREN_TEXT_PATTERN,
            self._TEXT_PATTERN,
            self._PAREN_PATTERN,
        ):
            match = pattern.search(raw)
            if not match:
                continue

            return ParsedProductName(
                raw_name=raw,
                name=raw[:match.start()].rstrip(" ,;"),
                series=match.group("series").strip(" ,;") or None,
                expiration_date=datetime.strptime(
                    match.group("date"),
                    "%d.%m.%Y" if len(match.group("date").rsplit(".", 1)[-1]) == 4 else "%d.%m.%y",
                ).date(),
            )

        return ParsedProductName(
            raw_name=raw,
            name=raw,
            series=None,
            expiration_date=None,
        )
