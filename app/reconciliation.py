from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from datetime import date, datetime, time
import re
from typing import Callable

from app.document_matching import (
    DocumentIdentity,
    PENDING_DOCUMENT_FAMILIES,
    document_reconciliation_status,
    is_reconcilable_document,
    is_requested_document,
    parse_document,
)
from app.models import Discrepancy, LedgerRow, ReconciliationNotice, ReconciliationResult, ReconciliationSummary, SourceObservation


MISSING_IN_TAX = "Отсутствует в бухгалтерии"
EXTRA_IN_TAX = "Лишняя строка в бухгалтерии"
DUPLICATE_IN_TAX = "Дублирующая строка в бухгалтерии"
MISSING_ITEM_IN_TAX = "Строка товара отсутствует в документе БУХ"
EXTRA_ITEM_IN_TAX = "Лишняя строка товара в документе БУХ"
MISSING_BALANCE_IN_TAX = "Остаток отсутствует в бухгалтерии"
EXTRA_BALANCE_IN_TAX = "Лишний остаток в бухгалтерии"
DOCUMENT_QUANTITY_MISMATCH = "Различие движения количества"
DOCUMENT_AMOUNT_MISMATCH = "Различие стоимости движения"
BALANCE_QUANTITY_MISMATCH = "Различие остатков количества"
BALANCE_AMOUNT_MISMATCH = "Различие стоимости остатков"
BALANCE_UNCHECKED = "Остаток не проверен"
BUH_ONLY_DOCUMENT = "Документ только в бухгалтерии"
# Compatibility for callers which imported the previous broad category.
DATA_MISMATCH = DOCUMENT_QUANTITY_MISMATCH
AMBIGUOUS_MATCH = "Невозможность однозначного сопоставления"

RELOAD_DOCUMENT = "Проверить загрузку документа и правила сопоставления"
REMOVE_DOCUMENT = "Проверить соответствие и уникальность строки бухгалтерии"
CHECK_QUANTITY = "Проверить движение количества"
CHECK_AMOUNT = "Проверить правила учета стоимости"
CHECK_BALANCE = "Проверить остаток на границе периода"
CHECK_BUH_ONLY = "Проверить, должен ли тип документа существовать только в бухгалтерии"
MANUAL_CHECK = "Выполнить ручную проверку"

NUMBER_DATE_MATCH = "Тип, номер и дата документа"
SOURCE_ID_MATCH = "Идентификатор исходного документа"
DOCUMENT_CONTENT_MATCH = "Дата, товары и количество движения"
ISSUED_INVOICE_MATCH = "Выданный счёт-фактура УТ и номер реализации БУХ"
IMPORT_PARTY_MATCH = "Бухгалтерская партия и контрагент"
SNAPSHOT_MATCH = "Товар и дата остатка"


@dataclass(frozen=True)
class _DocumentGroup:
    key: tuple[str, ...]
    warehouse_key: tuple[str, str]
    organization_key: tuple[str, str]
    identity: DocumentIdentity
    rows: tuple[LedgerRow, ...]
    has_duplicate_rows: bool = False


@dataclass(frozen=True)
class _DocumentReconciliation:
    discrepancies: list[Discrepancy]
    pairs: list[tuple[_DocumentGroup, _DocumentGroup, str]]
    ambiguous_documents: int
    duplicate_documents: int
    missing_documents: int
    extra_documents: int
    buh_only_documents: int


def reconcile(
    trade_rows: list[LedgerRow],
    tax_exports: dict[str, list[LedgerRow]],
    period: str,
    amount_tolerance: Decimal = Decimal("0.01"),
    quantity_tolerance: Decimal = Decimal("0"),
    start_date: date | None = None,
    end_date: date | None = None,
    trade_candidate_rows: list[LedgerRow] | None = None,
    tax_candidate_exports: dict[str, list[LedgerRow]] | None = None,
    warehouse_mapping_errors: dict[str, dict[str, str]] | None = None,
) -> ReconciliationResult:
    discrepancies: list[Discrepancy] = []
    summaries: list[ReconciliationSummary] = []
    notice_counts: Counter[tuple[str, str]] = Counter()
    boundaries = _period_boundaries(period, start_date, end_date)

    if not tax_exports:
        discrepancies.append(
            Discrepancy(
                period=period,
                base="",
                document="",
                nomenclature="",
                opening_quantity=Decimal("0.00"),
                opening_amount=Decimal("0.00"),
                turnover_quantity=Decimal("0.00"),
                turnover_amount=Decimal("0.00"),
                closing_quantity=Decimal("0.00"),
                closing_amount=Decimal("0.00"),
                discrepancy_type=AMBIGUOUS_MATCH,
                recommendation="Не настроены базы бухгалтерии для сверки",
            )
        )
        return ReconciliationResult(discrepancies=discrepancies, summaries=[])

    original_trade_rows = trade_rows
    internal_source_groups = _group_documents(
        row for row in trade_rows if row.document and is_reconcilable_document(row.document, row.period)
    )
    internal_source_keys = _internal_transfer_keys(internal_source_groups, trade_rows)
    internal_source_fingerprints = {_source_document_fingerprint(row) for row in trade_rows
                                    if _row_group_key(row) in internal_source_keys}
    trade_rows = [row for row in trade_rows if not row.warehouse_mapping_error]
    trade_policy_counts = _document_policy_counts(trade_rows)

    for tax_base_name, tax_rows in tax_exports.items():
        base_mapping_errors = (warehouse_mapping_errors or {}).get(tax_base_name, {})
        base_trade_rows = [replace(row, warehouse_mapping_error=base_mapping_errors[row.warehouse_mapping_id])
                           if row.warehouse_mapping_id in base_mapping_errors else row
                           for row in original_trade_rows]
        trade_rows = [row for row in base_trade_rows if not row.warehouse_mapping_error]
        original_tax_rows = tax_rows
        internal_tax_keys = _internal_counterpart_keys(
            internal_source_groups, internal_source_keys, _group_documents(original_tax_rows))
        internal_tax_fingerprints = {_source_document_fingerprint(row) for row in original_tax_rows
                                    if _row_group_key(row) in internal_tax_keys}
        tax_rows = [row for row in tax_rows if not row.warehouse_mapping_error]
        mapping_limitations = [
            row
            for side, rows in (("trade", base_trade_rows), ("tax", original_tax_rows))
            for row in rows if row.warehouse_mapping_error
            and (not row.document or is_requested_document(row.document, row.period, row.document_type))
            and not (side == "trade" and _row_group_key(row) in internal_source_keys)
            and not (side == "tax" and _row_group_key(row) in internal_tax_keys)
        ]
        for row in mapping_limitations:
            notice_counts[(tax_base_name, row.warehouse_mapping_error)] += 1
        tax_policy_counts = _document_policy_counts(tax_rows)
        import_trade_rows: list[LedgerRow] = []
        import_tax_rows: list[LedgerRow] = []
        if "2019" in tax_base_name:
            import_mapping_ids = {
                row.warehouse_mapping_id for row in tax_rows
                if row.warehouse_code == "БД-000003" and row.warehouse_mapping_id
            }
            import_trade_rows = [
                row for row in trade_rows
                if _is_goods_receipt(row) and row.warehouse_mapping_id
                and (
                    row.warehouse_mapping_id in import_mapping_ids
                    if import_mapping_ids else _is_yes(row.is_import)
                )
            ]
            import_tax_rows = [
                row for row in tax_rows
                if _is_goods_receipt(row) and row.warehouse_code == "БД-000003"
            ]
        import_trade_ids = {id(row) for row in import_trade_rows}
        import_tax_ids = {id(row) for row in import_tax_rows}
        ordinary_trade_rows = [row for row in trade_rows if id(row) not in import_trade_ids
                               and (not row.document or is_requested_document(row.document, row.period, row.document_type))]
        ordinary_tax_rows = [row for row in tax_rows if id(row) not in import_tax_ids
                             and (not row.document or is_requested_document(row.document, row.period, row.document_type))]
        all_trade_documents = _group_documents(
            row for row in ordinary_trade_rows
            if row.document and is_reconcilable_document(row.document, row.period)
        )
        internal_trade_keys = _internal_transfer_keys(all_trade_documents, ordinary_trade_rows) | internal_source_keys
        trade_documents = {
            key: group for key, group in all_trade_documents.items() if key not in internal_trade_keys
        }
        tax_documents = _group_documents(
            row for row in ordinary_tax_rows
            if row.document and is_reconcilable_document(row.document, row.period)
            and _row_group_key(row) not in internal_tax_keys
        )
        document_result = _reconcile_documents(
            trade_documents,
            tax_documents,
            tax_base_name,
            amount_tolerance,
            quantity_tolerance,
            trade_candidates=_group_documents(row for row in trade_candidate_rows
                if _source_document_fingerprint(row) not in internal_source_fingerprints)
                if trade_candidate_rows is not None else None,
            tax_candidates=_group_documents(row for row in tax_candidate_exports.get(tax_base_name, [])
                if _source_document_fingerprint(row) not in internal_tax_fingerprints)
                if tax_candidate_exports is not None else None,
        )
        import_discrepancies, import_matches, import_ambiguous, import_missing, import_extra = (
            _reconcile_import_receipts(
                import_trade_rows, import_tax_rows, tax_base_name,
                amount_tolerance, quantity_tolerance,
            )
        )
        snapshot_discrepancies = _reconcile_control_points(
            trade_rows, tax_rows, tax_base_name, boundaries, quantity_tolerance, notice_counts,
        ) if boundaries else _unbounded_stock_discrepancies(trade_rows, tax_rows, tax_base_name)
        # A limitation of the stock comparison is not a document to repair.
        # Keep its scope visible once in the summary without inventing zeroes.
        for item in snapshot_discrepancies:
            if item.discrepancy_type not in {BALANCE_QUANTITY_MISMATCH, BALANCE_AMOUNT_MISMATCH}:
                notice_counts[(tax_base_name, item.difference_summary.split(';')[0])] += 1
        snapshot_discrepancies = [item for item in snapshot_discrepancies
            if item.discrepancy_type in {BALANCE_QUANTITY_MISMATCH, BALANCE_AMOUNT_MISMATCH}]
        base_discrepancies = [
            *document_result.discrepancies, *import_discrepancies, *snapshot_discrepancies,
            *_pending_document_discrepancies(trade_rows, tax_rows, tax_base_name),
        ]
        discrepancies.extend(base_discrepancies)
        match_counts = Counter(method for _, _, method in document_result.pairs)
        discrepancy_counts = Counter(item.discrepancy_type for item in base_discrepancies)
        summaries.append(
            ReconciliationSummary(
                base=tax_base_name,
                trade_documents=_document_count(original_trade_rows),
                tax_documents=_document_count(original_tax_rows),
                matched_documents=len({(*_organization_key(t.rows[0]), t.identity.family, t.identity.number,
                                       t.identity.timestamp) for t, _, _ in document_result.pairs}),
                trade_document_groups=len(_group_documents(r for r in original_trade_rows if r.document)),
                tax_document_groups=len(_group_documents(r for r in original_tax_rows if r.document)),
                matched_document_groups=len(document_result.pairs),
                trade_item_rows=sum(bool(r.document) for r in original_trade_rows),
                tax_item_rows=sum(bool(r.document) for r in original_tax_rows),
                unverified_documents=len({(bool(d.trade_row), *_organization_key(d.trade_row or d.tax_row),
                                          (d.trade_row or d.tax_row).document)
                    for d in base_discrepancies if d.document_existence == "unknown"
                    and (d.trade_row or d.tax_row).document}),
                manual_item_rows=sum(requires_manual_review(d) for d in base_discrepancies),
                matched_import_groups=import_matches,
                matched_by_number_date=match_counts[NUMBER_DATE_MATCH],
                matched_by_content=match_counts[DOCUMENT_CONTENT_MATCH],
                ambiguous_documents=document_result.ambiguous_documents + import_ambiguous,
                duplicate_documents=document_result.duplicate_documents,
                missing_documents=document_result.missing_documents + import_missing,
                extra_documents=document_result.extra_documents + import_extra,
                buh_only_documents=document_result.buh_only_documents,
                ignored_internal_transfers=len(internal_trade_keys),
                excluded_trade_documents=trade_policy_counts["excluded"],
                excluded_tax_documents=tax_policy_counts["excluded"],
                pending_trade_documents=trade_policy_counts["pending"],
                pending_tax_documents=tax_policy_counts["pending"],
                document_quantity_differences=discrepancy_counts[DOCUMENT_QUANTITY_MISMATCH],
                document_amount_differences=discrepancy_counts[DOCUMENT_AMOUNT_MISMATCH],
                balance_quantity_differences=discrepancy_counts[BALANCE_QUANTITY_MISMATCH],
                balance_amount_differences=discrepancy_counts[BALANCE_AMOUNT_MISMATCH],
            )
        )

    return ReconciliationResult(
        discrepancies=_observe_source_boundaries(discrepancies, boundaries), summaries=summaries,
        notices=[ReconciliationNotice(base, reason, count)
                 for (base, reason), count in sorted(notice_counts.items())],
    )


