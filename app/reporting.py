from __future__ import annotations

import io
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from xml.sax.saxutils import escape

from app.document_matching import parse_document
from app.models import Discrepancy, ReconciliationResult, ReconciliationSummary
from app.reconciliation import (
    AMBIGUOUS_MATCH,
    BALANCE_AMOUNT_MISMATCH,
    BALANCE_QUANTITY_MISMATCH,
    BALANCE_UNCHECKED,
    BUH_ONLY_DOCUMENT,
    DOCUMENT_AMOUNT_MISMATCH,
    DOCUMENT_QUANTITY_MISMATCH,
    DUPLICATE_IN_TAX,
    EXTRA_BALANCE_IN_TAX,
    EXTRA_IN_TAX,
    MISSING_IN_TAX,
    MISSING_BALANCE_IN_TAX,
    MISSING_ITEM_IN_TAX,
    EXTRA_ITEM_IN_TAX,
    requires_manual_review,
)


HEADERS = [
    "База",
    "Тип расхождения",
    "Способ сопоставления",
    "Описание расхождения",
    "Рекомендация",
    "Период УТ",
    "Документ УТ",
    "Период БУХ",
    "Документ БУХ",
    "Код номенклатуры",
    "Артикул",
    "Номенклатура",
    "Соответствие склада",
    "Склад УТ",
    "Код склада УТ",
    "Склад БУХ",
    "Код склада БУХ",
    "Начальный остаток УТ, кол-во",
    "Начальный остаток БУХ, кол-во",
    "Разница начального остатка, кол-во",
    "Оборот УТ, кол-во",
    "Оборот БУХ, кол-во",
    "Разница оборота, кол-во",
    "Конечный остаток УТ, кол-во",
    "Конечный остаток БУХ, кол-во",
    "Разница конечного остатка, кол-во",
    "Начальный остаток УТ, сумма",
    "Начальный остаток БУХ, сумма",
    "Разница начального остатка, сумма",
    "Оборот УТ, сумма",
    "Оборот БУХ, сумма",
    "Разница оборота, сумма",
    "Конечный остаток УТ, сумма",
    "Конечный остаток БУХ, сумма",
    "Разница конечного остатка, сумма",
    "Склад-отправитель УТ",
    "Склад-получатель УТ",
    "Организация БУХ",
    "ИНН организации БУХ",
]


def build_excel_report(source: ReconciliationResult | list[Discrepancy]) -> bytes:
    if isinstance(source, ReconciliationResult):
        discrepancies = source.discrepancies
        summaries = source.summaries or []
    else:
        discrepancies = source
        summaries = []

    if discrepancies:
        detail_rows = [HEADERS] + [
            _report_row(item)
            for item in discrepancies
        ]
        sheets = [
            ("Итоги", _summary_rows(discrepancies, summaries)),
            ("Расхождения", detail_rows),
        ]
        balance_types = {
            BALANCE_QUANTITY_MISMATCH,
            BALANCE_AMOUNT_MISMATCH,
            MISSING_BALANCE_IN_TAX,
            EXTRA_BALANCE_IN_TAX,
        }
        categories = (
            (
                "Документы",
                [item for item in discrepancies if item.discrepancy_type not in {
                    DOCUMENT_QUANTITY_MISMATCH,
                    DOCUMENT_AMOUNT_MISMATCH,
                    *balance_types,
                }],
            ),
            ("Количество", [item for item in discrepancies if item.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH]),
            ("Остатки", [item for item in discrepancies if item.discrepancy_type in balance_types]),
        )
        sheets.extend((name, [HEADERS] + [_report_row(item) for item in items]) for name, items in categories)
    elif summaries:
        sheets = [("Итоги", _summary_rows([], summaries))]
    else:
        sheets = [("Итоги", [["Расхождения не обнаружены"]])]

    return _build_xlsx(sheets)


def _summary_rows(
    discrepancies: list[Discrepancy],
    summaries: list[ReconciliationSummary],
) -> list[list[object]]:
    grouped: dict[tuple[str, str, str], list[Discrepancy]] = defaultdict(list)
    for item in discrepancies:
        grouped[(item.base, item.discrepancy_type, item.match_method or "Не сопоставлен")].append(item)

    rows: list[list[object]] = [
        ["Итоги сверки"],
        ["Всего строк с расхождениями", len(discrepancies)],
        [],
        ["База", "Тип расхождения", "Способ сопоставления", "Строк", "Документов"],
    ]
    for (base, discrepancy_type, match_method), items in sorted(grouped.items()):
        documents = {
            (item.trade_row.document if item.trade_row else "")
            or (item.tax_row.document if item.tax_row else "")
            or f"остаток:{item.period}:{item.nomenclature_code or item.nomenclature}"
            for item in items
        }
        rows.append([base, discrepancy_type, match_method, len(items), len(documents)])
    if summaries:
        metrics = (
            ("Документов УТ", "trade_documents"),
            ("Документов БУХ", "tax_documents"),
            ("Сопоставлено", "matched_documents"),
            ("Сопоставлено групп импорта", "matched_import_groups"),
            ("По номеру и дате", "matched_by_number_date"),
            ("По составу", "matched_by_content"),
            ("Неоднозначных", "ambiguous_documents"),
            ("Возможных дублей", "duplicate_documents"),
            ("Не найдено в БУХ", "missing_documents"),
            ("Лишних в БУХ", "extra_documents"),
            ("Только в БУХ", "buh_only_documents"),
            ("Исключено внутренних перемещений", "ignored_internal_transfers"),
            ("Исключено документов УТ по типу", "excluded_trade_documents"),
            ("Исключено документов БУХ по типу", "excluded_tax_documents"),
            ("Ожидают правила типа в УТ", "pending_trade_documents"),
            ("Ожидают правила типа в БУХ", "pending_tax_documents"),
            ("Различий количества движения", "document_quantity_differences"),
            ("Различий количества остатков", "balance_quantity_differences"),
        )
        rows.extend(
            [
                [],
                ["Статистика сопоставления документов"],
                ["Показатель", *(summary.base for summary in summaries)],
            ]
        )
        rows.extend(
            [label, *(getattr(summary, field) for summary in summaries)]
            for label, field in metrics
        )
    return rows


