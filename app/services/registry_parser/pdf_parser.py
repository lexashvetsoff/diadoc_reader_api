import re
import fitz
from pathlib import Path
from decimal import Decimal, InvalidOperation

from app.services.registry_parser.models import RegistryDocument, RegistryProduct
from app.services.registry_parser.document_type import DocumentType, DocumentTypeDetector
from app.services.registry_parser.layout import KnownLayoutFallback, TableLayout, Word, HeaderDetector
from app.services.registry_parser.exceptions import RegistryParserError, UnsupportedRegistryLayoutError, UnsupportedDocumentTypeError


def _norm(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я№]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _cell(value) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value or None


class GridTableParser:
    """
    Анализ таблиц PDF с разлиновкой с использованием распознавания таблиц PyMuPDF.

    Значение столбца определяется по семантическому тексту заголовка, а не по названию поставщика,
    и не по жестко заданным координатам X. После обнаружения заголовка строки данных
    считываются из фактических ячеек таблицы, поэтому многострочные ячейки не могут проникать в
    соседние строки или столбцы.
    """

    ALIASES = {
        "number": (
            "№ п п", "п п", "№",
        ),
        "code": (
            "код", "код товара", "код продукции", "артикул",
        ),
        "name": (
            "наименование товара",
            "наименование и краткая характеристика товара",
            "наименование и характеристика товара",
            "наименование краткая характеристика товара",
            "торговое название",
            "наименование продукции",
            "товар",
        ),
        "series": (
            "серия", "номер серии", "партия",
        ),
        "series_expiration": (
            "серия срок годности",
            "серия / срок годности",
            "серия годен до",
            "серия / годен до",
            "партия срок годности",
            "партия / срок годности",
        ),
        "manufacturer": (
            "производитель", "изготовитель",
            "наименование производителя",
            "наименование и местонахождение производителя",
            "предприятие изготовитель",
            "предприятие изготовитель страна",
        ),
        "quantity": (
            "количество", "количество упаковок", "кол во", "количество товара",
        ),
        "expiration": (
            "срок годности", "годен до", "срок годн",
        ),
    }

    REQUIRED = {"number", "name"}

    def inspect(self, doc: fitz.Document):
        """Обнаружение семантических столбцов и их физических диапазонов по оси X.

        Важно, чтобы последующие страницы сопоставлялись по геометрии,
        а не по ``table.col_count``. В некоторых PDF-файлах на следующей странице добавляются фантомные ячейки границы,
        хотя видимые столбцы бизнес-логики остаются точно на тех же координатах X.
        """
        candidates = []

        for page_index, page in enumerate(doc):
            try:
                tables = page.find_tables().tables
            except Exception:
                continue
            for table_index, table in enumerate(tables):
                rows = table.extract()
                if not rows or table.col_count < 3:
                    continue

                schema, header_rows, score = self._detect_schema(rows)
                if not schema:
                    continue

                ranges = self._semantic_ranges(table, rows, schema)
                if not ranges or not self.REQUIRED.issubset(ranges):
                    continue

                candidates.append(
                    (score, len(schema), page_index, table_index,
                     schema, header_rows, ranges)
                )

        if not candidates:
            return None, 0.0, False

        candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
        score, _, _, _, schema, header_rows, ranges = candidates[0]

        return {
            "indices": schema,
            "header_rows": header_rows,
            "ranges": ranges,
        }, min(0.995, score), True

    def parse(self, doc: fitz.Document, schema_info) -> list[RegistryProduct]:
        """Анализ физических строк таблицы с учетом возможности смещения сетки между страницами.

        Схема изучается один раз на странице заголовка. Каждая последующая физическая
        ячейка сопоставляется с семантическим столбцом по его X-геометрии, поэтому страницы продолжения могут содержать дополнительные 
        фантомные столбцы без нарушения сопоставления.
        Строки без порядкового номера могут продолжать предыдущую строку (реальная ячейка
        может продолжаться на следующей странице), но заголовки/легенды/нижние колонтитулы
        явно отклоняются.
        """
        ranges = schema_info["ranges"]
        products: list[RegistryProduct] = []
        current: dict[str, str | None] | None = None

        for page in doc:
            try:
                tables = page.find_tables().tables
            except Exception:
                continue

            for table in tables:
                extracted_rows = table.extract()
                for row_index, table_row in enumerate(table.rows):
                    if row_index >= len(extracted_rows):
                        continue
                    values = self._map_physical_row(
                        extracted_rows[row_index], table_row, ranges
                    )
                    if not values:
                        continue

                    number_text = _cell(values.get("number"))
                    if number_text and re.fullmatch(r"\d{1,5}", number_text):
                        name = _cell(values.get("name"))
                        if not name or not re.search(r"[A-Za-zА-Яа-я]", name):
                            continue

                        if current is not None:
                            p = self._current_to_product(current)
                            if p is not None:
                                products.append(p)

                        current = {
                            "number": number_text,
                            "name": name,
                            "code": _cell(values.get("code")),
                            "series": _cell(values.get("series")),
                            "series_expiration": _cell(values.get("series_expiration")),
                            "manufacturer": _cell(values.get("manufacturer")),
                            "quantity": _cell(values.get("quantity")),
                            "expiration": _cell(values.get("expiration")),
                        }
                        continue

                    # Физическая строка без порядкового номера может быть продолжением
                    # предыдущего продукта за пределами страницы. Не допускает, чтобы
                    # повторяющийся заголовок, техническая легенда или подпись/нижний колонтитул
                    # становились частью этого продукта.
                    if current is not None and self._is_continuation(values):
                        self._append_current(current, values)

        if current is not None:
            p = self._current_to_product(current)
            if p is not None:
                products.append(p)

        unique = {}
        for p in products:
            if p.line_number not in unique:
                unique[p.line_number] = p
        return [unique[k] for k in sorted(unique)]

    @staticmethod
    def _map_physical_row(extracted_row, table_row, ranges):
        """Map extracted cell text to semantic fields by physical X overlap."""
        result = {}
        cells = table_row.cells
        for key, (sx0, sx1) in ranges.items():
            semantic_width = max(1e-6, sx1 - sx0)
            best = None
            for idx, cell in enumerate(cells):
                if cell is None or idx >= len(extracted_row):
                    continue
                x0, y0, x1, y1 = map(float, cell)
                overlap = max(0.0, min(x1, sx1) - max(x0, sx0))
                if overlap <= 0:
                    continue
                ratio = overlap / semantic_width
                cell_width = max(1e-6, x1 - x0)
                # Точные/почти точные ячейки столбцов превосходят гигантские объединенные ячейки, которые
                # случайно охватывают один и тот же семантический диапазон X.
                width_penalty = abs(cell_width - semantic_width)
                center_penalty = abs((x0 + x1) / 2 - (sx0 + sx1) / 2)
                score = (-width_penalty / semantic_width, ratio, -center_penalty)
                if best is None or score > best[0]:
                    best = (score, idx)
            if best is not None:
                result[key] = _cell(extracted_row[best[1]])
        return result

    def _is_continuation(self, values):
        text = " ".join(v for v in values.values() if v)
        norm = _norm(text)
        if not norm:
            return False

        reject_phrases = (
            "наименование товара", "торговое название", "международное",
            "номер серии", "серия дата", "срок годности", "производитель",
            "документ качества", "вид документа", "регистрирующий орган",
            "количество упаковок", "код товара",
            "ответственное лицо", "товар отпустил", "товар получил",
            "подпись", "должность", "ф и о", "м п", "примечание",
            "сертификатов декларации находятся", "расшифровка кратких",
        )
        if any(phrase in norm for phrase in reject_phrases):
            return False
        if text.lstrip().startswith(("*", "**")):
            return False

        # Technical column-number legend: 1 | 2 | 3 | ...
        nonempty = [v.strip() for v in values.values() if v and v.strip()]
        if nonempty and all(re.fullmatch(r"\d{1,3}[а-яa-z]?", v, re.I) for v in nonempty):
            return False

        # Название и Продолжения, относящиеся к производителю, особенно часто встречаются на разных страницах.
        return any(values.get(k) for k in (
            "name", "code", "series", "series_expiration", "manufacturer", "quantity", "expiration"
        ))

    @staticmethod
    def _append_current(current, values):
        for key in ("name", "code", "series", "series_expiration", "manufacturer", "quantity", "expiration"):
            addition = _cell(values.get(key))
            if not addition:
                continue
            old = _cell(current.get(key))
            current[key] = f"{old} {addition}" if old else addition

    def _current_to_product(self, current):
        number_text = _cell(current.get("number"))
        name = _cell(current.get("name"))
        if not number_text or not re.fullmatch(r"\d{1,5}", number_text):
            return None
        if not name or not re.search(r"[A-Za-zА-Яа-я]", name):
            return None

        series = _cell(current.get("series"))
        expiration = _cell(current.get("expiration"))

        combined = _cell(current.get("series_expiration"))
        if combined:
            combined_series, combined_expiration = self._split_series_expiration(combined)
            if not series:
                series = combined_series
            if not expiration:
                expiration = combined_expiration

        series, embedded_expiration = self._normalize_series_cell(series)
        if not expiration and embedded_expiration:
            expiration = embedded_expiration
        
        if series:
            series = series.lstrip("'’")

        return RegistryProduct(
            line_number=int(number_text),
            name=name,
            product_code=_cell(current.get("code")),
            series=series,
            manufacturer=_cell(current.get("manufacturer")),
            quantity=self._quantity_text(_cell(current.get("quantity"))),
            expiration_date=self._expiration(expiration),
        )

    @staticmethod
    def _normalize_series_cell(value):
        """
        Нормализация ячейки серии PDF, которая также может содержать срок действия.

        Например:
            "B269931, 31.12.2028 /" -> ("B269931", "31.12.2028")
            "0526, 31.01.2031 /"    -> ("0526", "31.01.2031")
            "AB685, 31.12.2028 /"   -> ("AB685", "31.12.2028")
        """
        value = _cell(value)
        if not value:
            return None, None

        value = value.strip()
        patterns = (
            r"^(?P<series>.+?)\s*[,;]\s*(?P<expiration>\d{2}\.\d{2}\.\d{4})\s*/?\s*$",
            r"^(?P<series>.+?)\s*[,;]\s*(?P<expiration>\d{2}\.\d{2}\.\d{2})\s*/?\s*$",
        )
        for pattern in patterns:
            match = re.match(pattern, value)
            if match:
                return (
                    match.group("series").strip().rstrip(",;/"),
                    match.group("expiration").strip(),
                )

        return value.rstrip().rstrip("/").strip(), None

    @staticmethod
    def _quantity_text(value):
        if not value:
            return None
        # Сохраняется только первое физическое/семантическое значение. Нижний колонтитул страницы может
        # отображаться в том же геометрическом столбце после реального значения.
        first = value.splitlines()[0].strip()
        groups = re.findall(r"\d+(?:[.,]\d+)?", first)
        if not groups:
            return None
        if len(groups) == 1:
            token = groups[0]
        elif (
            len(groups) == 2
            and re.fullmatch(r"\d{1,3}", groups[0])
            and re.fullmatch(r"\d{3}", groups[1])
        ):
            # Тысячи, разделённые пробелом, например, "2 160".
            token = groups[0] + groups[1]
        else:
            # Пример элемента нижнего колонтитула: "30 1800003491" -> 30.
            token = groups[0]
        try:
            return Decimal(token.replace(",", "."))
        except InvalidOperation:
            return None

    @staticmethod
    def _semantic_ranges(table, rows, schema):
        """Get X ranges from a real data row on the header page."""
        for row_index, extracted in enumerate(rows):
            if row_index >= len(table.rows):
                continue
            number_idx = schema.get("number")
            name_idx = schema.get("name")
            if number_idx is None or name_idx is None:
                continue
            if number_idx >= len(extracted) or name_idx >= len(extracted):
                continue

            number = _cell(extracted[number_idx])
            name = _cell(extracted[name_idx])
            if not number or not re.fullmatch(r"\d{1,5}", number):
                continue
            if not name or not re.search(r"[A-Za-zА-Яа-я]", name):
                continue

            cells = table.rows[row_index].cells
            result = {}
            for key, idx in schema.items():
                if idx >= len(cells) or cells[idx] is None:
                    continue
                x0, _, x1, _ = cells[idx]
                if x1 > x0:
                    result[key] = (float(x0), float(x1))
            if {"number", "name"}.issubset(result):
                return result
        return None

    @staticmethod
    def _row_y_range(table_row, number_range):
        """Находит физическую высоту строки, исходя из ячейки в числовом столбце.

        Это позволяет избежать использования ``row.bbox``, поскольку на некорректно сформированных страницах продолжения может быть
        одна большая объединенная фоновая ячейка, занимающая множество строк.
        """
        nx0, nx1 = number_range
        best = None
        for cell in table_row.cells:
            if cell is None:
                continue
            x0, y0, x1, y1 = map(float, cell)
            overlap = max(0.0, min(x1, nx1) - max(x0, nx0))
            if overlap <= 0:
                continue
            width = max(1e-6, nx1 - nx0)
            ratio = overlap / width
            # Предпочтительнее использовать ячейку, которая максимально точно соответствует числовому столбцу и имеет
            # обычную высоту строки, а не огромную ячейку, объединенную с основной страницей.
            height = y1 - y0
            score = (ratio, -abs(x0 - nx0) - abs(x1 - nx1), -height)
            if best is None or score > best[0]:
                best = (score, (y0, y1))
        if best is None:
            return None
        y0, y1 = best[1]
        if y1 <= y0 or y1 - y0 > 120:
            return None
        return y0, y1

    @staticmethod
    def _text_in_rect(words, x_range, y_range):
        x0, x1 = x_range
        y0, y1 = y_range
        selected = []
        for w in words:
            wx0, wy0, wx1, wy1, text, *_ = w
            cx = (wx0 + wx1) / 2
            cy = (wy0 + wy1) / 2
            if x0 <= cx < x1 and y0 <= cy < y1:
                selected.append(w)
        if not selected:
            return None

        # Сохранение порядка строк при нормализации пробелов.
        lines = []
        for w in sorted(selected, key=lambda z: (round(z[1], 1), z[0])):
            for line in lines:
                if abs(line[0] - w[1]) <= 2.2:
                    line[1].append(w)
                    break
            else:
                lines.append([w[1], [w]])
        value = " ".join(
            " ".join(q[4] for q in sorted(ws, key=lambda z: z[0]))
            for _, ws in lines
        )
        return _cell(value)

    def _values_to_product(self, values):
        number_text = _cell(values.get("number"))
        if not number_text or not re.fullmatch(r"\d{1,5}", number_text):
            return None

        name = _cell(values.get("name"))
        if not name or not re.search(r"[A-Za-zА-Яа-я]", name):
            return None

        series = _cell(values.get("series"))
        if series:
            series = series.lstrip("'’")

        return RegistryProduct(
            line_number=int(number_text),
            name=name,
            product_code=_cell(values.get("code")),
            series=series,
            manufacturer=_cell(values.get("manufacturer")),
            quantity=self._decimal(_cell(values.get("quantity"))),
            expiration_date=self._expiration(_cell(values.get("expiration"))),
        )

    def _detect_schema(self, rows):
        best = None

        # Детектор таблиц PDF может добавлять строку метаданных перед фактическим заголовком
        # (например, информация о поставщике/покупателе, объединенная по всей ширине таблицы).
        # Поэтому не следует предполагать, что rows[0] является заголовком.
        # Сканирует короткие окна в верхней части и выбирайте наиболее полную семантическую карту.
        max_start = min(6, len(rows))
        for start in range(max_start):
            for count in range(1, min(4, len(rows) - start) + 1):
                window = rows[start:start + count]
                width = max(len(r) for r in window)
                combined = []
                for col in range(width):
                    parts = []
                    for r in window:
                        if col < len(r) and r[col]:
                            parts.append(str(r[col]))
                    combined.append(_norm(" ".join(parts)))

                schema = {}
                for idx, text in enumerate(combined):
                    match = self._classify_header(text)
                    if match:
                        key, match_score = match
                        old = schema.get(key)
                        if old is None or match_score > old[1]:
                            schema[key] = (idx, match_score)

                if not self.REQUIRED.issubset(schema):
                    continue

                semantic = {k: v[0] for k, v in schema.items()}
                semantic_count = len(semantic)
                raw_score = 0.58 + 0.055 * semantic_count
                if {"series", "manufacturer", "expiration"}.issubset(semantic):
                    raw_score += 0.08
                if "quantity" in semantic:
                    raw_score += 0.03

                # Ранжирование в первую очередь осуществляется по количеству найденных уникальных семантических столбцов.
                # Это позволяет избежать ситуации, когда широкое окно метаданных + заголовка превосходит
                # чистый заголовок в одну строку после ограничения оценки. Предпочтительнее использовать более короткие
                # окна и более ранние заголовки только в качестве критерия разрешения спорных ситуаций.
                rank = (semantic_count, raw_score, -count, -start)
                candidate = (rank, semantic, count, start, raw_score)
                if best is None or candidate[0] > best[0]:
                    best = candidate

        if best is None:
            return None, 0, 0

        _, semantic, count, start, raw_score = best
        return semantic, count, min(raw_score, 0.995)

    def _classify_header(self, text):
        if not text:
            return None

        # Перед более общим отдельным псевдонимом "серии" необходимо проверить 
        # объединенный семантический столбец, иначе "Серия / Срок годности"
        # будет классифицировано просто как "серия".
        combined = _norm(text)
        for alias in self.ALIASES.get("series_expiration", ()):
            a = _norm(alias)
            if combined == a or (len(a) >= 5 and a in combined):
                return ("series_expiration", 1.0)

        best = None
        for key, aliases in self.ALIASES.items():
            if key == "series_expiration":
                continue
            for alias in aliases:
                a = _norm(alias)
                score = None
                if text == a:
                    score = 1.0
                elif len(a) >= 5 and a in text:
                    score = 0.92
                elif len(text) >= 5 and text in a:
                    score = 0.86
                if score is not None and (best is None or score > best[1]):
                    best = (key, score)
        return best

    @staticmethod
    def _looks_like_data_row(row):
        if not row:
            return False
        # Выполняет поиск в первых нескольких столбцах, так как некоторые некорректно извлеченные таблицы 
        # содержат пустую ячейку в начале.
        for value in row[:3]:
            text = _cell(value)
            if text and re.fullmatch(r"\d{1,5}", text):
                return True
        return False

    def _row_to_product(self, row, schema):
        def get(key):
            idx = schema.get(key)
            if idx is None or idx >= len(row):
                return None
            return _cell(row[idx])

        number_text = get("number")
        if not number_text or not re.fullmatch(r"\d{1,5}", number_text):
            return None

        # Числовое "название" обычно представляет собой техническую легенду столбца.
        # 1|2|3|... печатается под многострочным заголовком, а не продуктом.
        name = get("name")
        if not name or not re.search(r"[A-Za-zА-Яа-я]", name):
            return None

        quantity = self._quantity_from_raw(row, schema)
        series = get("series")
        expiration = get("expiration")

        combined = get("series_expiration")
        if combined:
            combined_series, combined_expiration = self._split_series_expiration(combined)
            if not series:
                series = combined_series
            if not expiration:
                expiration = combined_expiration
        
        if series:
            series = series.lstrip("'’").strip()

        return RegistryProduct(
            line_number=int(number_text),
            name=name,
            product_code=get("code"),
            series=series,
            manufacturer=get("manufacturer"),
            quantity=quantity,
            expiration_date=self._expiration(expiration),
        )

    @staticmethod
    def _split_series_expiration(value):
        """
        Разделите объединенную ячейку, например:
            "Y0C521M(М-8); 31.07.2026"
            "5PZ0152; 31.12.2026"
            "Н4450326; 28.02.2029"
        """
        if not value:
            return None, None

        value = re.sub(r"\s+", " ", value).strip()

        patterns = (
            r"^(?P<series>.+?)\s*;\s*(?P<expiration>\d{2}\.\d{2}\.\d{4})\s*$",
            r"^(?P<series>.+?)\s+(?P<expiration>\d{2}\.\d{2}\.\d{4})\s*$",
            r"^(?P<series>.+?)\s*;\s*(?P<expiration>\d{2}\.\d{4})\s*$",
        )
        for pattern in patterns:
            m = re.match(pattern, value)
            if m:
                return (
                    m.group("series").strip().rstrip(";"),
                    m.group("expiration").strip(),
                )

        return value.rstrip(";").strip(), None

    @staticmethod
    def _quantity_from_raw(row, schema):
        idx = schema.get("quantity")
        if idx is None or idx >= len(row) or row[idx] is None:
            return None

        # Некоторые генераторы PDF-файлов добавляют нижний колонтитул страницы в последнюю строку таблицы.
        # Пример: "30\n1800003491". Только первая физическая строка относится к
        # ячейке с количеством. Пробелы внутри этой строки могут быть разделителями тысяч,
        # например, "2 160".
        first_line = str(row[idx]).splitlines()[0].strip()
        compact = first_line.replace(" ", "").replace("\xa0", "").replace(",", ".")
        if not re.fullmatch(r"\d+(?:\.\d+)?", compact):
            return None
        try:
            return Decimal(compact)
        except InvalidOperation:
            return None

    @staticmethod
    def _decimal(value):
        if not value:
            return None
        # В ячейках PDF тысячи могут быть разделены пробелами: "2 160".
        compact = value.replace(" ", "").replace("\xa0", "").replace(",", ".")
        m = re.fullmatch(r"\d+(?:\.\d+)?", compact)
        if not m:
            return None
        try:
            return Decimal(compact)
        except InvalidOperation:
            return None

    @staticmethod
    def _expiration(value):
        if not value:
            return None
        value = value.strip().lstrip("'’")
        if re.fullmatch(r"\d{2}\.\d{4}", value):
            return value
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
            return value
        m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{2})", value)
        if m:
            return f"{m.group(1)}.{m.group(2)}.20{m.group(3)}"
        return value


