from __future__ import annotations

import re
import fitz
from io import BytesIO
from pathlib import Path
from datetime import date
from collections import Counter

from app.services.protocol_parser.models import ProtocolDocument, ProtocolProduct
from app.services.protocol_parser.exceptions import ProtocolParserError, UnsupportedProtocolFormatError
from app.services.protocol_parser.utils import clean_text, decimal_or_none, date_or_none, normalize_header


_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


class PDFProtocolParser:
    """
    Универсальный парсер, работающий по принципу «сетка в начале» для текстовых PDF-файлов с ценовыми протоколами.

    Регуляторные формы анализируются по *логическим* нумерованным столбцам (1..20/22),
    центры столбцов которых считываются из строки легенды, напечатанной под заголовком.
    Поэтому физическое количество столбцов в PyMuPDF может отличаться в зависимости от поставщика/страницы.
    """

    def parse(self, source: str | Path | bytes) -> ProtocolDocument:
        doc = self._open(source)
        try:
            full_text = "\n".join(page.get_text("text") for page in doc)
            if not self._is_protocol(full_text):
                raise UnsupportedProtocolFormatError(
                    "PDF не похож на протокол согласования цен"
                )

            products = self._parse_tables(doc)
            meta = self._metadata(full_text)

            # В некоторых формах не присваивается отдельный номер/дата протокола, и
            # протокол идентифицируется только по счету-фактуре/накладной, к которой он относится.
            # Сохранение этой полезной идентификации без потери явно указанных базовых полей.
            if meta["document_number"] is None and meta["basis_number"] is not None:
                meta["document_number"] = meta["basis_number"]
            if meta["document_date"] is None and meta["basis_date"] is not None:
                meta["document_date"] = meta["basis_date"]

            return ProtocolDocument(
                document_number=meta["document_number"],
                document_date=meta["document_date"],
                supplier=meta["supplier"],
                buyer=meta["buyer"],
                basis_number=meta["basis_number"],
                basis_date=meta["basis_date"],
                source_format="pdf",
                products=products,
            )
        finally:
            doc.close()

    def _parse_tables(self, doc: fitz.Document) -> list[ProtocolProduct]:
        products: list[tuple[ProtocolProduct, bool]] = []
        synthetic_line = 0
        carried_centers: dict[int, float] | None = None
        carried_schema: dict[str, int | None] | None = None

        for page in doc:
            page_words = page.get_text("words", sort=True)
            page_centers = self._legend_centers(page_words)
            if page_centers and {2, 3, 4, 5, 6, 8}.issubset(page_centers):
                carried_centers = page_centers

            for table in page.find_tables().tables:
                rows = table.extract()
                if not rows:
                    continue

                if self._is_compact_table(rows, table.col_count):
                    parsed, synthetic_line = self._parse_compact(rows, synthetic_line)
                    products.extend((p, True) for p in parsed)
                    continue

                schema = self._detect_regulatory_schema(rows) or carried_schema
                if schema is None:
                    continue
                carried_schema = schema

                centers = page_centers or carried_centers
                boundaries = (
                    self._column_boundaries(centers, page.rect.width)
                    if centers else None
                )

                for row_index, table_row in enumerate(table.rows):
                    row = rows[row_index] if row_index < len(rows) else []
                    if not row:
                        continue

                    name = self._cell(row, schema.get("name"))
                    series = self._cell(row, schema.get("series"))
                    manufacturer = self._cell(row, schema.get("manufacturer"))
                    regulated_text = self._cell(row, schema.get("regulated"))
                    actual_text = self._cell(row, schema.get("actual_without_vat"))

                    # Дата часто искажается из-за "хвоста" фактической ячейки цены, 
                    # поэтому ищется в небольшом районе.
                    date_parts = [
                        self._cell(row, schema.get("date")),
                        self._cell(row, (schema.get("date") or 0) - 1),
                    ]
                    sale_date = None
                    for part in date_parts:
                        sale_date = self._date_from_text(part)
                        if sale_date is not None:
                            break

                    if not self._looks_like_physical_product(
                        name, series, manufacturer,
                        regulated_text, actual_text, sale_date,
                    ):
                        continue

                    y0, y1 = self._row_y_bounds(table_row)
                    logical_values = {}
                    if y0 is not None and centers and boundaries:
                        logical_values = self._logical_values(
                            page_words, y0, y1, centers, boundaries
                        )

                    explicit = schema.get("line_number") is not None
                    line_number = None
                    if explicit:
                        raw_num = self._cell(row, schema.get("line_number"))
                        if raw_num and re.fullmatch(r"\d{1,5}", raw_num):
                            line_number = int(raw_num)

                    if line_number is None:
                        synthetic_line += 1
                        line_number = synthetic_line
                    else:
                        synthetic_line = max(synthetic_line, line_number)

                    wholesale = self._first_decimal(
                        logical_values.get(16), logical_values.get(11)
                    )
                    if wholesale is None:
                        start = schema.get("wholesale_group_start")
                        if start is not None:
                            wholesale = self._first_decimal(
                                self._cell(row, start + 2)
                            )

                    normalized_name = self._strip_gtin_from_name(name)
                    normalized_series = self._normalize_series(series)
                    if not normalized_series:
                        normalized_name, normalized_series = self._series_from_name(normalized_name)

                    product = ProtocolProduct(
                        line_number=line_number,
                        name=normalized_name,
                        product_code=None,
                        series=normalized_series,
                        manufacturer=self._clean_pdf_text(manufacturer),
                        manufacturer_actual_price_without_vat=decimal_or_none(actual_text),
                        manufacturer_regulated_price=decimal_or_none(regulated_text),
                        quantity=None,
                        wholesale_price=wholesale,
                        manufacturer_sale_date=sale_date,
                    )
                    products.append((product, explicit))

        # Удаление дубликатов повторяющихся физических копий/страниц. Некоторые PDF-файлы содержат
        # один и тот же протокол дважды. Для синтетической нумерации номер строки не является частью
        # идентификатора и переназначается после удаления дубликатов.
        unique: list[tuple[ProtocolProduct, bool]] = []
        seen = set()
        for p, explicit in products:
            key = (
                p.name, p.series, p.manufacturer,
                p.manufacturer_actual_price_without_vat,
                p.manufacturer_regulated_price,
                p.wholesale_price,
                p.manufacturer_sale_date,
            )
            if explicit:
                key = (p.line_number,) + key
            if key in seen:
                continue
            seen.add(key)
            unique.append((p, explicit))

        next_synthetic = 0
        result = []
        for p, explicit in unique:
            if not explicit:
                next_synthetic += 1
                p = p.model_copy(update={"line_number": next_synthetic})
            result.append(p)
        return result

    def _detect_regulatory_schema(self, rows):
        """Сопоставление бизнес-поля с физическими столбцами таблицы на основе семантических ячеек заголовка."""
        for row in rows[:6]:
            normalized = [normalize_header(c) if c else "" for c in row]
            squashed = [x.replace(" ", "") for x in normalized]

            def has_name(x):
                return "торгов" in x and ("наимен" in x or "назван" in x)

            if not any(has_name(x) for x in squashed):
                continue
            if not any("серия" in x for x in squashed):
                continue
            if not any("производ" in x for x in squashed):
                continue

            def idx(pred):
                return next((i for i, x in enumerate(squashed) if pred(x)), None)

            name_i = idx(has_name)
            series_i = idx(lambda x: "серия" in x)
            manuf_i = idx(
                lambda x: "производ" in x and "цена" not in x and "дата" not in x
            )
            reg_i = idx(lambda x: "зарегистр" in x and "предель" in x)
            actual_i = idx(lambda x: "фактическ" in x and "производ" in x)
            date_i = idx(lambda x: "дата" in x and ("реализа" in x or "производ" in x))
            wholesale_i = idx(lambda x: "отпускнаяценаорганизацииоптовойторговли" in x)

            if None not in (name_i, series_i, manuf_i, reg_i, actual_i, date_i):
                line_i = None
                for i, raw in enumerate(row):
                    raw_clean = clean_text(raw) or ""
                    if "№" in raw_clean and (
                        "п/п" in raw_clean.lower() or raw_clean.strip() == "№"
                    ):
                        line_i = i
                        break
                return {
                    "line_number": line_i,
                    "name": name_i,
                    "series": series_i,
                    "manufacturer": manuf_i,
                    "regulated": reg_i,
                    "actual_without_vat": actual_i,
                    "date": date_i,
                    "wholesale_group_start": wholesale_i,
                }
        return None

    def _looks_like_physical_product(
        self, name, series, manufacturer, regulated, actual, sale_date
    ):
        # В допустимой строке протокола может быть опущена зарегистрированная цена, дата и даже
        # выделенная ячейка серии (более старые/разреженные формы, такие как ФлораКавказа).
        # Требуется стабильное семантическое ядро ​​плюс как минимум одно денежное значение.
        return bool(
            name and re.search(r"[A-Za-zА-Яа-яЁё]", name)
            and manufacturer and re.search(r"[A-Za-zА-Яа-яЁё]", manufacturer)
            and (decimal_or_none(regulated) is not None or decimal_or_none(actual) is not None)
        )

    @staticmethod
    def _cell(row, index):
        if index is None or index < 0 or index >= len(row):
            return None
        return clean_text(row[index])

    # ------------------------------------------------------------------
    # Logical numbered-column geometry
    # ------------------------------------------------------------------

    def _legend_centers(self, words) -> dict[int, float] | None:
        """Find the horizontal legend containing logical column numbers."""
        clusters: dict[int, list[tuple[int, float]]] = {}
        for w in words:
            n = self._legend_token(w[4])
            if n is None:
                continue
            y_key = round(((w[1] + w[3]) / 2) / 3) * 3
            clusters.setdefault(y_key, []).append((n, (w[0] + w[2]) / 2))

        best = None
        for items in clusters.values():
            mapping = {}
            for n, x in items:
                mapping.setdefault(n, x)
            nums = sorted(mapping)
            # Real legends contain a long run of standard column numbers.
            coverage = len([n for n in nums if 1 <= n <= 20])
            score = coverage + (5 if {2, 3, 4, 5, 6, 8}.issubset(mapping) else 0)
            if coverage >= 8 and (best is None or score > best[0]):
                best = (score, mapping)

        return best[1] if best else None

    def _logical_values(self, words, y0, y1, centers, boundaries):
        buckets: dict[int, list] = {n: [] for n in centers}
        for w in words:
            cy = (w[1] + w[3]) / 2
            if not (y0 - 0.8 <= cy <= y1 + 0.8):
                continue
            cx = (w[0] + w[2]) / 2
            for logical, (left, right) in boundaries.items():
                if left <= cx < right:
                    buckets[logical].append(w)
                    break

        return {
            n: self._join_words(ws) if ws else None
            for n, ws in buckets.items()
        }

    def _looks_like_regulatory_product(self, values):
        name = values.get(2)
        series = values.get(3)
        manufacturer = values.get(4)
        regulated = decimal_or_none(values.get(5))
        actual = decimal_or_none(values.get(6))
        date_value = self._date_from_text(values.get(8))
        return bool(
            name and re.search(r"[A-Za-zА-Яа-яЁё]", name)
            and series and re.search(r"[A-Za-zА-Яа-яЁё0-9]", series)
            and manufacturer and re.search(r"[A-Za-zА-Яа-яЁё]", manufacturer)
            and (regulated is not None or actual is not None)
            and date_value is not None
        )

    @staticmethod
    def _legend_token(text: str) -> int | None:
        text = (text or "").strip()
        m = re.fullmatch(r"<?(\d{1,2})>?", text)
        if not m:
            return None
        n = int(m.group(1))
        return n if 1 <= n <= 22 else None

    @staticmethod
    def _column_boundaries(centers: dict[int, float], page_width: float):
        ordered = sorted(centers.items(), key=lambda kv: kv[1])
        result = {}
        for i, (logical, x) in enumerate(ordered):
            left = 0.0 if i == 0 else (ordered[i - 1][1] + x) / 2
            right = page_width if i == len(ordered) - 1 else (x + ordered[i + 1][1]) / 2
            result[logical] = (left, right)
        return result

    @staticmethod
    def _row_y_bounds(table_row):
        # Игнорировать патологически перекрывающиеся ячейки: выбрать наиболее распространенный/кратчайший
        # физический интервал строки среди непустых ячеек.
        intervals = []
        for cell in table_row.cells:
            if cell is None:
                continue
            y0, y1 = float(cell[1]), float(cell[3])
            if y1 > y0:
                intervals.append((round(y0, 2), round(y1, 2)))
        if not intervals:
            return None, None
        counts = Counter(intervals)
        # Предпочтительнее повторяющийся интервал
        interval = max(counts, key=lambda p: (counts[p], -(p[1] - p[0])))
        return interval

    @staticmethod
    def _join_words(words):
        lines: list[tuple[float, list]] = []
        for w in sorted(words, key=lambda z: (z[1], z[0])):
            for i, (ly, ws) in enumerate(lines):
                if abs(ly - w[1]) <= 2.3:
                    ws.append(w)
                    break
            else:
                lines.append((w[1], [w]))
        text = " ".join(
            " ".join(q[4] for q in sorted(ws, key=lambda z: z[0]))
            for _, ws in lines
        )
        return PDFProtocolParser._clean_pdf_text(text)

    @staticmethod
    def _explicit_number_x(words, centers):
        first_x = min(centers.values())
        candidates = []
        for w in words:
            txt = (w[4] or "").lower()
            if txt in {"№", "п/п"} or "№" in txt:
                cx = (w[0] + w[2]) / 2
                if cx < first_x:
                    candidates.append(cx)
        return min(candidates) if candidates else None

    @staticmethod
    def _line_number_for_row(words, y0, y1, explicit_number_x):
        if explicit_number_x is None:
            return None
        for w in words:
            cy = (w[1] + w[3]) / 2
            cx = (w[0] + w[2]) / 2
            if y0 <= cy <= y1 and cx < explicit_number_x + 15:
                if re.fullmatch(r"\d{1,5}", w[4] or ""):
                    return int(w[4])
        return None

    # ------------------------------------------------------------------
    # Compact legacy form
    # ------------------------------------------------------------------

    def _parse_compact(self, rows, synthetic_line):
        result = []
        for row in rows:
            if len(row) < 12:
                continue
            num = clean_text(row[0])
            if not num or not re.fullmatch(r"\d{1,5}", num):
                continue
            name = clean_text(row[1])
            if not name or not re.search(r"[A-Za-zА-Яа-я]", name):
                continue
            result.append(ProtocolProduct(
                line_number=int(num),
                name=self._clean_pdf_text(name),
                product_code=None,
                series=self._normalize_series(clean_text(row[3])),
                manufacturer=self._clean_pdf_text(row[2]),
                manufacturer_actual_price_without_vat=decimal_or_none(row[8]),
                manufacturer_regulated_price=None,
                quantity=decimal_or_none(row[5]),
                wholesale_price=decimal_or_none(row[11]),
                manufacturer_sale_date=None,
            ))
            synthetic_line = max(synthetic_line, int(num))
        return result, synthetic_line

    @staticmethod
    def _is_compact_table(rows, col_count):
        if col_count != 15:
            return False
        sample = normalize_header(" ".join(
            clean_text(c) or "" for row in rows[:3] for c in row
        ))
        return (
            "наименование товара" in sample
            and "количество" in sample
            and "отпускн цена поставщика" in sample
        )

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_pdf_text(value):
        value = clean_text(value)
        if not value:
            return None
        value = value.replace("\u00ad", "")
        # Объединение слов, разорванных по переносу строки в PDF-файле; это также удаляет
        # искусственный пробел в именах типа "Ацикловир- АКОС".
        value = re.sub(
            r"(?<=[A-Za-zА-Яа-яЁё])-\s+(?=[A-Za-zА-Яа-яЁё])", "-", value
        )
        return re.sub(r"\s+", " ", value).strip()

    @classmethod
    def _strip_gtin_from_name(cls, name):
        name = cls._clean_pdf_text(name)
        if not name:
            return None
        return re.sub(r"[,;]?\s*\d{13,14}\s*$", "", name).strip(" ,;")

    @classmethod
    def _series_from_name(cls, name):
        """Извлекает явно помеченную партию из ячейки с торговым наименованием.
        Примеры: ``... серия 120525 (флакон-ПЭТ)``. Метка удаляется
        из нормализованного названия, а оставшийся текст упаковки сохраняется.
        """
        name = cls._clean_pdf_text(name)
        if not name:
            return None, None
        m = re.search(
            r"(?i)сер(?:ия|\.)?\s*[:№-]?\s*([A-Za-zА-Яа-яЁё0-9-]+)",
            name,
        )
        if not m:
            return name, None
        series = cls._normalize_series(m.group(1))
        cleaned = (name[:m.start()] + " " + name[m.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;-")
        cleaned = re.sub(r"\s+-\s*(?=\()", " ", cleaned).strip()
        return cleaned or None, series

    @classmethod
    def _normalize_series(cls, value):
        value = cls._clean_pdf_text(value)
        if not value:
            return None
        # Серии обычно разделяются с помощью переноса строк в формате PDF: "GM82A06 26".
        # if re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9]+(?:\s+[A-Za-zА-Яа-яЁё0-9]+)+", value):
        #     return value.replace(" ", "")
        # return value
        return re.sub(r"\s+", "", value)

    @staticmethod
    def _first_decimal(*values):
        for value in values:
            parsed = decimal_or_none(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _date_from_text(value):
        value = clean_text(value)
        if not value:
            return None
        m = re.search(r"\b\d{2}\.\d{2}\.(?:\d{2}|\d{4})\b", value)
        return date_or_none(m.group(0)) if m else None

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _is_protocol(text):
        norm = re.sub(r"\s+", " ", text.lower().replace("ё", "е"))
        # заголовок через пробел "П Р О Т О К О Л"
        compact = re.sub(r"\s+", "", norm)
        return "протокол" in compact and "согласованияцен" in compact

    @classmethod
    def _metadata(cls, text):
        lines = [clean_text(x) for x in text.splitlines()]
        lines = [x for x in lines if x]
        compact = " ".join(lines)

        document_number = None
        document_date = None
        basis_number = None
        basis_date = None

        # номер протокола и числовую дату
        m = re.search(
            r"Протокол\s*№\s*([^\s;]+)(?:\s+от\s+(\d{1,2}\.\d{1,2}\.\d{4}))?",
            compact, re.I,
        )
        if m:
            document_number = m.group(1)
            if m.group(2):
                document_date = date_or_none(cls._pad_numeric_date(m.group(2)))

        # Дата протокола, указанная с указанием месяца, например, "7 июля 2026 г.".
        if document_number and document_date is None:
            pattern = (
                r"Протокол\s*№\s*" + re.escape(document_number) +
                r"\s+от\s+(\d{1,2})\s+([А-Яа-яЁё]+)\s+(\d{4})\s*г?\."
            )
            m = re.search(pattern, compact, re.I)
            if m:
                document_date = cls._russian_date(m.group(1), m.group(2), m.group(3))

        # Basis: накладная / товарная накладная / УПД / т/н.
        basis_patterns = (
            r"(?:товарной\s+)?накладной\s*№\s*([^\s;]+)\s+от\s+(\d{1,2}\.\d{1,2}\.\d{4})",
            r"(?:СФ/)?УПД\s*№\s*([^\s;]+)\s+от\s+(\d{1,2}\.\d{1,2}\.\d{4})",
            r"Согласно\s+т/н\s*№\s*([^\s;]+)\s+от\s+(\d{1,2}\.\d{1,2}\.\d{4})",
        )
        for pat in basis_patterns:
            m = re.search(pat, compact, re.I)
            if m:
                basis_number = m.group(1)
                basis_date = date_or_none(cls._pad_numeric_date(m.group(2)))
                break

        supplier = cls._supplier_from_lines(lines)
        buyer = cls._buyer_from_lines(lines)

        # Явная помеченная форма, используемая некоторыми генераторами.
        if supplier is None:
            m = re.search(r"(?:^|\s)Организация:\s*(.+?)(?=\s+поставщик|\s+Контрагент:|$)", compact, re.I)
            if m:
                supplier = cls._repair_org_name(clean_text(m.group(1)))
        if buyer is None:
            m = re.search(r"Контрагент:\s*(.+?)(?=\s+получател|$)", compact, re.I)
            if m:
                buyer = cls._repair_org_name(clean_text(m.group(1)))

        return {
            "document_number": document_number,
            "document_date": document_date,
            "supplier": supplier,
            "buyer": buyer,
            "basis_number": basis_number,
            "basis_date": basis_date,
        }

    @classmethod
    def _buyer_from_lines(cls, lines):
        supplier_roles = [
            i for i, line in enumerate(lines)
            if "поставщик" in line.lower() or "поставшик" in line.lower()
        ]
        buyer_roles = [
            i for i, line in enumerate(lines)
            if "получател" in line.lower() or "покупател" in line.lower()
        ]
        org_re = re.compile(
            r"^(?:ООО|АО|ЗАО|ОАО|ПАО|Общество\s+с\s+ограниченной\s+ответственностью|бщество\s+с\s+ограниченной\s+ответственностью|Акционерное\s+Общество)\b",
            re.I,
        )
        if supplier_roles and buyer_roles:
            sidx, bidx = supplier_roles[0], buyer_roles[0]
            if sidx < bidx:
                after = [line for line in lines[bidx + 1:bidx + 6] if org_re.match(line)]
                if len(after) >= 2:
                    return cls._repair_org_name(after[1])
        for fragment in ("получател", "покупател"):
            value = cls._organization_near_role(lines, fragment)
            if value:
                return value
        return None

    @classmethod
    def _supplier_from_lines(cls, lines):
        # Handle both correct "поставщик" and common typo "поставшик".
        supplier_roles = [
            i for i, line in enumerate(lines)
            if "поставщик" in line.lower() or "поставшик" in line.lower()
        ]
        buyer_roles = [
            i for i, line in enumerate(lines)
            if "получател" in line.lower() or "покупател" in line.lower()
        ]

        org_re = re.compile(
            r"^(?:ООО|АО|ЗАО|ОАО|ПАО|Общество\s+с\s+ограниченной\s+ответственностью|бщество\s+с\s+ограниченной\s+ответственностью|Акционерное\s+Общество)\b",
            re.I,
        )

        # В Фармстандарт используется следующая структура: сначала указываются обе роли, затем поставщик и покупатель.
        if supplier_roles and buyer_roles:
            sidx, bidx = supplier_roles[0], buyer_roles[0]
            if sidx < bidx:
                after = [line for line in lines[bidx + 1:bidx + 6] if org_re.match(line)]
                if len(after) >= 2:
                    return cls._repair_org_name(after[0])

        for fragment in ("поставщик", "поставшик"):
            value = cls._organization_near_role(lines, fragment)
            if value:
                return value
        return None

    @staticmethod
    def _repair_org_name(value):
        value = clean_text(value)
        if value and value.lower().startswith("бщество с ограниченной ответственностью"):
            return "О" + value
        return value

    @staticmethod
    def _organization_near_role(lines, role_fragment):
        org_re = re.compile(
            r"^(?:ООО|АО|ЗАО|ОАО|ПАО|Общество\s+с\s+ограниченной\s+ответственностью|бщество\s+с\s+ограниченной\s+ответственностью|Акционерное\s+Общество)\b",
            re.I,
        )
        role_indices = [i for i, line in enumerate(lines) if role_fragment in line.lower()]
        for idx in role_indices:
            # Если сама ролевая строка начинается с организации, сохраняет префикс.
            if org_re.search(lines[idx]):
                before_role = re.split(r"\(?\s*" + role_fragment, lines[idx], flags=re.I)[0].strip()
                if org_re.match(before_role):
                    return before_role

            candidates = []
            for delta in (-1, 1, -2, 2, -3, 3):
                j = idx + delta
                if 0 <= j < len(lines) and org_re.match(lines[j]):
                    candidates.append((abs(delta), 0 if delta < 0 else 1, lines[j]))
            if candidates:
                candidates.sort()
                return PDFProtocolParser._repair_org_name(candidates[0][2])
        return None

    @staticmethod
    def _pad_numeric_date(value):
        parts = value.split(".")
        if len(parts) == 3:
            return f"{int(parts[0]):02d}.{int(parts[1]):02d}.{parts[2]}"
        return value

    @staticmethod
    def _russian_date(day, month, year):
        month_num = _RU_MONTHS.get(month.lower())
        if month_num is None:
            return None
        try:
            return date(int(year), month_num, int(day))
        except ValueError:
            return None

    @staticmethod
    def _open(source):
        try:
            if isinstance(source, bytes):
                return fitz.open(stream=source, filetype="pdf")
            return fitz.open(str(Path(source)))
        except Exception as exc:
            raise ProtocolParserError(f"Не удалось открыть PDF: {exc}") from exc