def _report_row(item: Discrepancy) -> list[object]:
    trade = item.trade_row
    tax = item.tax_row
    row = tax or trade
    if row is None:
        raise ValueError("Расхождение не содержит исходную строку")
    return [
        item.base,
        item.discrepancy_type,
        item.match_method,
        item.difference_summary,
        item.recommendation,
        _value(trade, "period"),
        _documents_value(trade),
        _value(tax, "period"),
        _documents_value(tax),
        row.nomenclature_code,
        row.article,
        row.nomenclature,
        row.warehouse_mapping_id,
        _value(trade, "warehouse"),
        _value(trade, "warehouse_code"),
        _value(tax, "warehouse"),
        _value(tax, "warehouse_code"),
        _value(trade, "opening_quantity"),
        _value(tax, "opening_quantity"),
        _delta(trade, tax, "opening_quantity"),
        _value(trade, "turnover_quantity"),
        _value(tax, "turnover_quantity"),
        _delta(trade, tax, "turnover_quantity"),
        _value(trade, "closing_quantity"),
        _value(tax, "closing_quantity"),
        _delta(trade, tax, "closing_quantity"),
        _value(trade, "opening_amount"),
        _value(tax, "opening_amount"),
        _delta(trade, tax, "opening_amount"),
        _value(trade, "turnover_amount"),
        _value(tax, "turnover_amount"),
        _delta(trade, tax, "turnover_amount"),
        _value(trade, "closing_amount"),
        _value(tax, "closing_amount"),
        _delta(trade, tax, "closing_amount"),
        _value(trade, "sender_warehouse"),
        _value(trade, "receiver_warehouse"),
        _value(tax, "organization"),
        _value(tax, "organization_inn"),
    ]


def _value(row, field: str) -> object:
    return getattr(row, field) if row is not None else ""


def _documents_value(row) -> str:
    if row is None:
        return ""
    return "; ".join(row.related_documents or (row.document,))


def _delta(trade, tax, field: str) -> object:
    if trade is None or tax is None:
        return ""
    return getattr(tax, field) - getattr(trade, field)


ACTION_HEADERS = [
    "Действие",
    "База",
    "Причина",
    "Тип документа",
    "Номер",
    "Дата и время",
    "Документ УТ",
    "Документ БУХ",
    "Дата документа УТ",
    "Дата документа БУХ",
    "Склад УТ",
    "Склад БУХ",
    "Найдено вариантов",
    "Артикул",
    "Номенклатура",
    "Организация УТ",
    "ИНН организации УТ",
    "Организация БУХ",
    "ИНН организации БУХ",
    "Контрольная точка",
    "Дата контроля",
    "Статус наличия документа",
    "Количество УТ",
    "Количество БУХ",
    "Разница БУХ − УТ",
    "Период исходной строки УТ",
    "Исходный начальный остаток УТ",
    "Исходный конечный остаток УТ",
    "Период исходной строки БУХ",
    "Исходный начальный остаток БУХ",
    "Исходный конечный остаток БУХ",
]

BALANCE_HEADERS = [
    "Дата расхождения",
    "Артикул",
    "Наименование",
    "Остаток в УТ",
    "Остаток в НБ",
    "Расхождение",
    "База",
    "Тип остатка",
    "Причина",
    "Логический склад",
    "Код номенклатуры",
    "Склад УТ",
    "Склад НБ",
    "Организация УТ",
    "ИНН организации УТ",
    "Организация БУХ",
    "ИНН организации БУХ",
]

ADD_TO_TAX = "Довнести в БУХ"
EXTRA_IN_TAX_SHEET = "Лишние в БУХ"
MANUAL_REVIEW = "Проверить вручную"
UNCHECKED_BASES = "Базы не проверены"
BALANCE_DIFFERENCES = "Расхождения остатков"


@dataclass(frozen=True)
class _DocumentAction:
    section: str
    action: str
    base: str
    reason: str
    document_type: str
    number: str
    timestamp: str
    trade_document: str
    tax_document: str
    trade_document_date: str
    tax_document_date: str
    trade_warehouse: str
    tax_warehouse: str
    variants: int
    article: str = ""
    nomenclature: str = ""
    trade_quantity: Decimal | str = ""
    tax_quantity: Decimal | str = ""
    quantity_difference: Decimal | str = ""
    trade_organization: str = ""
    trade_inn: str = ""
    tax_organization: str = ""
    tax_inn: str = ""
    balance_point: str = ""
    balance_date: str = ""
    document_existence: str = "unknown"
    trade_source_period: str = ""
    trade_source_opening: Decimal | str = ""
    trade_source_closing: Decimal | str = ""
    tax_source_period: str = ""
    tax_source_opening: Decimal | str = ""
    tax_source_closing: Decimal | str = ""