class CoordinateRegistryParser:
    """
    Парсер координат для текстовых таблиц без надежной разметки или для устаревших PDF-файлов, 
    в которых извлеченная геометрия таблицы изменяется в середине файла.
    """

    MIN_AUTO_CONFIDENCE = 0.78

    def __init__(self, *, allow_fallback: bool = True):
        self.allow_fallback = allow_fallback
        self.header_detector = HeaderDetector()

    def parse_products(self, doc, full_text):
        pages = [self._words(p) for p in doc]
        layout = self._detect_layout(doc, pages, full_text)
        products = self._parse_pages(pages, layout)
        self._validate_products(products, layout)
        return products, layout

    def _detect_layout(self, doc, pages, full_text):
        for page, words in zip(doc, pages):
            layout = self.header_detector.detect(words, page.rect.width)
            if layout and layout.confidence >= self.MIN_AUTO_CONFIDENCE:
                return layout
        if self.allow_fallback:
            fallback = KnownLayoutFallback.detect(full_text)
            if fallback:
                return fallback
        raise UnsupportedRegistryLayoutError(
            "Не удалось автоматически определить колонки реестра"
        )

    @staticmethod
    def _words(page) -> list[Word]:
        return [tuple(w) for w in page.get_text("words", sort=True)]

    def _parse_pages(self, pages, layout):
        products = []
        current = None
        last_line_number = 0
        for words in pages:
            stop_y = self._table_stop_y(words)
            starts = [s for s in self._row_starts(words, layout, last_line_number)
                      if s[0] < stop_y]

            if current is not None and starts:
                lead = [w for w in words if 40 < w[1] < min(starts[0][0], stop_y)]
                self._append_fragment(current, lead, layout)
            elif current is not None and not starts:
                self._append_fragment(
                    current, [w for w in words if 40 < w[1] < stop_y], layout
                )

            for idx, (y, line_number) in enumerate(starts):
                if current is not None:
                    products.append(self._finalize(current))
                end_y = starts[idx + 1][0] if idx + 1 < len(starts) else stop_y
                fragment = [w for w in words if y - 6 <= w[1] < end_y - 6]
                current = {
                    "line_number": line_number,
                    "name": [], "code": [], "series": [], "manufacturer": [],
                    "quantity": [], "expiration": [],
                }
                self._append_fragment(current, fragment, layout)
                last_line_number = line_number

        if current is not None:
            products.append(self._finalize(current))
        return products

    @staticmethod
    def _table_stop_y(words):
        markers = {"Примечание:", "Проверил", "Ответственное", "М.П."}
        ys = [w[1] for w in words if w[4] in markers]
        ys += [w[1] for w in words if w[4].startswith("*") and w[1] > 400]
        return min(ys) if ys else 10_000

    def _row_starts(self, words, layout, previous_number):
        x0, x1 = layout.number
        candidates = []
        for w in words:
            x, y, _, _, txt, *_ = w
            if not (x0 <= x < x1) or not txt.isdigit():
                continue
            n = int(txt)
            if n <= 0 or n > 10000 or y < 45:
                continue
            has_name = any(
                layout.name[0] <= (q[0] + q[2]) / 2 < layout.name[1]
                and -8 <= q[1] - y <= 12
                and bool(re.search(r"[A-Za-zА-Яа-я]", q[4]))
                for q in words
            )
            if has_name:
                candidates.append((y, n))

        out = []
        expected = previous_number + 1 if previous_number else None
        for y, n in sorted(candidates):
            if expected is None:
                out.append((y, n))
                expected = n + 1
            elif n == expected:
                out.append((y, n))
                expected += 1
        return out

    def _append_fragment(self, row, words, layout):
        mapping = {
            "name": layout.name,
            "code": layout.code,
            "series": layout.series,
            "manufacturer": layout.manufacturer,
            "quantity": layout.quantity,
            "expiration": layout.expiration,
        }
        for key, rng in mapping.items():
            if rng is None:
                continue
            selected = [w for w in words
                        if rng[0] <= (w[0] + w[2]) / 2 < rng[1]]
            if selected:
                row[key].append(self._join_words(selected))

    @staticmethod
    def _join_words(words):
        lines = []
        for w in sorted(words, key=lambda z: (round(z[1], 1), z[0])):
            for line in lines:
                if abs(line[0] - w[1]) <= 2.2:
                    line[1].append(w)
                    break
            else:
                lines.append([w[1], [w]])
        return " ".join(
            " ".join(q[4] for q in sorted(ws, key=lambda z: z[0]))
            for _, ws in lines
        ).strip()

    def _finalize(self, row):
        text = lambda k: self._clean(" ".join(row[k])) or None
        series = text("series")
        if series:
            series = series.lstrip("'’")
        qty = text("quantity")
        quantity = None
        if qty:
            m = re.search(r"\d+(?:[.,]\d+)?", qty)
            if m:
                quantity = Decimal(m.group().replace(",", "."))
        return RegistryProduct(
            line_number=row["line_number"],
            name=text("name"),
            product_code=text("code"),
            series=series,
            manufacturer=text("manufacturer"),
            quantity=quantity,
            expiration_date=GridTableParser._expiration(text("expiration")),
        )

    @staticmethod
    def _clean(s):
        return re.sub(r"\s+", " ", s).strip()

    @staticmethod
    def _validate_products(products, layout):
        if not products:
            raise UnsupportedRegistryLayoutError(
                "Колонки найдены, но товарные строки не распознаны"
            )
        numbers = [p.line_number for p in products if p.line_number is not None]
        if len(numbers) > 1 and any(b != a + 1 for a, b in zip(numbers, numbers[1:])):
            raise UnsupportedRegistryLayoutError(
                "Нарушена последовательность порядковых номеров"
            )