def requires_manual_review(item: Discrepancy) -> bool:
    """One routing rule shared by the manual row statistic and XLSX renderer."""
    if item.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH:
        trade, tax = item.trade_row, item.tax_row
        return not (item.document_existence == "found" and trade and tax
                    and trade.turnover_quantity * tax.turnover_quantity >= 0
                    and abs(tax.turnover_quantity) > abs(trade.turnover_quantity))
    if item.discrepancy_type in {MISSING_IN_TAX, EXTRA_IN_TAX}:
        return False
    return item.discrepancy_type not in {
        BALANCE_QUANTITY_MISMATCH, BALANCE_AMOUNT_MISMATCH, DOCUMENT_AMOUNT_MISMATCH,
    }


def _is_goods_receipt(row: LedgerRow) -> bool:
    return bool(row.document) and parse_document(row.document, row.period).family == "goods_receipt"


def _pending_document_discrepancies(
    trade_rows: list[LedgerRow], tax_rows: list[LedgerRow], tax_base_name: str,
) -> list[Discrepancy]:
    counterpart_families = {
        "assembly": {"assembly"},
        "requirement_pending": {"write_off", "free_transfer_pending"},
        "free_transfer_pending": {"requirement_pending"},
    }
    result: list[Discrepancy] = []
    for rows, other_rows, is_trade in ((trade_rows, tax_rows, True), (tax_rows, trade_rows, False)):
        candidate_index: dict[tuple[object, ...], set[str]] = defaultdict(set)
        for other in other_rows:
            if not other.document:
                continue
            other_identity = parse_document(other.document, other.period)
            candidate_index[(
                other_identity.family, other_identity.timestamp[:10],
                *_warehouse_key(other), *_item_key(other),
            )].add(other.document)
        for row in rows:
            family = parse_document(row.document, row.period).family if row.document else ""
            if row.document and document_reconciliation_status(row.document, row.period) == "unknown":
                if not is_requested_document(row.document, row.period, row.document_type):
                    continue
                result.append(_from_rows(
                    row if is_trade else None, row if not is_trade else None,
                    tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK, "",
                    "Неизвестный вид или формат документа; требуется ручная проверка",
                ))
                continue
            if family not in PENDING_DOCUMENT_FAMILIES or is_reconcilable_document(row.document, row.period):
                continue
            identity = parse_document(row.document, row.period)
            candidates = sorted({document
                for candidate_family in counterpart_families[family]
                for document in candidate_index.get((
                    candidate_family, identity.timestamp[:10],
                    *_warehouse_key(row), *_item_key(row),
                ), ())})
            detail = "; ".join(candidates[:3]) if candidates else "не найдены"
            result.append(_from_rows(
                row if is_trade else None, row if not is_trade else None,
                tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK, "",
                f"Для вида документа нет утверждённого ключа; кандидаты: {detail}",
            ))
    return result


def _is_yes(value: str) -> bool:
    return value.strip().casefold() in {"да", "true", "1"}


def _units_available(row: LedgerRow) -> bool:
    # Missing optional descriptions do not change the exported quantity.
    # Never infer a package conversion or ignore an explicit group conflict.
    return (not row.package or bool(row.package_coefficient)) and not row.aggregation_conflict


def _organization_not_exported(row: LedgerRow) -> bool:
    return (not row.organization and not row.organization_inn
            and not {"organization", "organization_inn"} & row.present_fields)


def _organizations_compatible(trade: LedgerRow, tax: LedgerRow) -> bool:
    return (_organization_key(trade) == _organization_key(tax)
            or _organization_not_exported(trade)
            or _organization_not_exported(tax))


def _resolved_import_party(row: LedgerRow, side: str) -> str | None:
    if side == "УТ":
        return row.party.strip() or None
    # Real 1C exports use both slash directions. A plain invoice number alone
    # does not prove a party; require a separator or another explicit source.
    waybill_parts = re.split(r"[/\\]", row.party_waybill, maxsplit=1)
    if len(waybill_parts) == 1 and not row.party.strip() and not row.party_comment.strip():
        return None
    candidates = [value.strip() for value in (
        row.party, row.party_comment, waybill_parts[0],
    ) if value.strip()]
    keys = {_warehouse_name_key(value).replace("ё", "е") for value in candidates}
    if len(keys) != 1:
        return None
    return candidates[0]