@dataclass(frozen=True)
class _BalanceDifference:
    base: str
    control_date: str
    balance_type: str
    reason: str
    logical_warehouse: str
    nomenclature_code: str
    article: str
    nomenclature: str
    trade_warehouse: str
    tax_warehouse: str
    trade_quantity: Decimal
    tax_quantity: Decimal
    quantity_difference: Decimal
    trade_organization: str = ""
    trade_inn: str = ""
    tax_organization: str = ""
    tax_inn: str = ""


def build_action_excel_report(source: ReconciliationResult | list[Discrepancy]) -> bytes:
    discrepancies = source.discrepancies if isinstance(source, ReconciliationResult) else source
    summaries = (source.summaries or []) if isinstance(source, ReconciliationResult) else []
    source_errors = source.source_errors if isinstance(source, ReconciliationResult) else {}
    notices = source.notices if isinstance(source, ReconciliationResult) else []
    start_date = source.start_date if isinstance(source, ReconciliationResult) else None
    end_date = source.end_date if isinstance(source, ReconciliationResult) else None
    actions = _document_actions(discrepancies)
    balance_differences = _balance_differences(discrepancies, start_date, end_date)
    direct_actions = sum(action.section != MANUAL_REVIEW for action in actions)
    manual_actions = len(actions) - direct_actions

    grouped: dict[tuple[str, str], int] = defaultdict(int)
    for action in actions:
        grouped[(action.section, action.base)] += 1
    balance_counts: dict[str, int] = defaultdict(int)
    for difference in balance_differences:
        balance_counts[difference.base] += 1

    summary_rows: list[list[object]] = [
        ["Список документов для исправления"],
        ["Требуют исправления", direct_actions],
        ["Требуют ручной проверки", manual_actions],
        ["Баз проверено", len(summaries)],
        ["Баз не проверено", len(source_errors)],
        ["Расхождений остатков", len(balance_differences)],
        ["Раздел", "База", "Строк отчёта"],
    ]
    summary_rows.extend(
        [section, base, count]
        for (section, base), count in sorted(grouped.items())
    )
    summary_rows.extend([
        [],
        ["Статистика по базам"],
        [
            "База",
            "Документов УТ",
            "Документов БУХ",
            "Сопоставлено",
            "Не найдено в БУХ",
            "Лишних в БУХ",
            "Дублей",
            "Неоднозначных",
            "Внутренних перемещений исключено",
            "Исключено по типу УТ",
            "Исключено по типу БУХ",
            "Ожидают правила УТ",
            "Ожидают правила БУХ",
            "Сопоставлено групп импорта",
            "Расхождений остатков",
            "Складских групп УТ",
            "Складских групп БУХ",
            "Сопоставлено складских групп",
            "Товарных строк движений УТ",
            "Товарных строк движений БУХ",
            "Документов с непроверенным наличием",
            "Исходных строк в ручной проверке",
        ],
    ])
    summary_rows.extend(
        [
            summary.base,
            summary.trade_documents,
            summary.tax_documents,
            summary.matched_documents,
            summary.missing_documents,
            summary.extra_documents,
            summary.duplicate_documents,
            summary.ambiguous_documents,
            summary.ignored_internal_transfers,
            summary.excluded_trade_documents,
            summary.excluded_tax_documents,
            summary.pending_trade_documents,
            summary.pending_tax_documents,
            summary.matched_import_groups,
            balance_counts[summary.base],
            summary.trade_document_groups,
            summary.tax_document_groups,
            summary.matched_document_groups,
            summary.trade_item_rows,
            summary.tax_item_rows,
            summary.unverified_documents,
            summary.manual_item_rows,
        ]
        for summary in summaries
    )
    summary_rows.extend([
        [],
        [UNCHECKED_BASES],
        ["База", "Статус", "Причина"],
    ])
    summary_rows.extend(
        [base, "Не проверена", reason]
        for base, reason in source_errors.items()
    )
    if notices:
        summary_rows.extend([
            [],
            ["Ограничения сверки"],
            ["База", "Ограничение", "Затронуто строк"],
        ])
        summary_rows.extend(
            [notice.base, notice.reason, notice.affected_rows]
            for notice in notices
        )
    summary_rows.extend([
        [],
        ["Инструкция"],
        [ADD_TO_TAX, "Довнести указанный документ из УТ в соответствующую базу БУХ."],
        [EXTRA_IN_TAX_SHEET, "Проверить движение БУХ, не найденное в УТ; для сопоставленной пары проверить превышение количества существующего документа."],
        [MANUAL_REVIEW, "Не изменять автоматически; проверить соответствие документов вручную."],
    ])

    balance_rows = [BALANCE_HEADERS]
    balance_rows.extend(_balance_row(item) for item in balance_differences)
    sheets: list[tuple[str, list[list[object]]]] = [
        ("Итоги", summary_rows),
        (BALANCE_DIFFERENCES, balance_rows),
    ]
    for section in (ADD_TO_TAX, EXTRA_IN_TAX_SHEET, MANUAL_REVIEW):
        section_rows = [ACTION_HEADERS]
        section_rows.extend(_action_row(action) for action in actions if action.section == section)
        sheets.append((section, section_rows))
    if source_errors:
        unchecked_rows: list[list[object]] = [["База", "Статус", "Причина", "Что сделать"]]
        unchecked_rows.extend(
            [
                base,
                "Не проверена",
                reason,
                "Исправить или проверить эндпоинт 1С и повторить сверку.",
            ]
            for base, reason in source_errors.items()
        )
        sheets.append((UNCHECKED_BASES, unchecked_rows))
    return _build_xlsx(sheets, action_report=True)