class PDFRegistryParser:
    """
    Гибридный универсальный парсер.
    1. Таблица с разлиновкой -> фактические ячейки таблицы PyMuPDF.
    2. Таблица без границ / с несогласованными данными -> детектор семантических координат.
    3. Дополнительный резервный вариант с известной компоновкой только в режиме координат.
    """

    def __init__(self, *, allow_fallback: bool = True):
        self.allow_fallback = allow_fallback
        self.grid_parser = GridTableParser()
        self.document_type_detector = DocumentTypeDetector()

    def parse(self, source: str | Path | bytes) -> RegistryDocument:
        doc = self._open(source)
        try:
            full_text = "\n".join(p.get_text("text") for p in doc)

            document_type = self.document_type_detector.detect(full_text)
            if document_type != DocumentType.REGISTRY:
                raise UnsupportedDocumentTypeError(
                    f"PDF имеет тип {document_type.value!r}, а не registry"
                )
            
            supplier, buyer, inv_no, inv_date = self._metadata(full_text)

            schema, grid_confidence, grid_reliable = self.grid_parser.inspect(doc)

            if schema is not None and grid_reliable:
                products = self.grid_parser.parse(doc, schema)
                self._validate_sequence(products)
                source_layout = "grid:" + self._fingerprint(schema["indices"])
                layout_source = "auto"
                confidence = grid_confidence
            else:
                coordinate = CoordinateRegistryParser(
                    allow_fallback=self.allow_fallback
                )
                products, layout = coordinate.parse_products(doc, full_text)
                source_layout = layout.fingerprint
                layout_source = layout.source
                confidence = layout.confidence

            return RegistryDocument(
                source_layout=source_layout,
                layout_confidence=round(confidence, 3),
                layout_source=layout_source,
                supplier=supplier,
                buyer=buyer,
                invoice_number=inv_no,
                invoice_date=inv_date,
                products=products,
            )
        finally:
            doc.close()

    @staticmethod
    def _open(source):
        try:
            if isinstance(source, bytes):
                return fitz.open(stream=source, filetype="pdf")
            return fitz.open(str(Path(source)))
        except Exception as exc:
            raise RegistryParserError(f"Не удалось открыть PDF: {exc}") from exc

    @staticmethod
    def _fingerprint(indices):
        order = (
            "number", "code", "name", "series", "series_expiration",
            "quantity", "expiration", "manufacturer",
        )
        return "+".join(k for k in order if k in indices)

    @staticmethod
    def _validate_sequence(products):
        if not products:
            raise UnsupportedRegistryLayoutError(
                "Таблица распознана, но товарных строк не найдено"
            )
        nums = [p.line_number for p in products if p.line_number is not None]
        if nums and nums != list(range(nums[0], nums[-1] + 1)):
            raise UnsupportedRegistryLayoutError(
                f"Нарушена последовательность строк: {nums}"
            )

    @staticmethod
    def _metadata(text: str):
        supplier = buyer = inv_no = inv_date = None

        lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
        lines = [x for x in lines if x]

        org_re = re.compile(r"\b(ООО|АО|ЗАО|ОАО|ПАО|ИП)\b", re.I)

        def value_near_label(prefixes):
            for i, line in enumerate(lines):
                low = line.lower()
                if not any(low.startswith(p.lower()) for p in prefixes):
                    continue

                # Inline value: "Поставщик: ООО ..."
                if ":" in line:
                    tail = line.split(":", 1)[1].strip()
                    if tail and org_re.search(tail):
                        return tail

                # В PDF-файлах можно разместить текстовое представление непосредственно перед или после его метки. 
                # Предпочтительнее расположить его рядом, чтобы визуальное представление выглядело более упорядоченным.
                if i + 1 < len(lines) and org_re.search(lines[i + 1]):
                    return lines[i + 1]
                if i > 0 and org_re.search(lines[i - 1]):
                    return lines[i - 1]
            return None

        supplier = value_near_label(("Поставщик:",))
        buyer = value_near_label(("Покупатель:", "Получатель:", "Грузополучатель:"))

        if supplier is None:
            for line in lines[:15]:
                if org_re.match(line):
                    supplier = line
                    break

        patterns = [
            r"Реестр\s+сертификатов\s+к\s+УПД\s+№\s*(\S+)\s+от\s+(\d{2}\.\d{2}\.\d{4})",
            r"Приложение\s+к\s+УПД\s+№\s*(\S+)\s+от\s+(\d{2}\.\d{2}\.\d{4})",
            r"к\s+накладной\s+№\s*(\S+)\s+от\s+(\d{2}\.\d{2}\.\d{4})",
            r"по\s+накладной\s+(\S+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if m:
                inv_no = m.group(1)
                if m.lastindex and m.lastindex >= 2:
                    inv_date = m.group(2)
                break

        return supplier, buyer, inv_no, inv_date