def _import_candidate_key(row: LedgerRow, side: str) -> tuple[str, ...] | None:
    party = _resolved_import_party(row, side)
    if not row.warehouse_mapping_id or not party or not row.counterparty:
        return None
    return (
        row.warehouse_mapping_id,
        _warehouse_name_key(party).replace("ё", "е"),
        _warehouse_name_key(row.counterparty).replace("ё", "е"),
    )


def _import_key(row: LedgerRow, side: str) -> tuple[str, ...] | None:
    if not _units_available(row) or (side == "УТ" and not row.supplier_order):
        return None
    return _import_candidate_key(row, side)


def _reconcile_import_receipts(
    trade_rows: list[LedgerRow],
    tax_rows: list[LedgerRow],
    tax_base_name: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
) -> tuple[list[Discrepancy], int, int, int, int]:
    trade_groups: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    tax_groups: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    discrepancies: list[Discrepancy] = []
    incomplete_documents: set[tuple[str, str]] = set()
    candidate_organizations: dict[tuple[str, ...], set[str]] = defaultdict(set)
    candidate_rows: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for rows, side in ((trade_rows, "УТ"), (tax_rows, "БУХ2019")):
        for row in rows:
            key = _import_candidate_key(row, side)
            if key is not None:
                candidate_organizations[key].add(row.organization_inn)
                candidate_rows[key].append(row)
    repeated_import_keys = {
        _import_key(row, side)
        for rows, side in ((trade_rows, "УТ"), (tax_rows, "БУХ2019"))
        for row, count in Counter(rows).items() if count > 1
    }
    repeated_import_keys.discard(None)
    for rows, groups, side in (
        (trade_rows, trade_groups, "УТ"),
        (tax_rows, tax_groups, "БУХ2019"),
    ):
        for row in rows:
            key = _import_key(row, side)
            if key is None:
                incomplete_documents.add((side, row.document))
                discrepancies.append(_from_rows(
                    row if side == "УТ" else None,
                    row if side != "УТ" else None,
                    tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK, IMPORT_PARTY_MATCH,
                    f"Неполный или конфликтующий ключ импорта в {side}: партия, заказ строки, контрагент или склад",
                ))
            else:
                groups[key].append(replace(row, party=_resolved_import_party(row, side)))

    matched = missing = extra = 0
    ambiguous = len(incomplete_documents)
    for key in sorted(set(trade_groups) | set(tax_groups)):
        trade_group = trade_groups.get(key, [])
        tax_group = tax_groups.get(key, [])
        # Include ineligible source rows too: filtering a missing unit/order must
        # never make a mixed-organization candidate appear safe to aggregate.
        organizations = candidate_organizations[key]
        known_organizations = organizations - {""}
        unknown_organization = any(not row.organization_inn and not _organization_not_exported(row)
                                   for row in candidate_rows[key])
        if trade_group and tax_group and (unknown_organization or len(known_organizations) != 1):
            ambiguous += len({(*_organization_key(row), row.document)
                              for row in (*trade_group, *tax_group)})
            for group, is_trade in ((trade_group, True), (tax_group, False)):
                for row in group:
                    detail = "Организация импорта неизвестна или ИНН организаций группы различаются; вся группа требует ручной проверки"
                    if row.supplier_order:
                        detail += f"; заказ поставщику: {row.supplier_order}"
                    discrepancies.append(_from_rows(
                        row if is_trade else None, row if not is_trade else None,
                        tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK, IMPORT_PARTY_MATCH, detail,
                    ))
            continue
        if key in repeated_import_keys:
            ambiguous += len({row.document for row in (*trade_group, *tax_group)})
            discrepancies.extend(
                _from_rows(row, None, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                           IMPORT_PARTY_MATCH, "Повтор одинаковой импортной товарной строки в выгрузке")
                for row in trade_group
            )
            discrepancies.extend(
                _from_rows(None, row, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                           IMPORT_PARTY_MATCH, "Повтор одинаковой импортной товарной строки в выгрузке")
                for row in tax_group
            )
            continue
        conflicting_counterparty = (
            not tax_group and any(other[:2] == key[:2] for other in tax_groups)
            or not trade_group and any(other[:2] == key[:2] for other in trade_groups)
        )
        if conflicting_counterparty:
            ambiguous += len({row.document for row in (*trade_group, *tax_group)})
            for row, is_trade in [(r, True) for r in trade_group] + [(r, False) for r in tax_group]:
                discrepancies.append(_from_rows(
                    row if is_trade else None, row if not is_trade else None,
                    tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK, IMPORT_PARTY_MATCH,
                    "Есть импортное поступление с той же партией и складом, но другим контрагентом",
                ))
            continue
        if not tax_group and any(side == "БУХ2019" for side, _ in incomplete_documents):
            ambiguous += len({row.document for row in trade_group})
            discrepancies.extend(
                _from_rows(row, None, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                           IMPORT_PARTY_MATCH, "В БУХ2019 есть поступление с неполным или конфликтующим ключом")
                for row in trade_group
            )
            continue
        if not tax_group:
            missing += len({row.document for row in trade_group})
            discrepancies.extend(
                _from_rows(row, None, tax_base_name, MISSING_IN_TAX, RELOAD_DOCUMENT, IMPORT_PARTY_MATCH,
                           "Импортное движение не найдено; наличие карточки документа не проверено")
                for row in trade_group
            )
            continue
        if not trade_group and any(side == "УТ" for side, _ in incomplete_documents):
            ambiguous += len({row.document for row in tax_group})
            discrepancies.extend(
                _from_rows(None, row, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                           IMPORT_PARTY_MATCH, "В УТ есть поступление с неполным ключом")
                for row in tax_group
            )
            continue
        if not trade_group:
            extra += len({row.document for row in tax_group})
            discrepancies.extend(
                _from_rows(None, row, tax_base_name, EXTRA_IN_TAX, REMOVE_DOCUMENT, IMPORT_PARTY_MATCH,
                           "Импортное движение не найдено; наличие карточки документа не проверено")
                for row in tax_group
            )
            continue
        if len({row.document for row in tax_group}) != 1:
            ambiguous += len({row.document for row in (*trade_group, *tax_group)})
            discrepancies.extend(
                _from_rows(row, None, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                           IMPORT_PARTY_MATCH, "Несколько ПТУ БУХ2019 для одной партии и контрагента")
                for row in trade_group
            )
            discrepancies.extend(
                _from_rows(None, row, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                           IMPORT_PARTY_MATCH, "Несколько ПТУ БУХ2019 для одной партии и контрагента")
                for row in tax_group
            )
            continue
        matched += 1
        matched_items = _reconcile_rows(
            _aggregate_document_rows(trade_group),
            _aggregate_document_rows(tax_group),
            tax_base_name, amount_tolerance, quantity_tolerance,
            IMPORT_PARTY_MATCH, key_function=_item_key, comparison_scope="document",
        )
        discrepancies.extend(replace(item, document_existence="found",
            trade_document_refs=_group_document_refs(trade_group),
            tax_document_refs=_group_document_refs(tax_group)) for item in matched_items)
    return discrepancies, matched, ambiguous, missing, extra


