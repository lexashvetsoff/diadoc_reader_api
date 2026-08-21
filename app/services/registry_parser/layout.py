import re
from dataclasses import dataclass


Word = tuple[float, float, float, float, str, int, int, int]
Range = tuple[float, float]


@dataclass(frozen=True)
class ColumnBox:
    key: str
    x0: float
    x1: float
    y: float
    label: str
    confidence: float = 1.0


@dataclass(frozen=True)
class TableLayout:
    number: Range
    name: Range
    code: Range | None = None
    series: Range | None = None
    manufacturer: Range | None = None
    quantity: Range | None = None
    expiration: Range | None = None
    confidence: float = 0.0
    source: str = 'auto'
    fingerprint: str = 'generic'
    header_y: float = 0.0


class HeaderDetector:
    """Определение столбцов на основе семантического кластера слов в заголовках таблиц.

    Название поставщика намеренно не используется в качестве входных данных. Для обнаружения используется только текст заголовка и геометрия PDF-файла. 
    Многократные вхождения, такие как «№» или «Срок» в других местах страницы, разрешаются путем выбора горизонтальной полосы, содержащей
    наибольший набор семантических якорей столбцов.
    """

    BAND_RADIUS = 24.0

    def detect(self, words: list[Word], page_width: float) -> TableLayout | None:
        anchors = self._collect_anchors(words)
        if not anchors:
            return None

        band_y, band = self._best_band(anchors)
        by_key: dict[str, ColumnBox] = {}
        for a in band:
            old = by_key.get(a.key)
            if old is None or a.confidence > old.confidence or (
                a.confidence == old.confidence and a.x0 < old.x0
            ):
                by_key[a.key] = a

        # Реестр без номера/имени нельзя безопасно разделить на строки.
        if 'number' not in by_key or 'name' not in by_key:
            return None

        # Нам необходимо как минимум 4 семантических столбца в кластере заголовков, чтобы избежать
        # путаницы между метаданными и таблицей.
        if len(by_key) < 4:
            return None

        # В сгруппированном заголовке типа Акрихина используется явный низкоуровневый 
        # Подзаголовок "торговое" вместо широкого "Наименование товара".
        trading = self._nearest_word(words, 'торговое', band_y)
        international = self._nearest_prefix(words, 'международ', band_y)
        if trading is not None:
            current = by_key['name']
            if current.x0 - 40 <= trading[0] <= current.x1 + 60:
                by_key['name'] = ColumnBox(
                    'name', trading[0], trading[2], trading[1], 'торговое', 1.0
                )

        ordered = sorted(by_key.values(), key=lambda b: b.x0)
        # Заголовочные столбцы, которые мы не извлекаем, по-прежнему имеют значение в качестве геометрических разделителей.
        # Примеры: "Декларант" после производителя и "Вид документ" после производителя в реестре Medisorb.
        boundary_x = []
        for w in words:
            if abs(w[1] - band_y) <= self.BAND_RADIUS:
                n = self._norm(w[4])
                if (n.startswith("декларант") or n in {"вид", "номер", "дата", "регистрирующий", "орган"}):
                    boundary_x.append(w[0])

        ranges: dict[str, Range] = {}
        for i, box in enumerate(ordered):
            left = max(0.0, box.x0 - 15) if i == 0 else (ordered[i - 1].x1 + box.x0) / 2
            right = min(page_width, box.x1 + 25) if i == len(ordered) - 1 else (box.x1 + ordered[i + 1].x0) / 2
            # Ненужная соседняя колонна может обеспечить более близкую правую кромку.
            nuisance_right = [x for x in boundary_x if x > box.x1]
            if nuisance_right:
                right = min(right, (box.x1 + min(nuisance_right)) / 2)
            nuisance_left = [x for x in boundary_x if x < box.x0]
            if nuisance_left:
                left = max(left, (max(nuisance_left) + box.x0) / 2)
            ranges[box.key] = (left, right)

        # Если имя имеет второй подстолбец (международное непатентованное имя),
        # завершаем диапазон торговых наименований перед ним.
        if trading is not None and international is not None and international[0] > trading[2]:
            ranges['name'] = (
                ranges['name'][0],
                (trading[2] + international[0]) / 2
            )
            # Подстолбец с международными названиями расположен между торговым наименованием и
            # серией. Поэтому его правый край является фактическим левым краем серии.
            if 'series' in ranges:
                series_box = by_key['series']
                ranges['series'] = ((international[2] + series_box.x0) / 2, ranges['series'][1])

        keys = [k for k in ("number", "code", "name", "series", "quantity", "expiration", "manufacturer") if k in by_key]
        # 5 семантических якорей = достаточно высокая степень уверенности для автоматического режима. 
        # Дополнительные столбцы с кодом/количеством повышают уверенность, но не являются обязательными.
        confidence = min(0.99, 0.62 + 0.055 * len(by_key))
        if {'series', 'manufacturer', 'expiration'}.issubset(by_key):
            confidence += 0.05
        confidence = min(0.99, confidence)

        return TableLayout(
            number=ranges['number'],
            name=ranges['name'],
            code=ranges.get('code'),
            series=ranges.get('series'),
            manufacturer=ranges.get('manufacturer'),
            quantity=ranges.get('quantity'),
            expiration=ranges.get('expiration'),
            confidence=confidence,
            source='auto',
            fingerprint='+'.join(keys),
            header_y=band_y
        )

    def _collect_anchors(self, words: list[Word]) -> list[ColumnBox]:
        out: list[ColumnBox] = []
        usable = [w for w in words if w[1] < 320]

        for w in usable:
            x0, y0, x1, _, text, *_ = w
            n = self._norm(text)
            if not n:
                continue

            if text == '№':
                out.append(ColumnBox('number', x0, x1, y0, text, 0.92))
            if n in {"серия", "партия"}:
                out.append(ColumnBox('series', x0, x1, y0, text, 1.0))
            if n in {"количество", "кол во"}:
                out.append(ColumnBox('quantity', x0, x1, y0, text, 1.0))
            if n in {"производитель", "изготовитель", "производителя", "изготовителя"}:
                out.append(ColumnBox('manufacturer', x0, x1, y0, text, 1.0))
            if n == 'годности':
                out.append(ColumnBox('expiration', x0, x1, y0, text, 1.0))
            if n == 'торговое':
                out.append(ColumnBox('name', x0, x1, y0, text, 1.0))
            if n == 'наименование':
                # Распространить «Наименование товара» на второй токен, если он присутствует. 
                # Второе «Наименование» перед «производителем» остается ниже приоритет, чем явная привязка производителя.
                end = x1
                for q in usable:
                    if abs(q[1] - y0) <= 3 and 0 <= q[0] - x1 <= 10 and self._norm(q[4]) in {"товара", "продукции"}:
                        end = q[2]
                        break
                out.append(ColumnBox('name', x0, end, y0, text, 0.82))
            if n == 'код':
                out.append(ColumnBox('code', x0, x1, y0, text, 0.9))

        # Многословные псевдонимы p/p и code/name делают привязки более надежными.
        for i, w in enumerate(usable):
            x0, y0, x1, _, text, *_ = w
            n = self._norm(text)
            if n == "п п":
                out.append(ColumnBox("number", x0, x1, y0, text, 1.0))
            if n == "код" and self._near_token(usable, i, "товара"):
                xs = [w, self._near_token(usable, i, "товара", return_word=True)]
                xs = [q for q in xs if q is not None]
                out.append(ColumnBox("code", min(q[0] for q in xs), max(q[2] for q in xs), y0, "код товара", 1.0))

        return out

    def _best_band(self, anchors: list[ColumnBox]) -> tuple[float, list[ColumnBox]]:
        best_score = -1.0
        best_y = 0.0
        best_band: list[ColumnBox] = []

        for pivot in anchors:
            band = [a for a in anchors if abs(a.y - pivot.y) <= self.BAND_RADIUS]
            unique = {a.key for a in band}
            # Поощряется семантическое разнообразие; в метаданных часто встречается только числовое значение.
            score = len(unique) * 10 + sum(a.confidence for a in band)
            if {'series', 'manufacturer'}.issubset(unique):
                score += 8
            if 'expiration' in unique:
                score += 4

            if score > best_score:
                best_score = score
                best_y = sum(a.y for a in band) / len(band)
                best_band = band

        return best_y, best_band

    def _nearest_word(self, words: list[Word], token: str, band_y: float):
        matches = [w for w in words if self._norm(w[4]) == token and abs(w[1] - band_y) <= self.BAND_RADIUS]
        return min(matches, key=lambda w: abs(w[1] - band_y)) if matches else None

    def _nearest_prefix(self, words: list[Word], prefix: str, band_y: float):
        p = self._norm(prefix)
        matches = [w for w in words if self._norm(w[4]).startswith(p) and abs(w[1] - band_y) <= self.BAND_RADIUS]
        return min(matches, key=lambda w: abs(w[1] - band_y)) if matches else None

    @staticmethod
    def _near_token(words: list[Word], index: int, token: str, return_word: bool = False):
        source = words[index]
        for q in words:
            if q is source:
                continue
            if abs(q[1] - source[1]) <= 3 and 0 <= q[0] - source[2] <= 10:
                if HeaderDetector._norm(q[4]) == token:
                    return q if return_word else True
        return None if return_word else False

    @staticmethod
    def _norm(value: str) -> str:
        value = value.lower().replace("ё", "е")
        value = re.sub(r"[^0-9a-zа-я№]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()


class KnownLayoutFallback:
    """Профили, используемые в крайнем случае, для PDF-файлов,
    заголовки которых невозможно безопасно обнаружить, либо структура файла уже известна.
    """

    PROFILES = {
        "registry_v1_akrikhin": TableLayout(
            number=(60, 85), code=(85, 150), name=(145, 245), series=(305, 360),
            quantity=(355, 410), expiration=(410, 450), manufacturer=(450, 535),
            confidence=0.75, source="fallback", fingerprint="registry_v1_akrikhin",
        ),
        "registry_v1_medintorg": TableLayout(
            number=(30, 55), name=(55, 205), code=None, series=(205, 255),
            manufacturer=(255, 370), quantity=None, expiration=(370, 425),
            confidence=0.75, source="fallback", fingerprint="registry_v1_medintorg",
        ),
        "registry_v1_medisorb": TableLayout(
            number=(30, 55), name=(55, 185), code=None, series=(185, 240),
            manufacturer=(240, 345), quantity=None, expiration=(735, 800),
            confidence=0.75, source="fallback", fingerprint="registry_v1_medisorb",
        ),
    }

    @classmethod
    def detect(cls, text: str) -> TableLayout | None:
        t = text.upper()
        if "КОД ТОВАРА" in t and "КОЛИЧЕСТВО" in t and "АКРИХИН" in t:
            return cls.PROFILES["registry_v1_akrikhin"]
        if "ПОСТАВЩИК:" in t and "МЕДИНТОРГ" in t:
            return cls.PROFILES["registry_v1_medintorg"]
        if "ТОРГОВОЕ НАЗВАНИЕ" in t and "МЕДИСОРБ" in t:
            return cls.PROFILES["registry_v1_medisorb"]
        return None