def _balance_differences(
    discrepancies: list[Discrepancy],
    start_date: date | None,
    end_date: date | None,
) -> list[_BalanceDifference]:
    balance_types = {
        BALANCE_QUANTITY_MISMATCH,
        MISSING_BALANCE_IN_TAX,
        EXTRA_BALANCE_IN_TAX,
    }
    source = [item for item in discrepancies if item.discrepancy_type in balance_types]
    parsed_dates = [
        parsed.date()
        for item in source
        if (parsed := _parse_period(item.period)) is not None
    ]
    inferred_start = start_date or (min(parsed_dates) if parsed_dates else None)
    inferred_end = end_date or (max(parsed_dates) if parsed_dates else None)
    grouped: dict[tuple[object, ...], _BalanceDifference] = {}

    for item in source:
        trade = item.trade_row
        tax = item.tax_row
        row = trade or tax
        if row is None:
            continue
        if item.balance_point:
            field_prefix = item.balance_point
            balance_type = "Начальный остаток" if field_prefix == "opening" else "Конечный остаток"
            control_date = item.balance_control_date.strftime("%d.%m.%Y") if item.balance_control_date else item.period
        else:
            field_prefix, balance_type, control_date = _control_point(row.period, inferred_start, inferred_end)
        quantity_field = f"{field_prefix}_quantity"
        trade_quantity = item.balance_trade_quantity if item.balance_point else getattr(trade, quantity_field) if trade else None
        tax_quantity = item.balance_tax_quantity if item.balance_point else getattr(tax, quantity_field) if tax else None
        if trade_quantity is None or tax_quantity is None:
            continue
        reason = _balance_reason(trade, tax)
        logical_warehouse = (
            _first_value(trade, tax, "warehouse_mapping_id")
            or _first_value(trade, tax, "warehouse_code")
            or _first_value(trade, tax, "warehouse")
        )
        difference = _BalanceDifference(
            base=item.base,
            control_date=control_date,
            balance_type=balance_type,
            reason=reason,
            logical_warehouse=logical_warehouse,
            nomenclature_code=_first_value(trade, tax, "nomenclature_code"),
            article=_first_value(trade, tax, "article"),
            nomenclature=_first_value(trade, tax, "nomenclature"),
            trade_warehouse=_first_value(trade, None, "warehouse"),
            tax_warehouse=_first_value(tax, None, "warehouse"),
            trade_quantity=trade_quantity,
            tax_quantity=tax_quantity,
            quantity_difference=tax_quantity - trade_quantity,
            trade_organization=trade.organization if trade else "",
            trade_inn=trade.organization_inn if trade else "",
            tax_organization=tax.organization if tax else "",
            tax_inn=tax.organization_inn if tax else "",
        )
        item_identity = (
            difference.nomenclature_code
            or difference.article
            or difference.nomenclature.casefold()
        )
        key = (
            difference.base,
            difference.logical_warehouse,
            item_identity,
            difference.control_date,
            difference.balance_type,
            difference.trade_inn or difference.trade_organization,
            difference.tax_inn or difference.tax_organization,
        )
        grouped[key] = difference

    return sorted(
        grouped.values(),
        key=lambda item: (
            item.base.casefold(),
            _display_date_sort_key(item.control_date),
            item.balance_type,
            item.logical_warehouse.casefold(),
            (item.article or item.nomenclature_code or item.nomenclature).casefold(),
        ),
    )


def _control_point(
    period: str,
    start_date: date | None,
    end_date: date | None,
) -> tuple[str, str, str]:
    parsed = _parse_period(period)
    control_date = parsed.date() if parsed is not None else None
    if control_date is not None and start_date is not None and end_date is not None:
        if control_date == start_date != end_date:
            return "opening", "Начальный остаток", control_date.strftime("%d.%m.%Y")
        if control_date == end_date != start_date:
            return "closing", "Конечный остаток", control_date.strftime("%d.%m.%Y")
        if control_date == start_date == end_date:
            is_opening = parsed.time() <= time(12)
            return (
                "opening" if is_opening else "closing",
                "Начальный остаток" if is_opening else "Конечный остаток",
                control_date.strftime("%d.%m.%Y"),
            )
    if parsed is not None:
        is_opening = parsed.time() <= time(12)
        return (
            "opening" if is_opening else "closing",
            "Начальный остаток" if is_opening else "Конечный остаток",
            parsed.strftime("%d.%m.%Y"),
        )
    return "opening", "Начальный остаток", period


def _parse_period(value: str) -> datetime | None:
    normalized = " ".join(value.strip().split())
    for format_string in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(normalized, format_string)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _balance_reason(
    trade,
    tax,
) -> str:
    if trade is None:
        return "Нет остатка в УТ"
    if tax is None:
        return "Нет остатка в БУХ"
    return "Не совпадает количество"


def _first_value(primary, secondary, field: str) -> str:
    for row in (primary, secondary):
        if row is not None:
            value = getattr(row, field)
            if value:
                return str(value)
    return ""