def _reconcile_documents(
    trade_source: dict[tuple[str, ...], _DocumentGroup],
    tax_source: dict[tuple[str, ...], _DocumentGroup],
    tax_base_name: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
    trade_candidates: dict[tuple[str, ...], _DocumentGroup] | None = None,
    tax_candidates: dict[tuple[str, ...], _DocumentGroup] | None = None,
) -> _DocumentReconciliation:
    trade_groups = dict(trade_source)
    tax_groups = dict(tax_source)
    discrepancies: list[Discrepancy] = []
    pairs: list[tuple[_DocumentGroup, _DocumentGroup, str]] = []
    ambiguous_documents = 0
    duplicate_documents = 0
    quarantined_sale_keys: set[tuple[object, ...]] = set()

    uncertain_transfer_keys = {
        _number_date_key(group)
        for group in trade_groups.values()
        if group.identity.family == "transfer"
        and any((not row.sender_warehouse_code or not row.receiver_warehouse_code)
                and row.transfer_scope != "external"
                and not (row.sender_accounting_warehouse and row.receiver_accounting_warehouse
                         and _warehouse_name_key(row.sender_accounting_warehouse)
                         != _warehouse_name_key(row.receiver_accounting_warehouse))
                for row in group.rows)
    }
    for groups, side in ((trade_groups, "trade"), (tax_groups, "tax")):
        for key, group in list(groups.items()):
            if group.identity.family != "transfer" or _number_date_key(group) not in uncertain_transfer_keys:
                continue
            groups.pop(key)
            ambiguous_documents += 1
            discrepancies.extend(
                _from_rows(row if side == "trade" else None,
                           row if side == "tax" else None,
                           tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK, "",
                           "Не подтверждены коды складов отправителя и получателя для перемещения")
                for row in group.rows
            )

    repeated_match_keys = {
        _sale_invoice_key(group, side) if group.identity.family == "sale" else _number_date_key(group)
        for groups, side in ((trade_groups, "trade"), (tax_groups, "tax"))
        for group in groups.values() if group.has_duplicate_rows
    }
    repeated_match_keys.discard(None)
    for groups, side in ((trade_groups, "trade"), (tax_groups, "tax")):
        for key, group in list(groups.items()):
            business_key = _sale_invoice_key(group, side) if group.identity.family == "sale" else _number_date_key(group)
            if not group.has_duplicate_rows and business_key not in repeated_match_keys:
                continue
            groups.pop(key)
            ambiguous_documents += 1
            discrepancies.extend(
                _from_rows(row if side == "trade" else None,
                           row if side == "tax" else None,
                           tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK, "",
                           "Повтор одинаковой товарной строки в выгрузке документа")
                for row in group.rows
            )

    for groups, side in ((trade_groups, "trade"), (tax_groups, "tax")):
        invoice_index: dict[tuple[object, ...], list[tuple[str, ...]]] = defaultdict(list)
        for group_key, group in groups.items():
            invoice_key = _sale_invoice_key(group, side)
            if invoice_key is not None:
                invoice_index[(*invoice_key[:2], invoice_key[-1])].append(group_key)
        for invoice_key, group_keys in invoice_index.items():
            if len(group_keys) < 2:
                continue
            quarantined_sale_keys.add(invoice_key)
            ambiguous_documents += len(group_keys)
            for group_key in group_keys:
                group = groups.pop(group_key)
                discrepancies.extend(
                    _from_rows(row if side == "trade" else None,
                               row if side == "tax" else None,
                               tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                               ISSUED_INVOICE_MATCH,
                               "Номер счёта-фактуры повторяется в нескольких реализациях периода")
                    for row in group.rows
                )

    for key_function, match_method, ambiguous_duplicates in (
        (lambda group, side: _source_id_key(group, side), SOURCE_ID_MATCH, False),
        (lambda group, side: _sale_invoice_key(group, side), ISSUED_INVOICE_MATCH, False),
        (lambda group, side: _number_date_key(group), NUMBER_DATE_MATCH, False),
    ):
        matched, ambiguous, duplicates = _consume_group_matches(
            trade_groups,
            tax_groups,
            key_function,
            match_method,
            tax_base_name,
            discrepancies,
            ambiguous_duplicates=ambiguous_duplicates,
        )
        pairs.extend(matched)
        ambiguous_documents += ambiguous
        duplicate_documents += duplicates

    for trade_group, tax_group, match_method in pairs:
        discrepancies.extend(
            _compare_document_rows(
                trade_group,
                tax_group,
                tax_base_name,
                amount_tolerance,
                quantity_tolerance,
                match_method,
            )
        )

    # Unmatched movements route by side; counterpart card existence stays unknown.
    missing_documents = extra_documents = 0
    buh_only_documents = 0
    candidate_indexes = {
        "trade": _document_candidate_index(trade_candidates if trade_candidates is not None else trade_source, "trade"),
        "tax": _document_candidate_index(tax_candidates if tax_candidates is not None else tax_source, "tax"),
    }
    candidate_group_pools = {
        "trade": trade_candidates if trade_candidates is not None else trade_source,
        "tax": tax_candidates if tax_candidates is not None else tax_source,
    }
    candidate_group_indexes = {
        side: _index_groups(pool, lambda item, source_side=side: _document_candidate_key(item, source_side))
        for side, pool in candidate_group_pools.items()
    }
    for groups, side in ((trade_groups, "trade"), (tax_groups, "tax")):
        for group in groups.values():
            candidates = candidate_indexes["tax" if side == "trade" else "trade"].get(
                _document_candidate_key(group, side), ())
            other_side = "tax" if side == "trade" else "trade"
            pool = candidate_group_pools[other_side]
            candidate_groups = [pool[key] for key in candidate_group_indexes[other_side].get(
                _document_candidate_key(group, side), ())]
            related_candidates = []
            if not candidate_groups and _matching_family(group.identity.family) == "requirement":
                items = {_item_key(row) for row in group.rows}
                related_candidates = [candidate for candidate in pool.values()
                    if _matching_family(candidate.identity.family) == "requirement"
                    and candidate.warehouse_key == group.warehouse_key
                    and candidate.identity.timestamp[:10] == group.identity.timestamp[:10]
                    and items.intersection(_item_key(row) for row in candidate.rows)]
                candidates = tuple(sorted({ref for candidate in related_candidates
                                           for ref in _group_document_refs(candidate.rows)}))
            if not candidates and group.identity.family == "sale":
                # A sale with an unavailable invoice key can be the counterpart;
                # neither quantity nor a guessed invoice number selects it.
                related_candidates = [candidate for candidate in pool.values()
                    if candidate.identity.family == "sale"
                    and _document_candidate_key(candidate, other_side) is None
                    and candidate.warehouse_key == group.warehouse_key
                    and candidate.identity.timestamp[:10] == group.identity.timestamp[:10]]
                candidates = tuple(sorted({ref for candidate in related_candidates
                                           for ref in _group_document_refs(candidate.rows)}))
            reason = _unmatched_document_reason(group, side, tax_base_name, candidate_groups)
            if related_candidates:
                reason += "; возможные документы требуют проверки ключа связи: " + "; ".join(candidates)
            if any(row.aggregation_conflict for row in group.rows):
                reason += "; в товарных строках группы противоречат друг другу единицы, артикулы или реквизиты перемещения"
            elif any(not _units_available(row) for row in group.rows):
                reason += "; не подтверждена единица количества"
            unmatched = (not candidates and _document_candidate_key(group, side) is not None
                         and all(_units_available(row) and not row.aggregation_conflict for row in group.rows))
            if unmatched:
                reason = f"Движение не найдено в {tax_base_name if side == 'trade' else 'УТ'} за выбранный период; " + reason
                discrepancy_type = MISSING_IN_TAX if side == "trade" else EXTRA_IN_TAX
                recommendation = RELOAD_DOCUMENT if side == "trade" else REMOVE_DOCUMENT
                missing_documents += int(side == "trade")
                extra_documents += int(side == "tax")
            else:
                discrepancy_type, recommendation = AMBIGUOUS_MATCH, MANUAL_CHECK
                ambiguous_documents += 1
            for row in group.rows:
                item = _from_rows(
                    row if side == "trade" else None, row if side == "tax" else None,
                    tax_base_name, discrepancy_type, recommendation, "", reason,
                )
                discrepancies.append(replace(
                    item, tax_document_refs=candidates if side == "trade" else (),
                    trade_document_refs=candidates if side == "tax" else (),
                ))

    return _DocumentReconciliation(
        discrepancies=discrepancies,
        pairs=pairs,
        ambiguous_documents=ambiguous_documents,
        duplicate_documents=duplicate_documents,
        missing_documents=missing_documents,
        extra_documents=extra_documents,
        buh_only_documents=buh_only_documents,
    )


