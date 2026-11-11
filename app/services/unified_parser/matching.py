import re
import unicodedata
from decimal import Decimal
from dataclasses import dataclass, field
from rapidfuzz.fuzz import WRatio, token_set_ratio


_LOOKALIKE_TO_LATIN = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
    "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "Х": "X", "У": "Y",
})


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_identifier(value: str | None) -> str:
    value = clean_text(value).upper().replace("Ё", "Е")
    value = value.translate(_LOOKALIKE_TO_LATIN)
    return re.sub(r"[^0-9A-Z]+", "", value)


def normalize_product_code(value: str | None) -> str:
    """Стандартизация элементов оформления поставщика без изменения фактического кода продукта.

    Examples from real UPD:
        349632 (М) -> 349632
        354422 МП  -> 354422
        GTFC-0100-000 -> GTFC0100000
    """
    value = clean_text(value).upper().replace("Ё", "Е")
    # Supplier-specific marking suffixes are metadata, not part of the catalogue code.
    value = re.sub(r"\s*(?:\([МM]\)|[МM][ПP])\s*$", "", value)
    value = value.translate(_LOOKALIKE_TO_LATIN)
    return re.sub(r"[^0-9A-Z]+", "", value)


def normalize_gtin(value: str | None) -> str:
    """Canonicalize GTIN-13 / GTIN-14 with a packaging leading zero."""
    digits = re.sub(r"\D+", "", clean_text(value))
    if len(digits) == 14 and digits.startswith("0"):
        return digits[1:]
    return digits