def _display_date_sort_key(value: str) -> str:
    try:
        return datetime.strptime(value, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return value


def _balance_row(item: _BalanceDifference) -> list[object]:
    return [
        item.control_date,
        item.article,
        item.nomenclature,
        item.trade_quantity,
        item.tax_quantity,
        item.quantity_difference,
        item.base,
        item.balance_type,
        item.reason,
        item.logical_warehouse,
        item.nomenclature_code,
        item.trade_warehouse,
        item.tax_warehouse,
        item.trade_organization,
        item.trade_inn,
        item.tax_organization,
        item.tax_inn,
    ]


def _document_actions(discrepancies: list[Discrepancy]) -> list[_DocumentAction]:
    grouped: dict[tuple[object, ...], list[Discrepancy]] = defaultdict(list)
    for item in discrepancies:
        section = _action_section(item)
        if section is None:
            continue
        row = item.trade_row or item.tax_row
        if row is None:
            continue
        identity = parse_document(row.document, row.period)
        side = "trade" if item.trade_row is not None else "tax"
        can_compact = (
            section == MANUAL_REVIEW
            and bool(row.document)
            and not item.balance_point
            and item.discrepancy_type != DOCUMENT_QUANTITY_MISMATCH
            and item.discrepancy_type not in {
                BALANCE_UNCHECKED, MISSING_BALANCE_IN_TAX, EXTRA_BALANCE_IN_TAX,
            }
        )
        if can_compact:
            trade = item.trade_row
            tax = item.tax_row
            candidate_refs = (
                tuple(dict.fromkeys(item.trade_document_refs)) or tuple(
                    document for source_row in (trade,) if source_row is not None
                    for document in (source_row.related_documents or (source_row.document,)) if document
                ),
                tuple(dict.fromkeys(item.tax_document_refs)) or tuple(
                    document for source_row in (tax,) if source_row is not None
                    for document in (source_row.related_documents or (source_row.document,)) if document
                ),
            )
            control_point = (
                item.balance_point,
                item.balance_control_date,
                (item.trade_source.observed_point, item.trade_source.control_date)
                if item.trade_source else ("", None),
                (item.tax_source.observed_point, item.tax_source.control_date)
                if item.tax_source else ("", None),
            )
            detail_scope = (
                item.base,
                item.discrepancy_type,
                side,
                (trade.warehouse_mapping_id, trade.warehouse_code, trade.warehouse) if trade else ("", "", ""),
                (tax.warehouse_mapping_id, tax.warehouse_code, tax.warehouse) if tax else ("", "", ""),
                (trade.organization, trade.organization_inn) if trade else ("", ""),
                (tax.organization, tax.organization_inn) if tax else ("", ""),
                candidate_refs,
                item.document_existence,
                control_point,
                item.difference_summary,
                row.document,
            )
        else:
            item_key = (
                _first_value(item.trade_row, item.tax_row, "nomenclature_code")
                or _first_value(item.trade_row, item.tax_row, "article")
                or item.nomenclature.casefold()
            )
            if section == MANUAL_REVIEW:
                detail_scope = item_key if item.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH else id(item)
            else:
                detail_scope = item_key if item.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH or not row.document else ""
        grouped[(
            section,
            item.base,
            item.discrepancy_type,
            side,
            row.warehouse_mapping_id,
            row.organization_inn,
            identity.family,
            identity.document_type.casefold(),
            identity.number.casefold(),
            identity.timestamp,
            detail_scope,
        )].append(item)

    actions = [_document_action(items) for items in grouped.values()]
    return sorted(
        actions,
        key=lambda action: (
            (ADD_TO_TAX, EXTRA_IN_TAX_SHEET, MANUAL_REVIEW).index(action.section),
            action.base.casefold(),
            _timestamp_sort_key(action.timestamp),
            action.document_type.casefold(),
            action.number.casefold(),
        ),
    )


def _action_section(item: Discrepancy) -> str | None:
    if requires_manual_review(item):
        return MANUAL_REVIEW
    if item.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH:
        return EXTRA_IN_TAX_SHEET
    if item.discrepancy_type == MISSING_IN_TAX:
        return ADD_TO_TAX
    if item.discrepancy_type == EXTRA_IN_TAX:
        return EXTRA_IN_TAX_SHEET
    # Verified stock differences have a dedicated sheet; monetary differences
    # are outside the current quantity-only action report.
    return None


def _document_action(items: list[Discrepancy]) -> _DocumentAction:
    first = items[0]
    rows = [row for item in items for row in (item.trade_row, item.tax_row) if row is not None]
    trade_rows = [item.trade_row for item in items if item.trade_row is not None]
    tax_rows = [item.tax_row for item in items if item.tax_row is not None]
    identity_row = trade_rows[0] if trade_rows else tax_rows[0]
    identity = parse_document(identity_row.document, identity_row.period)
    section = _action_section(first)
    if section is None:
        raise ValueError(f"Неподдерживаемый тип действия: {first.discrepancy_type}")

    if first.discrepancy_type == MISSING_IN_TAX:
        action = f"Довнести в {first.base}" if section == ADD_TO_TAX else "Проверить наличие документа"
        reason = first.difference_summary or f"Движение УТ не найдено в {first.base} за выбранный период"
    elif first.discrepancy_type == EXTRA_IN_TAX:
        action = "Проверить наличие документа в УТ"
        reason = first.difference_summary or "Движение не найдено; наличие карточки документа УТ не проверено"
    elif first.discrepancy_type == DUPLICATE_IN_TAX:
        action = f"Проверить дубль в {first.base}"
        reason = f"В {first.base} найдено несколько документов с одинаковыми типом, номером и датой"
    elif first.discrepancy_type == BUH_ONLY_DOCUMENT:
        action = f"Проверить допустимость в {first.base}"
        reason = f"Тип документа может создаваться только в {first.base}"
    elif first.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH:
        if (first.trade_row and first.tax_row
                and first.trade_row.turnover_quantity * first.tax_row.turnover_quantity < 0):
            action = "Проверить направление движения"
            reason = f"Противоположные знаки движения: {first.difference_summary}"
        else:
            action = f"Проверить количество существующего документа в {first.base}"
            reason = f"Расхождение количества внутри документа: {first.difference_summary}"
    elif first.discrepancy_type in {MISSING_ITEM_IN_TAX, EXTRA_ITEM_IN_TAX}:
        action = "Проверить состав существующего документа"
        reason = first.difference_summary
    elif first.discrepancy_type in {BALANCE_UNCHECKED, MISSING_BALANCE_IN_TAX, EXTRA_BALANCE_IN_TAX}:
        action = "Проверить контрольный остаток"
        reason = first.difference_summary or "Остаток не проверен: нет подтверждённой строки одной базы"
    else:
        action = "Сопоставить вручную"
        reason = first.difference_summary or "Найдено несколько возможных соответствий"

    variants = {row.document for row in rows if row.document}
    trade_quantity = first.trade_row.turnover_quantity if first.trade_row else Decimal("0")
    tax_quantity = first.tax_row.turnover_quantity if first.tax_row else Decimal("0")
    if first.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH and trade_quantity < 0 and tax_quantity < 0:
        trade_quantity, tax_quantity = -trade_quantity, -tax_quantity
    item_details = first.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH or section == MANUAL_REVIEW
    compact_details = len(items) > 1 and section == MANUAL_REVIEW
    trade_refs = tuple(dict.fromkeys(doc for item in items for doc in item.trade_document_refs)) or tuple(
        dict.fromkeys(document for row in trade_rows for document in (row.related_documents or (row.document,)) if document))
    tax_refs = tuple(dict.fromkeys(doc for item in items for doc in item.tax_document_refs)) or tuple(
        dict.fromkeys(document for row in tax_rows for document in (row.related_documents or (row.document,)) if document))
    # A grouped recommendation must never present one member's balances as group totals.
    trade_source = first.trade_source if len(items) == 1 else None
    tax_source = first.tax_source if len(items) == 1 else None
    observed_points = {(source.observed_point, source.control_date)
                       for item in items for source in (item.trade_source, item.tax_source)
                       if source is not None}
    observed_point, observed_date = next(iter(observed_points)) if len(observed_points) == 1 else ("", None)
    point = first.balance_point or observed_point
    control_date = first.balance_control_date or observed_date

    def detail_value(getter):
        values = [getter(item) for item in items]
        return "\n".join("" if value is None else str(value) for value in values)

    if compact_details:
        article = detail_value(lambda item: item.article or _first_value(item.trade_row, item.tax_row, "article"))
        nomenclature = detail_value(
            lambda item: item.nomenclature or _first_value(item.trade_row, item.tax_row, "nomenclature")
        )
        trade_quantity_value = detail_value(
            lambda item: item.trade_row.turnover_quantity if item.trade_row is not None else ""
        )
        tax_quantity_value = detail_value(
            lambda item: item.tax_row.turnover_quantity if item.tax_row is not None else ""
        )
        trade_source_period = detail_value(lambda item: item.trade_source.period if item.trade_source else "")
        trade_source_opening = detail_value(
            lambda item: item.trade_source.opening_quantity if item.trade_source else ""
        )
        trade_source_closing = detail_value(
            lambda item: item.trade_source.closing_quantity if item.trade_source else ""
        )
        tax_source_period = detail_value(lambda item: item.tax_source.period if item.tax_source else "")
        tax_source_opening = detail_value(
            lambda item: item.tax_source.opening_quantity if item.tax_source else ""
        )
        tax_source_closing = detail_value(
            lambda item: item.tax_source.closing_quantity if item.tax_source else ""
        )
        quantity_difference_value = detail_value(lambda item: "")
    else:
        article = first.article if item_details or not identity_row.document else ""
        nomenclature = first.nomenclature if item_details or not identity_row.document else ""
        trade_quantity_value = ((first.balance_trade_quantity if first.balance_trade_quantity is not None else "") if first.balance_point else
                                trade_quantity if item_details and identity_row.document and first.trade_row
                                else first.trade_row.closing_quantity if not identity_row.document and first.trade_row else "")
        tax_quantity_value = ((first.balance_tax_quantity if first.balance_tax_quantity is not None else "") if first.balance_point else
                              tax_quantity if item_details and identity_row.document and first.tax_row
                              else first.tax_row.closing_quantity if not identity_row.document and first.tax_row else "")
        trade_source_period = trade_source.period if trade_source else ""
        trade_source_opening = trade_source.opening_quantity if trade_source else ""
        trade_source_closing = trade_source.closing_quantity if trade_source else ""
        tax_source_period = tax_source.period if tax_source else ""
        tax_source_opening = tax_source.opening_quantity if tax_source else ""
        tax_source_closing = tax_source.closing_quantity if tax_source else ""
        quantity_difference_value = (
            tax_quantity - trade_quantity
            if first.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH else ""
        )

    return _DocumentAction(
        section=section,
        action=action,
        base=first.base,
        reason=reason,
        document_type="Остаток" if first.balance_point else identity.document_type or ("Остаток" if not identity_row.document else "Не распознан"),
        number=identity.number,
        timestamp=_display_timestamp(identity.timestamp),
        trade_document="; ".join(trade_refs),
        tax_document="; ".join(tax_refs),
        trade_document_date="; ".join(dict.fromkeys(
            _display_timestamp(parse_document(document).timestamp)
            for document in trade_refs
        )),
        tax_document_date="; ".join(dict.fromkeys(
            _display_timestamp(parse_document(document).timestamp)
            for document in tax_refs
        )),
        trade_warehouse="; ".join(sorted({row.warehouse for row in trade_rows if row.warehouse})),
        tax_warehouse="; ".join(sorted({row.warehouse for row in tax_rows if row.warehouse})),
        variants=max(len(variants), 1),
        article=article,
        nomenclature=nomenclature,
        trade_quantity=trade_quantity_value,
        tax_quantity=tax_quantity_value,
        quantity_difference=quantity_difference_value,
        trade_organization=first.trade_row.organization if first.trade_row else "",
        trade_inn=first.trade_row.organization_inn if first.trade_row else "",
        tax_organization=first.tax_row.organization if first.tax_row else "",
        tax_inn=first.tax_row.organization_inn if first.tax_row else "",
        balance_point=("Начальный остаток" if point == "opening" else "Конечный остаток") if point else "",
        balance_date=control_date.strftime("%d.%m.%Y") if control_date else "",
        document_existence={"found": "Документ найден", "confirmed_absent": "Отсутствие подтверждено", "unknown": "Наличие неизвестно"}[first.document_existence],
        trade_source_period=trade_source_period,
        trade_source_opening=trade_source_opening,
        trade_source_closing=trade_source_closing,
        tax_source_period=tax_source_period,
        tax_source_opening=tax_source_opening,
        tax_source_closing=tax_source_closing,
    )


def _display_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M:%S")
    except ValueError:
        return value


def _timestamp_sort_key(value: str) -> str:
    try:
        return datetime.strptime(value, "%d.%m.%Y %H:%M:%S").isoformat()
    except ValueError:
        return value


def _action_row(action: _DocumentAction) -> list[object]:
    return [
        action.action,
        action.base,
        action.reason,
        action.document_type,
        action.number,
        action.timestamp,
        action.trade_document,
        action.tax_document,
        action.trade_document_date,
        action.tax_document_date,
        action.trade_warehouse,
        action.tax_warehouse,
        action.variants,
        action.article,
        action.nomenclature,
        action.trade_organization,
        action.trade_inn,
        action.tax_organization,
        action.tax_inn,
        action.balance_point,
        action.balance_date,
        action.document_existence,
        action.trade_quantity,
        action.tax_quantity,
        action.quantity_difference,
        action.trade_source_period,
        action.trade_source_opening,
        action.trade_source_closing,
        action.tax_source_period,
        action.tax_source_opening,
        action.tax_source_closing,
    ]


def _build_xlsx(sheets: list[tuple[str, list[list[object]]]], action_report: bool = False) -> bytes:
    output = io.BytesIO()
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml(len(sheets)))
        archive.writestr("_rels/.rels", _root_rels_xml())
        archive.writestr("docProps/core.xml", _core_xml(timestamp))
        archive.writestr("docProps/app.xml", _app_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml([name for name, _ in sheets]))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels_xml(len(sheets)))
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, (sheet_name, rows) in enumerate(sheets, start=1):
            is_summary = index == 1 and len(sheets) > 1
            is_balance_report = sheet_name == BALANCE_DIFFERENCES
            primary_header_row = next(
                (row_index for row_index, row in enumerate(rows, start=1) if row and row[0] in {"База", "Раздел"}),
                4,
            ) if is_summary else 1
            header_rows = {1, primary_header_row} if is_summary else {1}
            if is_summary:
                header_rows.update(
                    row_index
                    for row_index, row in enumerate(rows, start=1)
                    if row and row[0] in {
                        "Статистика сопоставления документов",
                        "Показатель",
                        "Инструкция",
                        "Статистика по базам",
                        "База",
                        UNCHECKED_BASES,
                    }
                )
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _sheet_xml(
                    rows,
                    header_rows=header_rows,
                    freeze_rows=primary_header_row if is_summary else 1,
                    filter_row=primary_header_row if is_summary else 1,
                    summary=is_summary,
                    action_report=action_report,
                    balance_report=is_balance_report,
                ),
            )

    return output.getvalue()