def _consume_group_matches(
    trade_groups: dict[tuple[str, ...], _DocumentGroup],
    tax_groups: dict[tuple[str, ...], _DocumentGroup],
    key_function: Callable[[_DocumentGroup, str], tuple[object, ...] | None],
    match_method: str,
    tax_base_name: str,
    discrepancies: list[Discrepancy],
    ambiguous_duplicates: bool = False,
) -> tuple[list[tuple[_DocumentGroup, _DocumentGroup, str]], int, int]:
    trade_index = _index_groups(trade_groups, lambda group: key_function(group, "trade"))
    tax_index = _index_groups(tax_groups, lambda group: key_function(group, "tax"))
    pairs: list[tuple[_DocumentGroup, _DocumentGroup, str]] = []
    ambiguous_documents = 0
    duplicate_documents = 0

    for match_key in sorted(set(trade_index) & set(tax_index), key=str):
        trade_keys = trade_index[match_key]
        tax_keys = tax_index[match_key]
        if len(trade_keys) == len(tax_keys) == 1:
            # A unique existing key can match the older UT contract, which does
            # not export organization metadata. Explicit conflicts still block
            # it; multiple organization candidates take the ambiguity branch.
            if not _organizations_compatible(trade_groups[trade_keys[0]].rows[0],
                                             tax_groups[tax_keys[0]].rows[0]):
                continue
            trade_group = trade_groups.pop(trade_keys[0])
            tax_group = tax_groups.pop(tax_keys[0])
            pairs.append((trade_group, tax_group, match_method))
            continue

        trade_ambiguous = [trade_groups.pop(key) for key in trade_keys]
        tax_ambiguous = [tax_groups.pop(key) for key in tax_keys]
        if ambiguous_duplicates:
            ambiguous_documents += len(trade_ambiguous) + len(tax_ambiguous)
            for group in trade_ambiguous:
                for row in group.rows:
                    discrepancies.append(
                        _from_rows(
                            row,
                            None,
                            tax_base_name,
                            AMBIGUOUS_MATCH,
                            MANUAL_CHECK,
                            match_method,
                            "Несколько документов имеют одинаковые дату, товары и количество движения",
                        )
                    )
            for group in tax_ambiguous:
                for row in group.rows:
                    discrepancies.append(
                        _from_rows(
                            None,
                            row,
                            tax_base_name,
                            AMBIGUOUS_MATCH,
                            MANUAL_CHECK,
                            match_method,
                            "Несколько документов имеют одинаковые дату, товары и количество движения",
                        )
                    )
        else:
            tax_is_duplicate = (
                len(tax_ambiguous) > 1
                and len({group.organization_key for group in tax_ambiguous}) == 1
                and len({group.identity.family for group in tax_ambiguous}) == 1
            )
            ambiguous_documents += len(trade_ambiguous)
            duplicate_documents += len(tax_ambiguous) if tax_is_duplicate else 0
            if not tax_is_duplicate:
                ambiguous_documents += len(tax_ambiguous)

            for group in trade_ambiguous:
                for row in group.rows:
                    discrepancies.append(
                        _from_rows(
                            row,
                            None,
                            tax_base_name,
                            AMBIGUOUS_MATCH,
                            MANUAL_CHECK,
                            match_method,
                            "Документ УТ имеет несколько кандидатов сопоставления",
                        )
                    )
            for group in tax_ambiguous:
                for row in group.rows:
                    discrepancies.append(
                        _from_rows(
                            None,
                            row,
                            tax_base_name,
                            DUPLICATE_IN_TAX if tax_is_duplicate else AMBIGUOUS_MATCH,
                            MANUAL_CHECK,
                            match_method,
                            (
                                "Повторяющийся идентификатор документа в бухгалтерии"
                                if tax_is_duplicate
                                else "Документ бухгалтерии имеет несколько кандидатов сопоставления"
                            ),
                        )
                    )
    return pairs, ambiguous_documents, duplicate_documents


def _compare_document_rows(
    trade_group: _DocumentGroup,
    tax_group: _DocumentGroup,
    tax_base_name: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
    match_method: str,
) -> list[Discrepancy]:
    items = _reconcile_rows(
        list(trade_group.rows),
        list(tax_group.rows),
        tax_base_name,
        amount_tolerance,
        quantity_tolerance,
        match_method,
        key_function=_item_key,
        comparison_scope="document",
    )

    return [replace(item, document_existence="found",
                    trade_document_refs=_group_document_refs(trade_group.rows),
                    tax_document_refs=_group_document_refs(tax_group.rows)) for item in items]


def _reconcile_snapshot_rows(
    trade_rows: list[LedgerRow],
    tax_rows: list[LedgerRow],
    tax_base_name: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
    entity_key: Callable[[LedgerRow], tuple[str, ...]] | None = None,
) -> list[Discrepancy]:
    entity_key = entity_key or _stock_entity_resolver(trade_rows, tax_rows)
    return _reconcile_rows(
        trade_rows,
        tax_rows,
        tax_base_name,
        amount_tolerance,
        quantity_tolerance,
        SNAPSHOT_MATCH,
        key_function=lambda row: (*entity_key(row), row.balance_point, row.period),
        comparison_scope="snapshot",
    )


def _reconcile_rows(
    trade_rows: list[LedgerRow],
    tax_rows: list[LedgerRow],
    tax_base_name: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
    match_method: str,
    key_function: Callable[[LedgerRow], tuple[str, ...]] | None = None,
    comparison_scope: str = "snapshot",
) -> list[Discrepancy]:
    discrepancies: list[Discrepancy] = []
    key_function = key_function or (lambda row: row.match_key)
    raw_trade_index = _index_rows(trade_rows, key_function)
    raw_tax_index = _index_rows(tax_rows, key_function)
    duplicate_trade_keys = {key for key, rows in raw_trade_index.items() if len(rows) > 1}
    duplicate_tax_keys = {key for key, rows in raw_tax_index.items() if len(rows) > 1}

    entity = "товарная строка в документе" if comparison_scope == "document" else "строка остатка"
    for key in sorted(duplicate_trade_keys):
        for row in raw_trade_index[key]:
            discrepancies.append(
                _from_rows(
                    row,
                    None,
                    tax_base_name,
                    AMBIGUOUS_MATCH,
                    MANUAL_CHECK,
                    match_method,
                    f"Повторяющаяся {entity} УТ",
                )
            )
    for key in sorted(duplicate_tax_keys):
        for row in raw_tax_index[key]:
            discrepancies.append(
                _from_rows(
                    None,
                    row,
                    tax_base_name,
                    DUPLICATE_IN_TAX,
                    MANUAL_CHECK,
                    match_method,
                    f"Повторяющаяся {entity} бухгалтерии",
                )
            )

    trade_index = {key: rows[0] for key, rows in raw_trade_index.items() if key not in duplicate_trade_keys}
    tax_index = {key: rows[0] for key, rows in raw_tax_index.items() if key not in duplicate_tax_keys}

    for key, trade_row in trade_index.items():
        if key in duplicate_tax_keys:
            discrepancies.append(_from_rows(
                trade_row, None, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                match_method, f"Повторяющаяся {entity} бухгалтерии; строка УТ не сопоставлена",
            ))
            continue
        tax_row = tax_index.get(key)
        if tax_row is None:
            if comparison_scope == "snapshot" and (trade_row.organization_inn or trade_row.organization):
                discrepancies.append(_from_rows(
                    trade_row, None, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                    match_method, "Остаток не сопоставлен по организации; проверить соответствие организаций",
                ))
                continue
            if not _units_available(trade_row):
                discrepancies.append(_from_rows(
                    trade_row, None, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                    match_method, "Не подтверждена единица количества УТ",
                ))
                continue
            discrepancy_type = MISSING_ITEM_IN_TAX if comparison_scope == "document" else MISSING_BALANCE_IN_TAX
            description = (
                "В соответствующем документе нет товарной строки"
                if comparison_scope == "document"
                else "В бухгалтерии нет остатка по товару на контрольную дату"
            )
            discrepancies.append(
                _from_rows(
                    trade_row,
                    None,
                    tax_base_name,
                    discrepancy_type,
                    RELOAD_DOCUMENT if comparison_scope == "document" else CHECK_BALANCE,
                    match_method,
                    description,
                )
            )
            continue
        if (
            trade_row.organization_inn
            and tax_row.organization_inn
            and trade_row.organization_inn != tax_row.organization_inn
        ):
            discrepancies.append(
                _from_rows(
                    trade_row,
                    tax_row,
                    tax_base_name,
                    AMBIGUOUS_MATCH,
                    MANUAL_CHECK,
                    match_method,
                    "Строки относятся к разным организациям",
                )
            )
            continue
        if trade_row.aggregation_conflict or tax_row.aggregation_conflict:
            discrepancies.append(
                _from_rows(
                    trade_row, tax_row, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                    match_method, "В группе документов смешаны единицы, упаковки, коэффициенты, артикулы или реквизиты перемещения",
                )
            )
            continue
        if trade_row.article and tax_row.article and trade_row.article.casefold() != tax_row.article.casefold():
            discrepancies.append(
                _from_rows(
                    trade_row, tax_row, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                    match_method, "У совпавшего кода товара различаются артикулы УТ и БУХ",
                )
            )
            continue
        if (
            not _units_available(trade_row) or not _units_available(tax_row)
            or trade_row.unit and tax_row.unit
            and _warehouse_name_key(trade_row.unit) != _warehouse_name_key(tax_row.unit)
            or trade_row.package and tax_row.package
            and _warehouse_name_key(trade_row.package) != _warehouse_name_key(tax_row.package)
            or trade_row.package_coefficient and tax_row.package_coefficient
            and trade_row.package_coefficient != tax_row.package_coefficient
        ):
            discrepancies.append(
                _from_rows(
                    trade_row, tax_row, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                    match_method, "Не подтверждена сопоставимость единиц количества УТ и БУХ",
                )
            )
            continue
        discrepancies.extend(
            _value_discrepancies(
                trade_row,
                tax_row,
                tax_base_name,
                match_method,
                comparison_scope,
                amount_tolerance,
                quantity_tolerance,
            )
        )

    for key, tax_row in tax_index.items():
        if key in duplicate_trade_keys:
            discrepancies.append(_from_rows(
                None, tax_row, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                match_method, f"Повторяющаяся {entity} УТ; строка бухгалтерии не сопоставлена",
            ))
            continue
        if key in duplicate_tax_keys:
            continue
        if key not in trade_index:
            if comparison_scope == "snapshot" and (tax_row.organization_inn or tax_row.organization):
                discrepancies.append(_from_rows(
                    None, tax_row, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                    match_method, "Остаток не сопоставлен по организации; проверить соответствие организаций",
                ))
                continue
            if not _units_available(tax_row):
                discrepancies.append(_from_rows(
                    None, tax_row, tax_base_name, AMBIGUOUS_MATCH, MANUAL_CHECK,
                    match_method, "Не подтверждена единица количества бухгалтерии",
                ))
                continue
            discrepancy_type = EXTRA_ITEM_IN_TAX if comparison_scope == "document" else EXTRA_BALANCE_IN_TAX
            description = (
                "В соответствующем документе УТ нет товарной строки"
                if comparison_scope == "document"
                else "В УТ нет остатка по товару на контрольную дату"
            )
            discrepancies.append(
                _from_rows(
                    None,
                    tax_row,
                    tax_base_name,
                    discrepancy_type,
                    REMOVE_DOCUMENT if comparison_scope == "document" else CHECK_BALANCE,
                    match_method,
                    description,
                )
            )
    return discrepancies


