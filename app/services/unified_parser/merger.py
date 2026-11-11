from typing import Any
from pathlib import Path
from datetime import date
from loguru import logger
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from app.services.upd_parser.parser import UPDParser
from app.services.registry_parser.parser import RegistryParser
from app.services.protocol_parser.parser import ProtocolParser
from app.services.unified_parser.matching import (
    ProductMatcher, 
    normalize_series, 
    normalize_product_code, 
    normalize_name,
    name_similarity,
    same_product_identity
)
from app.services.unified_parser.models import MatchDecision, MergeReport, MergeResult, MergeWarning, UnifiedDocument, UnifiedProduct


class UnifierDocumentBuilder:
    """
    UPD — это авторитетная структура строк. 
    Реестр и протокол являются необязательными источниками обогащения. 
    Ни одна строка UPD никогда не удаляется.
    """

    def __init__(self):
        self.upd_parser = UPDParser()
        self.registry_parser = RegistryParser()
        self.protocol_parser = ProtocolParser()
        self.matcher = ProductMatcher()

    def build(self, upd, registry=None, protocol=None) -> MergeResult:
        upd_doc = self._ensure_upd(upd)
        # registry_doc = self._ensure_registry(registry) if registry is not None else None
        # protocol_doc = self._ensure_protocol(protocol) if protocol is not None else None

        report = MergeReport()

        # Реестр и протокол — это дополнительные документы, служащие для обогащения данных. 
        # Некорректно сформированное, пустое или неподдерживаемое вложение не должно приводить к прерыванию.
        registry_doc = self._try_optional_document("registry", registry, report)
        protocol_doc = self._try_optional_document("protocol", protocol, report)

        self._document_level_warnings(upd_doc, registry_doc, protocol_doc, report)

        registry_positional = (
            self._build_indistinguishable_duplicate_map(
                upd_doc.products, registry_doc.products
            )
            if registry_doc is not None
            else {}
        )
        protocol_positional = (
            self._build_indistinguishable_duplicate_map(
                upd_doc.products, protocol_doc.products
            )
            if protocol_doc is not None
            else {}
        )

        protocol_order_only = {}
        if protocol_doc is not None and protocol_doc.source_format in ('xlsx', 'xml'):
            protocol_order_only = self._build_ordered_lot_group_map(
                upd_doc.products, protocol_doc.products,
                require_exact_code=(protocol_doc.source_format == 'xml'),
            )
            for line, src_index in protocol_order_only.items():
                protocol_positional.setdefault(line, src_index)

        registry_map = {}
        used_registry = set()

        if registry_doc is not None:
            for upd_product in upd_doc.products:
                positional_idx = registry_positional.get(upd_product.line_number)
                if positional_idx is not None and positional_idx not in used_registry:
                    source_product = registry_doc.products[positional_idx]
                    used_registry.add(positional_idx)
                    registry_map[upd_product.line_number] = source_product
                    report.registry_matches.append(
                        self._positional_duplicate_decision(
                            "registry", upd_product.line_number, source_product
                        )
                    )
                    continue

                excluded_registry = set(used_registry)
                if registry_doc.layout_source == "xls":
                    name_key = normalize_name(upd_product.name)
                    same_name_rows = [
                        (idx, product)
                        for idx, product in enumerate(registry_doc.products)
                        if normalize_name(product.name) == name_key
                    ]
                    for idx, product in enumerate(registry_doc.products):
                        if normalize_name(product.name) != name_key:
                            excluded_registry.add(idx)
                    if len(same_name_rows) > 1 and not upd_product.series:
                        quantities = [product.quantity for _, product in same_name_rows]
                        if (
                            upd_product.quantity is not None
                            and all(q is not None for q in quantities)
                            and sum(quantities) == upd_product.quantity
                        ):
                            report.registry_matches.append(MatchDecision(
                                source='registry', upd_line_number=upd_product.line_number,
                                status='ambiguous', source_line_number=None,
                                method='multiple_registry_lots',
                                evidence=['same_product_name', 'sum_of_lot_quantities_equals_upd'],
                            ))

                            message = (
                                "Одна строка УПД содержит несколько партий реестра: "
                                + ", ".join(
                                    f"{product.series} ({product.quantity})"
                                    for _, product in same_name_rows
                                )
                                + ". Нельзя назначить одну серию всей строке УПД."
                            )
                            report.warnings.append(MergeWarning(
                                code="registry_multiple_lots_in_upd_row",
                                upd_line_number=upd_product.line_number,
                                field="series",
                                message=message,
                            ))
                            logger.warning(message)
                            continue
                status, best, _ = self.matcher.choose(
                    upd_product,
                    registry_doc.products,
                    used=excluded_registry,
                )
                decision = self._decision('registry', upd_product.line_number, status, best)
                report.registry_matches.append(decision)
                if status == 'matched' and best is not None:
                    used_registry.add(best.index)
                    registry_map[upd_product.line_number] = registry_doc.products[best.index]

        protocol_map = {}
        used_protocol = set()

        if protocol_doc is not None:
            for upd_product in upd_doc.products:
                positional_idx = protocol_positional.get(upd_product.line_number)
                if positional_idx is not None and positional_idx not in used_protocol:
                    source_product = protocol_doc.products[positional_idx]
                    used_protocol.add(positional_idx)
                    protocol_map[upd_product.line_number] = source_product
                    if upd_product.line_number in protocol_order_only:
                        report.protocol_matches.append(MatchDecision(
                            source="protocol", upd_line_number=upd_product.line_number,
                            status="matched", source_line_number=source_product.line_number,
                            score=None, method="positional_order_assumed",
                            evidence=["same_product_name", "equal_group_cardinality", "source_order_assumption"] + (
                                ["exact_product_code"] if protocol_doc.source_format == 'xml' else []
                            ),
                        ))

                        message=(
                            "Серия из протокола назначена по порядку строк "
                            "одинакового товара; в УПД серия отсутствует и "
                            "независимого подтверждения соответствия партий нет"
                        )
                        report.warnings.append(MergeWarning(
                            code="series_assigned_by_order",
                            upd_line_number=upd_product.line_number,
                            field="series",
                            message=message,
                        ))
                        logger.warning(message)
                    else:
                        report.protocol_matches.append(
                            self._positional_duplicate_decision(
                                "protocol", upd_product.line_number, source_product
                            )
                        )
                    continue

                # Сопоставление позиций в PDF-документах с учетом цены. В регуляторных графах может быть указана оптовая цена с учетом НДС, 
                # тогда как цена за единицу в универсальном передаточном документе (УПД) приводится без НДС. Уникальная комбинация «цена + наименование» 
                # позволяет идентифицировать строку, даже если в УПД отсутствует информация о серии товара. Категорически не допускается определять 
                # партию товара исключительно на основании порядка следования строк или только по цене.
                if protocol_doc.source_format == "pdf":
                    price_idx = self._unique_protocol_wholesale_vat_match(
                        upd_product, protocol_doc.products, used_protocol
                    )
                    if price_idx is not None:
                        source_product = protocol_doc.products[price_idx]
                        used_protocol.add(price_idx)
                        protocol_map[upd_product.line_number] = source_product
                        report.protocol_matches.append(MatchDecision(
                            source="protocol",
                            upd_line_number=upd_product.line_number,
                            status="matched",
                            source_line_number=source_product.line_number,
                            score=None,
                            method="name_price_vat",
                            evidence=["same_product_identity", "name_similarity>=0.55", "unique_wholesale_vat_price"],
                        ))
                        continue

                reg_hint = registry_map.get(upd_product.line_number)
                status, best, _ = self.matcher.choose(upd_product, protocol_doc.products, hint=reg_hint, used=used_protocol)
                decision = self._decision('protocol', upd_product.line_number, status, best)
                report.protocol_matches.append(decision)
                if status == 'matched' and best is not None:
                    used_protocol.add(best.index)
                    protocol_map[upd_product.line_number] = protocol_doc.products[best.index]

        # Второй шанс получить строки реестра, используя уже найденный протокол в качестве подсказки.
        if registry_doc is not None and protocol_doc is not None:
            for i, upd_product in enumerate(upd_doc.products):
                if upd_product.line_number in registry_map:
                    continue
                protocol_hint = protocol_map.get(upd_product.line_number)
                if protocol_hint is None:
                    continue

                status, best, _ = self.matcher.choose(upd_product, registry_doc.products, hint=protocol_hint, used=used_registry)
                if status == 'matched' and best is not None:
                    used_registry.add(best.index) 
                    registry_map[upd_product.line_number] = registry_doc.products[best.index]
                    report.registry_matches[i] = self._decision('registry', upd_product.line_number, status, best)

        products = []
        for upd_product in upd_doc.products:
            reg = registry_map.get(upd_product.line_number)
            prot = protocol_map.get(upd_product.line_number)
            products.append(self._merge_product(upd_product, reg, prot, report))

        if registry_doc is not None:
            report.unmatched_registry_lines = [
                self._source_line(p, idx)
                for idx, p in enumerate(registry_doc.products)
                if idx not in used_registry
            ]

        if protocol_doc is not None:
            report.unmatched_protocol_lines = [
                self._source_line(p, idx)
                for idx, p in enumerate(protocol_doc.products)
                if idx not in used_protocol
            ]

        return MergeResult(
            document=UnifiedDocument(
                upd_number=upd_doc.document_number,
                upd_date=upd_doc.document_data,
                products=products
            ),
            report=report
        )

    @staticmethod
    def _compatible_strength_and_pack(upd_name, candidate_name):
        import re

        def strength(text):
            m = re.search(r"(\d+(?:[.,]\d+)?)\s*мг", text or "", re.I)
            return Decimal(m.group(1).replace(",", ".")) if m else None

        def pack_size(text):
            text = text or ""
            m = re.search(r"(\d+)\s*шт\.?\s*[-–]?\s*блистер\s*\((\d+)\)", text, re.I)
            if m:
                return int(m.group(1)) * int(m.group(2))
            m = re.search(r"№\s*(\d+)", text)
            if m:
                return int(m.group(1))
            m = re.search(r"(\d+)\s*шт\.?", text, re.I)
            return int(m.group(1)) if m else None

        a_strength, b_strength = strength(upd_name), strength(candidate_name)
        if a_strength is not None and b_strength is not None and a_strength != b_strength:
            return False
        a_pack, b_pack = pack_size(upd_name), pack_size(candidate_name)
        if a_pack is not None and b_pack is not None and a_pack != b_pack:
            return False
        return True

    @staticmethod
    def _unique_protocol_wholesale_vat_match(upd_product, candidates, used_indices):
        upd_price = getattr(upd_product, "price", None)
        vat_text = str(getattr(upd_product, "vat_rate", "") or "")
        if upd_price is None:
            return None
        import re
        match = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*%\s*", vat_text)
        if not match:
            return None
        try:
            multiplier = Decimal("1") + Decimal(match.group(1).replace(",", ".")) / 100
        except InvalidOperation:
            return None
        if multiplier <= 0:
            return None

        possible = []
        source_series = normalize_series(getattr(upd_product, "series", None))
        for idx, candidate in enumerate(candidates):
            if idx in used_indices or candidate.wholesale_price is None:
                continue
            if not same_product_identity(upd_product.name, candidate.name):
                continue
            if name_similarity(upd_product.name, candidate.name) < 0.55:
                continue
            # Сравнение дозировки и фактического количества таблеток в упаковке: у
            # одного и того же бренда могут быть разные варианты дозировки и размеры упаковок. Это также
            # позволяет сопоставить, например, «20 шт. — блистер (3)» и «№ 60».
            if not UnifierDocumentBuilder._compatible_strength_and_pack(
                upd_product.name, candidate.name
            ):
                continue
            candidate_series = normalize_series(candidate.series)
            if source_series and candidate_series and source_series != candidate_series:
                continue
            # Исходная цена (с учетом НДС) обычно округляется до копеек. 
            # Допуск учитывает округление, не допуская при этом смешивания упаковок с разными ценами.
            if abs(candidate.wholesale_price - upd_price * multiplier) <= Decimal("0.025"):
                possible.append(idx)
        return possible[0] if len(possible) == 1 else None

    @staticmethod
    def _build_ordered_lot_group_map(upd_products, source_products, require_exact_code=False):
        def identity(name):
            return normalize_name(name)

        upd_groups = defaultdict(list)
        src_groups = defaultdict(list)
        for i, p in enumerate(upd_products):
            upd_groups[identity(p.name)].append((i, p))
        for i, p in enumerate(source_products):
            src_groups[identity(p.name)].append((i, p))

        result = {}
        for name, upd_group in upd_groups.items():
            source_group = src_groups.get(name, [])
            if not (len(upd_group) > 1 and len(upd_group) == len(source_group)):
                continue
            if require_exact_code:
                upd_codes = {normalize_product_code(p.product_code) for _, p in upd_group}
                src_codes = {normalize_product_code(p.product_code) for _, p in source_group}
                if len(upd_codes) != 1 or upd_codes != src_codes or not next(iter(upd_codes)):
                    continue
            if any(getattr(p, 'series', None) for _, p in upd_group):
                continue
            if not all(getattr(p, 'series', None) for _, p in source_group):
                continue
            if any(b[0] != a[0] + 1 for a,b in zip(upd_group,upd_group[1:])):
                continue
            if any(b[0] != a[0] + 1 for a,b in zip(source_group,source_group[1:])):
                continue
            for (_, upd_p), (src_index, _) in zip(upd_group, source_group):
                result[upd_p.line_number] = src_index
        return result

    @classmethod
    def _build_indistinguishable_duplicate_map(cls, upd_products, source_products):
        """
        Позиционный резервный вариант для очень узкого случая:

            * UPD содержит N > 1 совершенно неразличимых строк бизнес-данных;
            * источник содержит ровно N строк с одинаковым нормализованным идентификатором;
            * все строки источника имеют одинаковую серию (или все не имеют серии).

        В этой ситуации ни одно поле бизнес-данных не может отличить строку A от строки B.
        Если кардинальность одинакова, сохраните порядок источника внутри дублирующейся группы.
        """
        from collections import defaultdict

        upd_groups = defaultdict(list)
        source_groups = defaultdict(list)

        for idx, product in enumerate(upd_products):
            fp = cls._duplicate_identity_fingerprint(product)
            if fp is not None:
                upd_groups[fp].append((idx, product))

        for idx, product in enumerate(source_products):
            fp = cls._duplicate_identity_fingerprint(product)
            if fp is not None:
                source_groups[fp].append((idx, product))

        mapping = {}

        for fp, upd_group in upd_groups.items():
            source_group = source_groups.get(fp, [])
            if len(upd_group) <= 1 or len(source_group) != len(upd_group):
                continue

            source_series = {
                normalize_series(getattr(product, "series", None))
                for _, product in source_group
            }
            # Различные непустые серии представляют собой различимые партии.
            non_empty_series = {value for value in source_series if value}
            if len(non_empty_series) > 1:
                continue

            # Стабильный порядок: сначала указан номер строки исходного кода, затем физический индекс.
            upd_sorted = sorted(
                upd_group,
                key=lambda pair: (
                    getattr(pair[1], "line_number", None) is None,
                    getattr(pair[1], "line_number", None) or pair[0] + 1,
                    pair[0],
                ),
            )
            source_sorted = sorted(
                source_group,
                key=lambda pair: (
                    getattr(pair[1], "line_number", None) is None,
                    getattr(pair[1], "line_number", None) or pair[0] + 1,
                    pair[0],
                ),
            )

            for (_, upd_product), (source_idx, _) in zip(upd_sorted, source_sorted):
                mapping[upd_product.line_number] = source_idx

        return mapping

    @staticmethod
    def _duplicate_identity_fingerprint(product):
        code = normalize_product_code(getattr(product, "product_code", None))
        name = normalize_name(getattr(product, "name", None))
        quantity = getattr(product, "quantity", None)

        if not name or quantity is None:
            return None

        # Необходимо иметь хотя бы один достаточно стабильный идентификатор продукта, помимо названия.
        if not code:
            return None

        return (
            code,
            name,
            str(quantity.normalize()) if hasattr(quantity, "normalize") else str(quantity),
        )

    @staticmethod
    def _positional_duplicate_decision(source, upd_line, source_product):
        return MatchDecision(
            source=source,
            upd_line_number=upd_line,
            status="matched",
            source_line_number=getattr(source_product, "line_number", None),
            score=1.0,
            method="positional_duplicate_group",
            evidence=[
                "indistinguishable_duplicate_group",
                "equal_group_cardinality",
                "stable_source_order",
            ],
        )

    def _merge_product(self, upd, reg, prot, report):
        series = self._pick_with_conflict(
            'series',
            upd.line_number,
            [
                ('registry', getattr(reg, 'series', None)),
                ('upd', getattr(upd, 'series', None)),
                ('protocol', getattr(prot, 'series', None))
            ],
            report,
            normalizer=normalize_series
        )

        manufacturer = self._pick_with_conflict(
            'manufacturer',
            upd.line_number,
            [
                ('registry', getattr(reg, 'manufacturer', None)),
                ('protocol', getattr(prot, 'manufacturer', None)),
            ],
            report
        )

        registry_exp = getattr(reg, 'expiration_date', None)
        upd_exp = getattr(upd, 'expiration_date', None)
        upd_exp_text = (
            upd_exp.strftime("%d.%m.%Y")
            if isinstance(upd_exp, date)
            else str(upd_exp) if upd_exp not in (None, "") else None
        )
        expiration = self._pick_with_conflict(
            'expiration_date',
            upd.line_number,
            [
                ('registry', registry_exp),
                ('upd', upd_exp_text),
                ('protocol', getattr(prot, 'expiration_date', None))
            ],
            report
        )

        return UnifiedProduct(
            line_number=upd.line_number,
            name=upd.name,
            product_code=upd.product_code,
            unit=upd.unit,
            okei=upd.okei,
            quantity=upd.quantity,
            price=upd.price,
            amount_without_vat=upd.amount_without_vat,
            vat_rate=upd.vat_rate,
            vat_amount=upd.vat_amount,
            amount_with_vat=upd.amount_with_vat,
            gtin=upd.gtin,

            series=series,
            manufacturer=manufacturer,
            manufacturer_actual_price_without_vat=getattr(
                prot, 'manufacturer_actual_price_without_vat', None
            ),
            manufacturer_regulated_price=getattr(
                prot, 'manufacturer_regulated_price', None
            ),
            wholesale_price=getattr(prot, 'wholesale_price', None),
            expiration_date=expiration,
            manufacturer_sale_date=getattr(prot, 'manufacturer_sale_date', None),
        )

    @staticmethod
    def _pick_with_conflict(field, line, values, report, normalizer=None):
        present = [(source, value) for source, value in values if value not in (None, '')]
        if not present:
            return None

        chosen_source, chosen = present[0]
        norm = normalizer or (lambda x: str(x).strip().casefold())
        chosen_norm = norm(chosen)

        conflicts = [
            (source, value)
            for source, value in present[1:]
            if norm(value) != chosen_norm
        ]
        if conflicts:
            message=(
                f"Конфликт поля {field}: выбрано {chosen_source}={chosen!r}; "
                + ", ".join(f"{s}={v!r}" for s, v in conflicts)
            )
            report.warnings.append(
                MergeWarning(
                    code='source_conflict',
                    upd_line_number=line,
                    field=field,
                    message=message
                )
            )
            logger.warning(message)
        return chosen

    @staticmethod
    def _decision(source, upd_line, status, best):
        if best is None:
            return MatchDecision(
                source=source,
                upd_line_number=upd_line,
                status=status
            )
        return MatchDecision(
            source=source,
            upd_line_number=upd_line,
            status=status,
            source_line_number=best.source_line_number,
            score=round(best.score, 4),
            method=best.method,
            evidence=best.evidence
        )

    @staticmethod
    def _source_line(product, index):
        return getattr(product, 'line_number', None) or (index + 1)

    @staticmethod
    def _document_level_warnings(upd, registry, protocol, report):
        upd_no = (upd.document_number or '').strip()
        if registry is not None:
            reg_no = (registry.invoice_number or '').strip()
            if upd_no and reg_no and upd_no != reg_no:
                message=f"УПД №{upd_no} и реестр №{reg_no} имеют разные номера"
                report.warnings.append(
                    MergeWarning(
                        code='document_number_mismatch',
                        message=message
                    )
                )
                logger.warning(message)

        if protocol is not None:
            basis_no = (protocol.basis_number or '').strip()
            # Номер протокола сам по себе не должен совпадать с номером UPD.
            if upd_no and basis_no and upd_no != basis_no:
                message=f"УПД №{upd_no} и основание протокола №{basis_no} различаются"
                report.warnings.append(
                    MergeWarning(
                        code='basis_number_mismatch',
                        message=message,
                    )
                )
                logger.warning(message)

    def _try_optional_document(self, kind, source, report):
        if source is None:
            return None

        parser = self._ensure_registry if kind == "registry" else self._ensure_protocol
        source_label = (
            str(source) if isinstance(source, (str, Path))
            else "<bytes>" if isinstance(source, bytes)
            else "<parsed document>"
        )
        try:
            doc = parser(source)
            if not doc.products:
                raise ValueError("Не обнаружены товарные строки")
            return doc
        except Exception as exc:
            message=(
                f"Документ {kind} '{source_label}' исключён из обработки: "
                f"{type(exc).__name__}: {exc}"
            )
            report.warnings.append(MergeWarning(
                code=f"{kind}_document_skipped",
                message=message,
            ))
            logger.warning(message)
            return None

    def _ensure_upd(self, value):
        if hasattr(value, 'products') and hasattr(value, 'document_number'):
            return value
        return self.upd_parser.parse(value)

    def _ensure_registry(self, value):
        if hasattr(value, 'products') and hasattr(value, 'source_layout'):
            return value
        return self.registry_parser.parse(value)

    def _ensure_protocol(self, value):
        if hasattr(value, 'products') and hasattr(value, 'source_format'):
            return value
        return self.protocol_parser.parse(value)