def _sheet_xml(
    rows: list[list[object]],
    header_rows: set[int] | None = None,
    freeze_rows: int = 1,
    filter_row: int = 1,
    summary: bool = False,
    action_report: bool = False,
    balance_report: bool = False,
) -> str:
    header_rows = header_rows or {1}
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cells = []
        for column_index, value in enumerate(row, start=1):
            cell_ref = f"{_column_name(column_index)}{row_index}"
            cells.append(_cell_xml(
                cell_ref,
                value,
                header=row_index in header_rows,
                action_body=action_report and not summary and row_index not in header_rows,
            ))
        if row_index in header_rows:
            row_attributes = ' ht="32" customHeight="1"'
        elif action_report and not summary:
            row_attributes = ' ht="36" customHeight="1"'
        else:
            row_attributes = ""
        row_xml.append(f'<row r="{row_index}"{row_attributes}>{"".join(cells)}</row>')

    last_row = max(len(rows), 1)
    last_column = _column_name(max((len(row) for row in rows), default=1))
    columns = (
        """
    <col min="1" max="1" width="42" customWidth="1"/>
    <col min="2" max="2" width="34" customWidth="1"/>
    <col min="3" max="3" width="38" customWidth="1"/>
    <col min="4" max="5" width="18" customWidth="1"/>"""
        if summary
        else """
    <col min="1" max="1" width="18" customWidth="1"/>
    <col min="2" max="2" width="20" customWidth="1"/>
    <col min="3" max="3" width="48" customWidth="1"/>
    <col min="4" max="6" width="18" customWidth="1"/>
    <col min="7" max="7" width="16" customWidth="1"/>
    <col min="8" max="8" width="22" customWidth="1"/>
    <col min="9" max="9" width="28" customWidth="1"/>
    <col min="10" max="11" width="24" customWidth="1"/>
    <col min="12" max="13" width="42" customWidth="1"/>
    <col min="14" max="17" width="30" customWidth="1"/>"""
        if balance_report
        else """
    <col min="1" max="1" width="46" customWidth="1"/>
    <col min="2" max="2" width="18" customWidth="1"/>
    <col min="3" max="3" width="48" customWidth="1"/>
    <col min="4" max="4" width="36" customWidth="1"/>
    <col min="5" max="6" width="22" customWidth="1"/>
    <col min="7" max="8" width="52" customWidth="1"/>
    <col min="9" max="10" width="42" customWidth="1"/>
    <col min="11" max="12" width="32" customWidth="1"/>
    <col min="13" max="14" width="20" customWidth="1"/>
    <col min="15" max="15" width="48" customWidth="1"/>
    <col min="16" max="19" width="28" customWidth="1"/>
    <col min="20" max="22" width="30" customWidth="1"/>
    <col min="23" max="25" width="20" customWidth="1"/>
    <col min="26" max="31" width="32" customWidth="1"/>"""
        if action_report
        else """
    <col min="1" max="2" width="24" customWidth="1"/>
    <col min="3" max="3" width="30" customWidth="1"/>
    <col min="4" max="5" width="60" customWidth="1"/>
    <col min="6" max="9" width="42" customWidth="1"/>
    <col min="10" max="11" width="22" customWidth="1"/>
    <col min="12" max="12" width="48" customWidth="1"/>
    <col min="13" max="17" width="28" customWidth="1"/>
    <col min="18" max="35" width="18" customWidth="1"/>
    <col min="36" max="38" width="32" customWidth="1"/>
    <col min="39" max="39" width="20" customWidth="1"/>"""
    )

    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://internal.internal.internal.example.invalid/spreadsheetml/2006/main">
  <dimension ref="A1:{last_column}{last_row}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="{freeze_rows}" topLeftCell="A{freeze_rows + 1}" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <sheetFormatPr defaultRowHeight="15"/>
  <cols>{columns}
  </cols>
  <sheetData>{"".join(row_xml)}</sheetData>
  <autoFilter ref="A{filter_row}:{last_column}{last_row}"/>