def _group_documents(rows) -> dict[tuple[str, ...], _DocumentGroup]:
    grouped: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in rows:
        warehouse_key = _warehouse_key(row)
        organization_key = _organization_key(row)
        grouped[(*warehouse_key, *organization_key, row.document)].append(row)

    return {
        key: _DocumentGroup(
            key=key,
            warehouse_key=_warehouse_key(group_rows[0]),
            organization_key=_organization_key(group_rows[0]),
            identity=parse_document(group_rows[0].document, group_rows[0].period),
            rows=tuple(group_rows if len(set(group_rows)) != len(group_rows)
                       else _aggregate_document_rows(group_rows)),
            has_duplicate_rows=len(set(group_rows)) != len(group_rows),
        )
        for key, group_rows in grouped.items()
    }


def _document_policy_counts(rows: list[LedgerRow]) -> Counter[str]:
    unique_documents = {
        (*_warehouse_key(row), *_organization_key(row), row.document, row.period)
        for row in rows
        if row.document
    }
    counts: Counter[str] = Counter()
    for *_, document, period in unique_documents:
        counts[document_reconciliation_status(document, period)] += 1
    return counts


def _aggregate_document_rows(rows: list[LedgerRow]) -> list[LedgerRow]:
    grouped: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in rows:
        grouped[(*_organization_key(row), *_item_key(row))].append(row)

    result: list[LedgerRow] = []
    for _, group_rows in sorted(grouped.items()):
        group_rows = sorted(group_rows, key=lambda r: (r.document, r.period, r.supplier_order,
                            r.organization, r.warehouse_code, str(r.turnover_quantity)))
        first = group_rows[0]
        if len(group_rows) == 1:
            result.append(first)
            continue
        documents = _group_document_refs(group_rows)
        dimensions = {
            (row.unit, row.package, row.package_coefficient, row.article.casefold(),
             (row.transfer_scope, row.sender_accounting_warehouse, row.receiver_accounting_warehouse,
              row.sender_warehouse_code, row.receiver_warehouse_code)
             if parse_document(row.document, row.period).family == "transfer" else ())
            for row in group_rows
        }
        result.append(
            replace(
                first,
                is_aggregated=True,
                turnover_quantity=sum((row.turnover_quantity for row in group_rows), Decimal("0")),
                turnover_amount=sum((row.turnover_amount for row in group_rows), Decimal("0")),
                related_documents=documents if len(documents) > 1 else first.related_documents,
                aggregation_conflict=len(dimensions) > 1 or any(row.aggregation_conflict for row in group_rows),
            )
        )
    return result


def _index_groups(
    groups: dict[tuple[str, ...], _DocumentGroup],
    key_function: Callable[[_DocumentGroup], tuple[object, ...] | None],
) -> dict[tuple[object, ...], list[tuple[str, ...]]]:
    result: dict[tuple[object, ...], list[tuple[str, ...]]] = defaultdict(list)
    for group_key, group in groups.items():
        match_key = key_function(group)
        if match_key is not None:
            result[match_key].append(group_key)
    return result


def _number_date_key(group: _DocumentGroup) -> tuple[object, ...] | None:
    if group.identity.family == "sale":
        return None
    return (
        *group.warehouse_key, group.identity.family,
        group.identity.number.casefold(), group.identity.timestamp[:10],
    )


def _source_id_key(group: _DocumentGroup, side: str) -> tuple[object, ...] | None:
    identifiers = {row.source_document_id for row in group.rows if row.source_document_id} if side == 'tax' else set()
    identifiers = identifiers or {row.document_id for row in group.rows if row.document_id}
    if len(identifiers) != 1:
        return None
    return (*group.warehouse_key, _matching_family(group.identity.family),
            group.identity.timestamp[:10], next(iter(identifiers)).casefold())


def _matching_family(family: str) -> str:
    return "requirement" if family in {"requirement_pending", "write_off", "free_transfer_pending"} else family


def _row_group_key(row: LedgerRow) -> tuple[str, ...]:
    return (*_warehouse_key(row), *_organization_key(row), row.document)


def _source_document_fingerprint(row: LedgerRow) -> tuple[str, ...]:
    return (row.document, *_organization_key(row), row.warehouse_code or row.warehouse)


def _sale_invoice_key(group: _DocumentGroup, side: str) -> tuple[object, ...] | None:
    if group.identity.family != "sale":
        return None
    if side == "trade":
        numbers = {row.issued_invoice_number for row in group.rows}
        if len(numbers) != 1:
            return None
        raw_number = next(iter(numbers))
    else:
        raw_number = group.identity.number
    match = re.fullmatch(r"(?:ДЖ)?0*([1-9][0-9]*)", raw_number.strip(), re.IGNORECASE)
    if match is None:
        return None
    return (*group.warehouse_key, group.identity.timestamp[:10], match.group(1))


def _content_key(group: _DocumentGroup, side: str = "") -> tuple[object, ...] | None:
    if group.identity.family != "sale":
        return None
    content = tuple(sorted((_item_key(row), row.turnover_quantity) for row in group.rows))
    return (*group.warehouse_key, group.identity.family, group.identity.timestamp, content)


def _internal_transfer_keys(
    groups: dict[tuple[str, ...], _DocumentGroup],
    all_trade_rows: list[LedgerRow],
) -> set[tuple[str, ...]]:
    raw_rows_by_group: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in all_trade_rows:
        if not row.document:
            continue
        raw_rows_by_group[
            (*_warehouse_key(row), *_organization_key(row), row.document)
        ].append(row)

    result: set[tuple[str, ...]] = set()
    for key, group in groups.items():
        if group.identity.family != "transfer":
            continue
        raw_rows = raw_rows_by_group.get(key, list(group.rows))
        if all(row.transfer_scope == "internal" or
               (row.sender_accounting_warehouse and row.receiver_accounting_warehouse
                and _warehouse_name_key(row.sender_accounting_warehouse)
                == _warehouse_name_key(row.receiver_accounting_warehouse)) for row in raw_rows):
            result.add(key)
            continue
    return result


def _internal_counterpart_keys(trade_groups, internal_keys, tax_groups):
    """Drop only a uniquely identified counterpart of an excluded transfer."""
    trade_index = _index_groups(trade_groups, _number_date_key)
    tax_index = _index_groups(tax_groups, _number_date_key)
    result = set()
    for tax_key, group in tax_groups.items():
        if group.identity.family != "transfer":
            continue
        business_key = _number_date_key(group)
        candidates = trade_index.get(business_key, [])
        if len(tax_index.get(business_key, [])) != 1 or not candidates:
            continue
        if all(key in internal_keys and _organizations_compatible(trade_groups[key].rows[0], group.rows[0])
               for key in candidates):
            result.add(tax_key)
    return result