def normalize_series(value: str | None) -> str:
    value = clean_text(value)

    value = re.sub(
        r"\s*\(\s*2\s*D\s*[-–—]?\s*код\s*\)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    # "(070426, 03.2029)" or "(В9663, 01.2029)" is a technical
    value = re.sub(
        r"\s*\(\s*[^,()]+\s*,\s*\d{2}\.\d{4}\s*\)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s*\(\s*\d{2}\.\d{2}\.\d{4}\s*\)\s*$",
        "",
        value,
    )

    return normalize_identifier(value)


def normalize_name(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", clean_text(value)).lower()
    value = value.replace("ё", "е").replace("®", " ").replace("™", " ")
    # GTIN / barcode at the end is not a semantic part of the product name.
    value = re.sub(r"(?<!\d)\d{13,14}(?!\d)", " ", value)
    value = value.replace("№", " n ")
    replacements = {
        "таб.": " таблетки ",
        "капс.": " капсулы ",
        "р-р": " раствор ",
        "д/": " для ",
        "в/м": " внутримышечно ",
        "п/к": " подкожно ",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    value = re.sub(r"[^0-9a-zа-я%+]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


_GENERIC_PRODUCT_HEADS = {
    "вакцина",
    "таблетки",
    "таблетка",
    "капсулы",
    "капсула",
    "мазь",
    "крем",
    "гель",
    "раствор",
    "сироп",
    "порошок",
    "гранулы",
    "суспензия",
    "капли",
    "свечи",
    "суппозитории",
}


def product_identity_token(value: str | None) -> str:
    """
    Возвращает уникальный ведущий токен продукта/бренда.

    Он намеренно игнорирует префиксы общих лекарственных форм, такие как «Вакцина»,
    так что «Вакцина Пентаксим ...» и «Пентаксим ...» приводят к одному и тому же токену.

    Это не автономный сопоставитель; это защита от ложных совпадений, когда
    несвязанные лекарственные препараты имеют одинаковый номер партии/серии.
    """
    normalized = normalize_name(value)
    tokens = re.findall(r"[0-9a-zа-я]+", normalized)

    for token in tokens[:6]:
        if token in _GENERIC_PRODUCT_HEADS:
            continue
        if len(token) < 3:
            continue
        return token

    return tokens[0] if tokens else ""


def same_product_identity(a: str | None, b: str | None) -> bool:
    aa = product_identity_token(a)
    bb = product_identity_token(b)
    return bool(aa and bb and aa == bb)


_CONTINUATION_HEADS = {
    "мл",
    "мг",
    "г",
    "шт",
    "таблетки",
    "таблетка",
    "капсулы",
    "капсула",
    "порошок",
    "аэрозоль",
    "спрей",
    "гель",
    "крем",
    "мазь",
    "капли",
    "сироп",
    "лиофилизат",
    "покрытые",
    "покрытый",
    "оболочкой",
    "суспензии",
    "суспензия",
    "раствора",
    "раствор",
    "приема",
    "внутрь",
    "флакон",
    "блистер",
    "пачка",
    "картонная",
    "дозированный",
}


def looks_like_continuation_fragment(value: str | None) -> bool:
    """
    Обнаружение названия продукта протокола, которое явно начинается в середине строки.

    Это происходит, когда строка физической таблицы PDF разделена разрывом страницы:
        первая страница содержит начало торгового названия без серий/цен,
        в то время как следующая страница содержит конец названия плюс серии/цены.
    """
    normalized = normalize_name(value)
    if not normalized:
        return False

    tokens = re.findall(r"[0-9a-zа-я]+", normalized)
    if not tokens:
        return False

    head = tokens[0]

    # Typical mid-sentence starts observed in multi-page protocol rows.
    if head in _CONTINUATION_HEADS:
        return True

    # A fragment may start with a dosage/unit or packaging continuation.
    if re.fullmatch(r"\d+(?:[.,]\d+)?", head):
        return True

    return False


def name_similarity(a: str | None, b: str | None) -> float:
    aa, bb = normalize_name(a), normalize_name(b)
    if not aa or not bb:
        return 0.0
    return max(WRatio(aa, bb), token_set_ratio(aa, bb)) / 100.0


def decimal_equal(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= Decimal("0.0001")


@dataclass(slots=True)
class CandidateScore:
    index: int
    score: float
    method: str
    evidence: list[str] = field(default_factory=list)
    has_strong_key: bool = False
    exact_quantity: bool = False
    exact_series: bool = False
    exact_identifier: bool = False
    name_similarity: float = 0.0
    source_line_number: int | None = None


class ProductMatcher:
    """
    Консервативный алгоритм сопоставления "один к одному".

    Приоритет отдается семантическим идентификаторам, а не позиции в строке:
        1. код продукта / GTIN;
        2. серия;
        3. нормализованное название;
        4. количество;
        5. номер строки только в качестве критерия разрешения неоднозначности.

    Если два кандидата остаются практически равными без точного идентификатора/серии,
    строка помечается как неоднозначная, а не как предполагаемая.
    """

    min_score = 0.72
    ambiguity_margin = 0.055

    def rank(self, anchor, candidates, *, hint=None, used=None) -> list[CandidateScore]:
        used = used or set()
        ranked = []
        for idx, candidate in enumerate(candidates):
            if idx in used:
                continue
            scored = self._score(anchor, candidate, idx, hint=hint)
            if scored is not None:
                ranked.append(scored)
        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked

    def choose(self, anchor, candidates, *, hint=None, used=None):
        ranked = self.rank(anchor, candidates, hint=hint, used=used)
        if not ranked or ranked[0].score < self.min_score:
            return "unmatched", None, ranked[:3]

        # Точная серия — это бизнес-ключ уровня партии. Если ровно один кандидат
        # соответствует серии UPD (или подсказки), выберирается он перед сравнением насыщенных
        # агрегированных оценок. Это критически важно, когда повторяющиеся строки имеют одинаковый
        # код продукта, название и количество.
        exact_series_candidates = [x for x in ranked if x.exact_series]
        if len(exact_series_candidates) == 1:
            candidate = exact_series_candidates[0]
            candidate_product = candidates[candidate.index]

            # Номер партии/серии НЕ является уникальным для всех лекарственных препаратов.
            # Он может идентифицировать партию только после установления идентичности продукта.
            # Поэтому точный номер серии сам по себе не может сопоставить несвязанные продукты.
            identity_ok = same_product_identity(
                getattr(anchor, "name", None),
                getattr(candidate_product, "name", None),
            )
            if not identity_ok and hint is not None:
                identity_ok = same_product_identity(
                    getattr(hint, "name", None),
                    getattr(candidate_product, "name", None),
                )

            continuation_ok = (
                looks_like_continuation_fragment(
                    getattr(candidate_product, "name", None)
                )
                and candidate.name_similarity >= 0.80
            )

            if (
                candidate.exact_identifier
                or identity_ok
                or candidate.name_similarity >= 0.985
                or continuation_ok
            ):
                if continuation_ok and "pdf_continuation_fragment" not in candidate.evidence:
                    candidate.evidence.append("pdf_continuation_fragment")
                return "matched", candidate, ranked[:3]

            # Продолжает искать нормальное имя/идентификатор вместо 
            # позволения совпадающему серийному номеру украсть эту строку протокола.
            ranked_without_weak_series = [
                x for x in ranked if x.index != candidate.index
            ]
            if (
                ranked_without_weak_series
                and ranked_without_weak_series[0].score >= self.min_score
                and ranked_without_weak_series[0].name_similarity >= 0.90
            ):
                return "matched", ranked_without_weak_series[0], ranked[:3]

            return "unmatched", None, ranked[:3]

        if len(exact_series_candidates) > 1:
            # Серийный номер может встречаться более одного раза в одном протоколе (даже для разных вариантов продукта). 
            # Перед сравнением нечетких названий используется точное количество в качестве дискриминатора на уровне партии.
            exact_quantity_candidates = [
                x for x in exact_series_candidates if x.exact_quantity
            ]
            if len(exact_quantity_candidates) == 1:
                quantity_winner = exact_quantity_candidates[0]
                if (
                    quantity_winner.exact_identifier
                    or quantity_winner.name_similarity >= 0.78
                ):
                    return "matched", quantity_winner, ranked[:3]

            # Если несколько строк имеют общие серию и количество, используется резервный вариант:
            # идентификатор/название продукта, а неоднозначность сохраняется, если она еще не определена.
            series_pool = (
                exact_quantity_candidates
                if len(exact_quantity_candidates) > 1
                else exact_series_candidates
            )

            by_name = sorted(
                series_pool,
                key=lambda x: x.name_similarity,
                reverse=True,
            )
            best_series = by_name[0]
            second_series = by_name[1]

            best_product = candidates[best_series.index]
            best_identity = same_product_identity(
                getattr(anchor, "name", None),
                getattr(best_product, "name", None),
            )
            if not best_identity and hint is not None:
                best_identity = same_product_identity(
                    getattr(hint, "name", None),
                    getattr(best_product, "name", None),
                )

            if (
                best_identity
                and best_series.name_similarity >= 0.78
                and best_series.name_similarity - second_series.name_similarity >= 0.05
            ):
                return "matched", best_series, ranked[:3]

            # Один и тот же номер партии для несвязанных продуктов никогда не должен 
            # использоваться в качестве единственного решающего сигнала.
            if best_series.name_similarity < 0.78:
                return "unmatched", None, ranked[:3]

            return "ambiguous", None, ranked[:3]

        best = ranked[0]
        if len(ranked) > 1:
            second = ranked[1]
            close = best.score - second.score < self.ambiguity_margin

            # Повторяющиеся строки для одного и того же продукта — обычное явление. Когда названия/коды
            # идентичны, но количество различается, количество является ключевым фактором для бизнеса
            # и должно превышать фактический номер строки.
            if close and best.exact_quantity != second.exact_quantity:
                # Показатель количества определяет, какая из повторяющихся строк бизнес-объекта относится к UPD.
                # Не позволяет насыщенности оценок или порядку строк источника скрывать этот сигнал.
                quantity_best = best if best.exact_quantity else second
                return "matched", quantity_best, ranked[:3]

            # Надежный ключ может идентифицировать семейство продуктов, но не конкретную
            # дублирующуюся партию. Если оставшиеся кандидаты по-прежнему одинаково правдоподобны,
            # не будем угадывать по номеру строки источника.
            if close:
                return "ambiguous", None, ranked[:3]

        return "matched", best, ranked[:3]

    def _score(self, anchor, candidate, index: int, *, hint=None):
        evidence = []
        score = 0.0
        strong = False
        exact_series_match = False
        exact_identifier_match = False

        anchor_code = normalize_product_code(getattr(anchor, "product_code", None))
        anchor_gtin = normalize_gtin(getattr(anchor, "gtin", None))
        candidate_code = normalize_product_code(getattr(candidate, "product_code", None))
        candidate_gtin = normalize_gtin(getattr(candidate, "gtin", None))

        if anchor_code and candidate_code and anchor_code == candidate_code:
            score += 0.62
            strong = True
            exact_identifier_match = True
            evidence.append("exact_product_code")
        elif anchor_code and candidate_code and anchor_code != candidate_code:
            score -= 0.08

        # В PDF-файлах/XML-архивах реестра штрихкод может отображаться либо как отдельное поле GTIN,
        # либо как общее поле кода продукта. GTIN-14 с ведущим нулем
        # эквивалентен соответствующему GTIN-13 для целей сопоставления.
        gtin_candidates = {x for x in (candidate_gtin, normalize_gtin(getattr(candidate, "product_code", None))) if x}
        anchor_code_gtin = normalize_gtin(getattr(anchor, "product_code", None))
        anchor_barcodes = {value for value in (anchor_gtin, anchor_code_gtin)
                           if len(value) in (13, 14)}
        candidate_barcodes = {value for value in gtin_candidates
                              if len(value) in (13, 14)}
        if anchor_barcodes & candidate_barcodes:
            score += 0.62
            strong = True
            exact_identifier_match = True
            evidence.append("exact_gtin" if anchor_gtin in candidate_barcodes else "product_code_as_gtin")

        anchor_series = normalize_series(getattr(anchor, "series", None))
        hint_series = normalize_series(getattr(hint, "series", None)) if hint else ""
        candidate_series = normalize_series(getattr(candidate, "series", None))

        if candidate_series and candidate_series in {anchor_series, hint_series} - {""}:
            score += 0.58
            strong = True
            exact_series_match = True
            evidence.append("exact_series")
        elif candidate_series and anchor_series and candidate_series != anchor_series:
            score -= 0.12

        sim_anchor = name_similarity(getattr(anchor, "name", None), getattr(candidate, "name", None))
        sim_hint = name_similarity(getattr(hint, "name", None), getattr(candidate, "name", None)) if hint else 0.0
        sim = max(sim_anchor, sim_hint)
        # Для кандидата достаточно почти идентичного нормализованного имени,
        # но повторяющиеся партии остаются неоднозначными, если их не разделяет другой сигнал.
        score += 0.75 * sim
        evidence.append(f"name={sim:.3f}")

        anchor_qty = getattr(anchor, "quantity", None)
        candidate_qty = getattr(candidate, "quantity", None)
        exact_quantity = decimal_equal(anchor_qty, candidate_qty)
        if exact_quantity:
            score += 0.08
            evidence.append("exact_quantity")

        anchor_line = getattr(anchor, "line_number", None)
        candidate_line = getattr(candidate, "line_number", None)
        if anchor_line is not None and candidate_line == anchor_line:
            score += 0.035
            evidence.append("same_line_number")

        # Для отбора кандидата необходимо как минимум наличие значимого сходства имен или точного ключа.
        if "product_code_as_gtin" in evidence and sim < 0.78:
            return None
        if not strong and sim < 0.78:
            return None

        if strong:
            if "exact_product_code" in evidence or "exact_gtin" in evidence or "product_code_as_gtin" in evidence:
                method = "identifier"
            else:
                method = "series"
        elif sim >= 0.96:
            method = "name_exactish"
        else:
            method = "name_fuzzy"

        return CandidateScore(
            index=index,
            score=max(0.0, min(1.0, score)),
            method=method,
            evidence=evidence,
            has_strong_key=strong,
            exact_quantity=exact_quantity,
            exact_series=exact_series_match,
            exact_identifier=exact_identifier_match,
            name_similarity=sim,
            source_line_number=getattr(candidate, "line_number", None),
        )