</worksheet>'''


def _cell_xml(cell_ref: str, value: object, header: bool = False, action_body: bool = False) -> str:
    style = ' s="2"' if header else (' s="3"' if action_body else "")
    if isinstance(value, Decimal):
        number_style = ' s="3"' if action_body else ' s="1"'
        return f'<c r="{cell_ref}"{number_style}><v>{value}</v></c>'
    if isinstance(value, int | float):
        number_style = ' s="3"' if action_body else ' s="1"'
        return f'<c r="{cell_ref}"{number_style}><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{cell_ref}"{style} t="inlineStr"><is><t>{text}</t></is></c>'


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _content_types_xml(sheet_count: int) -> str:
    worksheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://internal.internal.internal.example.invalid/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  {worksheets}
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''


def _root_rels_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://internal.internal.internal.example.invalid/package/2006/relationships">
  <Relationship Id="rId1" Type="http://internal.internal.internal.example.invalid/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://internal.internal.internal.example.invalid/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://internal.internal.internal.example.invalid/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''


def _core_xml(timestamp: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://internal.internal.internal.example.invalid/package/2006/metadata/core-properties" xmlns:dc="http://internal.internal.example.invalid/dc/elements/1.1/" xmlns:dcterms="http://internal.internal.example.invalid/dc/terms/" xmlns:xsi="http://internal.internal.example.invalid/2001/XMLSchema-instance">
  <dc:creator>IntegrationOneS</dc:creator>
  <cp:lastModifiedBy>IntegrationOneS</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{timestamp}</dcterms:modified>
</cp:coreProperties>'''


def _app_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://internal.internal.internal.example.invalid/officeDocument/2006/extended-properties" xmlns:vt="http://internal.internal.internal.example.invalid/officeDocument/2006/docPropsVTypes">
  <Application>IntegrationOneS</Application>
</Properties>'''


def _workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://internal.internal.internal.example.invalid/spreadsheetml/2006/main" xmlns:r="http://internal.internal.internal.example.invalid/officeDocument/2006/relationships">
  <sheets>{sheets}</sheets>
</workbook>'''


def _workbook_rels_xml(sheet_count: int) -> str:
    worksheets = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://internal.internal.internal.example.invalid/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    styles_id = sheet_count + 1
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://internal.internal.internal.example.invalid/package/2006/relationships">
  {worksheets}
  <Relationship Id="rId{styles_id}" Type="http://internal.internal.internal.example.invalid/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''


def _styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://internal.internal.internal.example.invalid/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="0.00########"/></numFmts>
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF245B78"/><bgColor indexed="64"/></patternFill></fill></fills>
  <borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="FFD9E2E8"/></left><right style="thin"><color rgb="FFD9E2E8"/></right><top style="thin"><color rgb="FFD9E2E8"/></top><bottom style="thin"><color rgb="FFD9E2E8"/></bottom><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