def _aggregate_snapshot_rows(rows: list[LedgerRow]) -> list[LedgerRow]:
    grouped: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in rows:
        grouped[row.match_key].append(row)

    result: list[LedgerRow] = []
    for group_rows in grouped.values():
        physical_warehouses = {
            row.warehouse_code or _warehouse_name_key(row.warehouse)
            for row in group_rows
        }
        if (
            len(group_rows) == 1
            or not group_rows[0].warehouse_mapping_id
            or len(physical_warehouses) != len(group_rows)
        ):
            result.extend(group_rows)
            continue
        first = group_rows[0]
        result.append(
            replace(
                first,
                opening_quantity=sum((row.opening_quantity for row in group_rows), Decimal("0")),
                is_aggregated=True,
                opening_amount=sum((row.opening_amount for row in group_rows), Decimal("0")),
                turnover_quantity=sum((row.turnover_quantity for row in group_rows), Decimal("0")),
                turnover_amount=sum((row.turnover_amount for row in group_rows), Decimal("0")),
                closing_quantity=sum((row.closing_quantity for row in group_rows), Decimal("0")),
                closing_amount=sum((row.closing_amount for row in group_rows), Decimal("0")),
                warehouse="; ".join(dict.fromkeys(row.warehouse for row in group_rows if row.warehouse)),
                warehouse_code="; ".join(dict.fromkeys(row.warehouse_code for row in group_rows if row.warehouse_code)),
                aggregation_conflict=(
                    len({(row.unit, row.package, row.package_coefficient, row.article.casefold())
                         for row in group_rows}) > 1
                    or any(row.aggregation_conflict for row in group_rows)
                ),
            )
        )
    return result


def _index_rows(
    rows: list[LedgerRow],
    key_function: Callable[[LedgerRow], tuple[str, ...]],
) -> dict[tuple[str, ...], list[LedgerRow]]:
    result: dict[tuple[str, ...], list[LedgerRow]] = defaultdict(list)
    for row in rows:
        result[key_function(row)].append(row)
    return result


def _item_key(row: LedgerRow) -> tuple[str, ...]:
    return (
        "code" if row.nomenclature_code else ("article" if row.article else "name"),
        row.nomenclature_code or row.article or row.nomenclature,
    )


def _warehouse_key(row: LedgerRow) -> tuple[str, str]:
    if row.warehouse_mapping_id:
        return "mapping", row.warehouse_mapping_id
    if row.warehouse_code:
        return "code", row.warehouse_code
    return "name", _warehouse_name_key(row.warehouse)


def _warehouse_name_key(value: str) -> str:
    return " ".join(value.replace("–", "-").replace("—", "-").strip().casefold().split())


def _organization_key(row: LedgerRow) -> tuple[str, str]:
    if row.organization_inn:
        return "inn", row.organization_inn
    if row.organization:
        return "name", " ".join(row.organization.strip().casefold().split())
    return "none", ""


def _value_discrepancies(
    trade_row: LedgerRow,
    tax_row: LedgerRow,
    tax_base_name: str,
    match_method: str,
    comparison_scope: str,
    amount_tolerance: Decimal,
    quantity_tolerance: Decimal,
) -> list[Discrepancy]:
    # Суммы продолжают нормализоваться для совместимости с контрактом 1С, но по
    # требованиям встречи 11.09.2026 не участвуют в пользовательской сверке.
    _ = amount_tolerance
    if comparison_scope == "document":
        profiles = (
            (DOCUMENT_QUANTITY_MISMATCH, CHECK_QUANTITY, (("оборот количества", "turnover_quantity"),), quantity_tolerance),
        )
    else:
        point = trade_row.balance_point or tax_row.balance_point
        fields = (("начальный остаток количества", "opening_quantity"),
                  ("конечный остаток количества", "closing_quantity"))
        if point:
            fields = tuple((label, field) for label, field in fields if field.startswith(point))
        profiles = (
            (
                BALANCE_QUANTITY_MISMATCH,
                CHECK_BALANCE,
                fields,
                quantity_tolerance,
            ),
        )

    discrepancies: list[Discrepancy] = []
    for discrepancy_type, recommendation, fields, tolerance in profiles:
        differences = []
        for label, field in fields:
            trade_value = getattr(trade_row, field)
            tax_value = getattr(tax_row, field)
            if abs(trade_value - tax_value) > tolerance:
                if comparison_scope == "document" and trade_value < 0 and tax_value < 0:
                    shown_trade, shown_tax = -trade_value, -tax_value
                else:
                    shown_trade, shown_tax = trade_value, tax_value
                differences.append(
                    f"{label}: УТ {shown_trade}, БУХ {shown_tax}, разница {shown_tax - shown_trade}"
                )
        if differences:
            discrepancies.append(
                _from_rows(
                    trade_row,
                    tax_row,
                    tax_base_name,
                    discrepancy_type,
                    recommendation,
                    match_method,
                    "; ".join(differences),
                )
            )
    return discrepancies


def _from_rows(
    trade_row: LedgerRow | None,
    tax_row: LedgerRow | None,
    base: str,
    discrepancy_type: str,
    recommendation: str,
    match_method: str,
    difference_summary: str = "",
) -> Discrepancy:
    row = tax_row or trade_row
    if row is None:
        raise ValueError("Для расхождения нужна хотя бы одна исходная строка")
    return Discrepancy(
        period=row.period,
        base=base,
        document=row.document,
        nomenclature=row.nomenclature,
        opening_quantity=row.opening_quantity,
        opening_amount=row.opening_amount,
        turnover_quantity=row.turnover_quantity,
        turnover_amount=row.turnover_amount,
        closing_quantity=row.closing_quantity,
        closing_amount=row.closing_amount,
        discrepancy_type=discrepancy_type,
        recommendation=recommendation,
        nomenclature_code=row.nomenclature_code,
        article=row.article,
        warehouse=row.warehouse,
        warehouse_code=row.warehouse_code,
        warehouse_mapping_id=row.warehouse_mapping_id,
        organization=row.organization,
        organization_inn=row.organization_inn,
        trade_row=trade_row,
        tax_row=tax_row,
        match_method=match_method,
        difference_summary=difference_summary,
        document_existence="found" if trade_row and tax_row and trade_row.document and tax_row.document else "unknown",
        trade_document_refs=_group_document_refs([trade_row]) if trade_row else (),
        tax_document_refs=_group_document_refs([tax_row]) if tax_row else (),
        trade_source=_source_observation(trade_row),
        tax_source=_source_observation(tax_row),
    )


def _group_document_refs(rows) -> tuple[str, ...]:
    return tuple(sorted({document for row in rows
        for document in (row.related_documents or (row.document,)) if document}))


def _source_observation(row: LedgerRow | None) -> SourceObservation | None:
    if row is None or row.is_aggregated or row.balance_point:
        return None
    return SourceObservation(row.period, row.opening_quantity, row.closing_quantity)


def _observe_source_boundaries(discrepancies, boundaries):
    def observe(source):
        if source is None or boundaries is None:
            return source
        stamp = _row_timestamp(source)
        begin, end = boundaries
        point = ("opening" if stamp == datetime.combine(begin, time.min) else
                 "closing" if stamp == datetime.combine(end, time(23, 59, 59)) else "")
        return replace(source, observed_point=point, control_date=stamp.date() if point else None)

    return [replace(item, trade_source=observe(item.trade_source), tax_source=observe(item.tax_source))
            for item in discrepancies]


def _period_boundaries(period: str, begin: date | None, end: date | None):
    if begin is not None and end is not None:
        return begin, end
    dates = re.findall(r"\b\d{2}\.\d{2}\.\d{4}\b", period)
    if dates:
        parsed = [datetime.strptime(value, "%d.%m.%Y").date() for value in dates]
        return parsed[0], parsed[-1]
    return None


def _row_timestamp(row: LedgerRow):
    for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(row.period, pattern)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(row.period)
    except ValueError:
        return None


def _stock_entity(row):
    return (*_warehouse_key(row), *_organization_key(row), *_item_key(row))


def _stock_entity_resolver(trade_rows, tax_rows):
    """Scope an older export only when one organization candidate exists.

    This resolves matching keys, not source fields. Known organizations remain
    distinct, and an unspecified source cannot be allocated to multiple ones.
    """
    organizations = defaultdict(set)
    for row in (*trade_rows, *tax_rows):
        if not _organization_not_exported(row):
            organizations[(*_warehouse_key(row), *_item_key(row))].add(_organization_key(row))

    def key(row):
        organization = _organization_key(row)
        candidates = organizations[(*_warehouse_key(row), *_item_key(row))]
        if _organization_not_exported(row) and len(candidates) == 1:
            organization = next(iter(candidates))
        return (*_warehouse_key(row), *organization, *_item_key(row))

    return key


def _reconcile_control_points(trade_rows, tax_rows, base, boundaries, tolerance, notice_counts=None):
    """Compare explicit period boundaries, never add running balances together.

    The current export marks midnight opening and 23:59:59 closing. A closing
    row may carry a document; a midnight row's closing field alone is not proof
    of an interval ending. No missing source row establishes a zero balance.
    """
    begin, end = boundaries
    checkpoints = {"opening": datetime.combine(begin, time.min),
                   "closing": datetime.combine(end, time(23, 59, 59))}
    entity_key = _stock_entity_resolver(trade_rows, tax_rows)
    originals = {}; projected = {"trade": [], "tax": []}
    for side, rows in (("trade", trade_rows), ("tax", tax_rows)):
        for row in rows:
            stamp = _row_timestamp(row)
            point = next((p for p, at in checkpoints.items() if stamp == at), "")
            if not row.document or point:
                originals.setdefault(entity_key(row), {}).setdefault(side, []).append(row)

    # Unknown units affect this stock entity only. Retain each original row:
    # projecting it first would overwrite the original opening/closing values.
    output = []
    unverified_entities = set()
    for entity, sources in originals.items():
        rows = [row for side_rows in sources.values() for row in side_rows]
        dimensions = [{_warehouse_name_key(getattr(row, field)) for row in rows if getattr(row, field)}
                      for field in ('unit', 'package', 'package_coefficient')]
        if all(_units_available(row) for row in rows) and all(len(values) <= 1 for values in dimensions):
            continue
        unverified_entities.add(entity)
        for side, side_rows in sources.items():
            output.extend(_from_rows(
                row if side == "trade" else None, row if side == "tax" else None,
                base, AMBIGUOUS_MATCH, MANUAL_CHECK, SNAPSHOT_MATCH,
                "Не подтверждена сопоставимость единиц количества; показаны исходные остатки без межбазовой разницы",
            ) for row in side_rows)
    originals = {entity: sources for entity, sources in originals.items() if entity not in unverified_entities}
    for side, rows in (("trade", trade_rows), ("tax", tax_rows)):
        for row in rows:
            stamp = _row_timestamp(row)
            point = next((p for p, at in checkpoints.items() if stamp == at), "")
            if point and entity_key(row) not in unverified_entities:
                quantity = getattr(row, point + "_quantity")
                amount = getattr(row, point + "_amount")
                projected[side].append(replace(
                    row, document="", related_documents=_group_document_refs([row]),
                    period=checkpoints[point].strftime("%d.%m.%Y %H:%M:%S"), balance_point=point,
                    opening_quantity=quantity, closing_quantity=quantity,
                    opening_amount=amount, closing_amount=amount,
                    turnover_quantity=Decimal("0"), turnover_amount=Decimal("0"),
                ))
    point_rows_index = defaultdict(list)
    for side, rows in projected.items():
        for row in rows:
            point_rows_index[(side, entity_key(row), row.balance_point)].append(row)
    incomplete = {}
    for entity, sources in originals.items():
        for side, rows in sources.items():
            physical = {r.warehouse_code or r.warehouse for r in rows}
            for point in checkpoints:
                point_rows = point_rows_index[(side, entity, point)]
                supplied_codes = {r.warehouse_code or r.warehouse for r in point_rows}
                if supplied_codes and supplied_codes != physical:
                    incomplete[(side, entity, point)] = point_rows
    known = {side: _aggregate_snapshot_rows([r for r in rows
             if (side, entity_key(r), r.balance_point) not in incomplete])
             for side, rows in projected.items()}
    compared = _reconcile_snapshot_rows(known['trade'], known['tax'], base, Decimal('0'), tolerance,
                                        entity_key=entity_key)
    for item in compared:
        point = (item.trade_row or item.tax_row).balance_point
        label = "Начальный" if point == "opening" else "Конечный"
        unknown = item.trade_row is None or item.tax_row is None
        entity = entity_key(item.trade_row or item.tax_row)
        partial_trade = incomplete.get(('trade', entity, point), [])
        partial_tax = incomplete.get(('tax', entity, point), [])
        raw_partial = "; ".join(f"{side} {r.warehouse_code}: {getattr(r, point + '_quantity')}"
            for side, rows in (("УТ", partial_trade), ("БУХ", partial_tax)) for r in rows)
        reason = label + " остаток не проверен: нет подтверждённой строки одной базы"
        if unknown and item.difference_summary:
            reason += "; " + item.difference_summary
        if raw_partial:
            reason += "; неполный набор складских строк: " + raw_partial
        output.append(replace(
            item, discrepancy_type=BALANCE_UNCHECKED if unknown else item.discrepancy_type,
            difference_summary=reason if unknown else item.difference_summary,
            trade_row=item.trade_row or (partial_trade[0] if partial_trade else None),
            tax_row=item.tax_row or (partial_tax[0] if partial_tax else None),
            balance_point=point, balance_control_date=checkpoints[point].date(),
            balance_trade_quantity=getattr(item.trade_row, point + '_quantity') if item.trade_row else None,
            balance_tax_quantity=getattr(item.tax_row, point + '_quantity') if item.tax_row else None,
        ))
    supplied = {(side, entity_key(row), row.balance_point)
                for side, rows in known.items() for row in rows}
    for entity, sources in originals.items():
        for point, at in checkpoints.items():
            if any((side, entity, point) in supplied for side in ('trade', 'tax')):
                continue
            label = "Начальный" if point == "opening" else "Конечный"
            if notice_counts is not None:
                notice_counts[(base, f"{label} остаток не проверен: граничные строки отсутствуют")] += 1
    return output


def _unbounded_stock_discrepancies(trade_rows, tax_rows, base):
    result = []
    for side, rows in (("trade", trade_rows), ("tax", tax_rows)):
        for row in rows:
            if row.document:
                continue
            reason = ("Конечный остаток не проверен: границы периода не заданы; "
                      f"исходные поля: начальный {row.opening_quantity}, конечный {row.closing_quantity}")
            result.append(_from_rows(
                row if side == "trade" else None, row if side == "tax" else None,
                base, BALANCE_UNCHECKED, MANUAL_CHECK, SNAPSHOT_MATCH, reason))
    return result


def _document_count(rows: list[LedgerRow]) -> int:
    return len({(*_organization_key(r), *parse_document(r.document, r.period).strong_key)
                for r in rows if r.document})


def _document_candidate_key(group, side: str):
    if group.identity.family == "sale":
        invoice_key = _sale_invoice_key(group, side)
        number = str(invoice_key[-1]) if invoice_key else ""
    else:
        number = group.identity.number.casefold()
    return (_matching_family(group.identity.family), number) if number else None


def _unmatched_document_reason(group, side, base, other_groups):
    """Explain the failed identity check without claiming a card is missing."""
    target = base if side == "trade" else "УТ"
    key = _document_candidate_key(group, side)
    if group.identity.family == "sale" and side == "trade" and key is None:
        numbers = {row.issued_invoice_number.strip() for row in group.rows}
        if numbers == {""}:
            reason = "В УТ пустой номер выданного счёта-фактуры; номер реализации БУХ для поиска не определён"
        else:
            reason = "Номер выданного счёта-фактуры УТ неоднозначен или имеет неподдерживаемый формат: " + ", ".join(sorted(numbers))
        return reason + "; наличие карточки документа не проверено"
    other_side = "tax" if side == "trade" else "trade"
    candidates = [candidate for candidate in other_groups
                  if key is not None and _document_candidate_key(candidate, other_side) == key]
    if not candidates:
        number = key[-1] if key else group.identity.number
        return (f"В полученных движениях {target} нет кандидата по соответствующему виду и номеру {number}"
                "; наличие карточки документа не проверено")
    details = []
    for candidate in sorted(candidates, key=lambda item: item.identity.original):
        conflicts = []
        if candidate.identity.family != group.identity.family:
            conflicts.append("виды допустимы по таблице соответствий, но связь конкретных документов по выгрузке не установлена")
        if candidate.identity.timestamp[:10] != group.identity.timestamp[:10]:
            def shown_day(value):
                try:
                    return datetime.fromisoformat(value).strftime("%d.%m.%Y")
                except ValueError:
                    return value[:10]
            conflicts.append("дата: " + shown_day(group.identity.timestamp) + " / " + shown_day(candidate.identity.timestamp))
        if candidate.warehouse_key != group.warehouse_key:
            conflicts.append("склад: " + (group.rows[0].warehouse or group.rows[0].warehouse_code)
                             + " / " + (candidate.rows[0].warehouse or candidate.rows[0].warehouse_code))
        if not _organizations_compatible(group.rows[0], candidate.rows[0]):
            conflicts.append("организация: " + (group.rows[0].organization_inn or group.rows[0].organization or "не указана")
                             + " / " + (candidate.rows[0].organization_inn or candidate.rows[0].organization or "не указана"))
        if not conflicts:
            conflicts.append("кандидат есть, но проверка уникальности или реквизитов не пройдена")
        details.append(candidate.identity.original + " [" + "; ".join(conflicts) + "]")
    return (f"В {target} найден кандидат, требуется проверить: " + "; ".join(details)
            + "; наличие карточки документа не проверено")


def _document_candidate_index(groups, side: str):
    index = defaultdict(set)
    for group in groups.values():
        key = _document_candidate_key(group, side)
        if key is not None:
            index[key].update(_group_document_refs(group.rows))
    return {key: tuple(sorted(refs)) for key, refs in index.items()}
