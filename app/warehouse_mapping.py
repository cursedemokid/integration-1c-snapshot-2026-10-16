from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace

from app.config import ConfigurationError, Settings, WarehouseMappingConfig
from app.document_matching import parse_document
from app.models import LedgerRow, Warehouse


@dataclass(frozen=True)
class WarehouseMappingResolution:
    mappings: tuple[WarehouseMappingConfig, ...]
    diagnostics: dict[str, object]


def enrich_transfer_warehouse_codes(
    rows: list[LedgerRow],
    warehouses: list[Warehouse],
    context_rows: list[LedgerRow] | None = None,
) -> list[LedgerRow]:
    """Resolve UT transfer endpoint labels and copy their accounting names.

    Ambiguous and unknown labels stay unresolved for manual review. Existing source
    codes and display names are preserved.
    """
    by_name = _warehouses_by_name(warehouses)
    by_code: dict[str, list[Warehouse]] = {}
    for warehouse in warehouses:
        by_code.setdefault(warehouse.code, []).append(warehouse)
    contexts: dict[tuple[str, str], list[LedgerRow]] = {}
    for row in context_rows if context_rows is not None else rows:
        organization_key = _name_key(row.organization_inn or row.organization)
        contexts.setdefault((row.document, organization_key), []).append(row)
    enriched: list[LedgerRow] = []
    for row in rows:
        updates: dict[str, str] = {}
        endpoint_conflict = False
        for name_field, accounting_field, code_field in (
            ("sender_warehouse", "sender_accounting_warehouse", "sender_warehouse_code"),
            ("receiver_warehouse", "receiver_accounting_warehouse", "receiver_warehouse_code"),
        ):
            code = getattr(row, code_field)
            code_matches = by_code.get(code, []) if code else []
            warehouse = code_matches[0] if len(code_matches) == 1 else None
            name = getattr(row, name_field)
            name_key = _name_key(name)
            name_matches = by_name.get(name_key, []) if name_key else []
            if code and warehouse is None:
                endpoint_conflict = True
            elif code and name and (len(name_matches) != 1 or name_matches[0].code != code):
                endpoint_conflict = True
                warehouse = None
            elif warehouse is None and not code:
                warehouse = name_matches[0] if len(name_matches) == 1 else None
                if warehouse and not code:
                    updates[code_field] = warehouse.code
            accounting_hint = getattr(row, accounting_field, "")
            if warehouse and accounting_hint and _name_key(accounting_hint) != _name_key(warehouse.accounting_name):
                endpoint_conflict = True
                warehouse = None
            if warehouse and warehouse.accounting_name and not getattr(row, accounting_field, ""):
                updates[accounting_field] = warehouse.accounting_name
        sender_accounting = getattr(row, "sender_accounting_warehouse", "") or updates.get("sender_accounting_warehouse", "")
        receiver_accounting = getattr(row, "receiver_accounting_warehouse", "") or updates.get("receiver_accounting_warehouse", "")
        scope = ""
        is_transfer = parse_document(row.document, row.period).family == "transfer"
        if is_transfer and not endpoint_conflict and sender_accounting and receiver_accounting:
            scope = _transfer_scope(sender_accounting, receiver_accounting)
        elif is_transfer:
            scope = _scope_from_document_context(
                row,
                contexts.get((row.document, _name_key(row.organization_inn or row.organization)), []),
                by_code,
            )
        if scope and not getattr(row, "transfer_scope", ""):
            updates["transfer_scope"] = scope
        enriched.append(replace(row, **updates) if updates else row)
    return enriched


def _scope_from_document_context(
    row: LedgerRow,
    context: list[LedgerRow],
    by_code: dict[str, list[Warehouse]],
) -> str:
    if not context or any(not item.warehouse_code for item in context):
        return ""
    codes = {item.warehouse_code for item in context}
    if len(codes) != 2:
        return ""
    # Existing endpoint information must agree with the inferred unordered pair.
    by_name: dict[str, list[Warehouse]] = {}
    for entries in by_code.values():
        for warehouse in entries:
            if warehouse.name:
                by_name.setdefault(_name_key(warehouse.name), []).append(warehouse)
    endpoint_pairs: set[tuple[str, str]] = set()
    for item in context:
        resolved_endpoints: list[str] = []
        for name_field, code_field in (
            ("sender_warehouse", "sender_warehouse_code"),
            ("receiver_warehouse", "receiver_warehouse_code"),
        ):
            explicit_code = getattr(item, code_field)
            explicit_name = getattr(item, name_field)
            if not explicit_code and not explicit_name:
                resolved_endpoints.append("")
                continue
            code_matches = by_code.get(explicit_code, []) if explicit_code else []
            name_key = _name_key(explicit_name)
            name_matches = by_name.get(name_key, []) if name_key else []
            if explicit_code and explicit_name:
                if len(code_matches) != 1 or len(name_matches) != 1 or code_matches[0].code != name_matches[0].code:
                    return ""
                matches = code_matches
            elif explicit_code:
                matches = code_matches
            else:
                matches = name_matches
            if len(matches) != 1 or matches[0].code not in codes:
                return ""
            resolved_endpoints.append(matches[0].code)
        if all(resolved_endpoints):
            endpoint_pairs.add(tuple(sorted(resolved_endpoints)))
    if len(endpoint_pairs) > 1:
        return ""
    if endpoint_pairs and set(next(iter(endpoint_pairs))) != codes:
        return ""
    accounting_names: list[str] = []
    for code in codes:
        matches = by_code.get(code, [])
        if len(matches) != 1 or not matches[0].accounting_name:
            return ""
        accounting_names.append(matches[0].accounting_name)
    return _transfer_scope(accounting_names[0], accounting_names[1])


def _transfer_scope(sender: str, receiver: str) -> str:
    return "internal" if _name_key(sender) == _name_key(receiver) else "external"


def resolve_warehouse_mappings(
    settings: Settings,
    directories: dict[str, list[Warehouse]],
) -> WarehouseMappingResolution:
    trade_id = settings.trade_base.base_id
    tax_bases = settings.tax_bases
    trade_warehouses = directories.get(trade_id, [])
    tax_name_indexes = {
        base.base_id: _warehouses_by_name(directories.get(base.base_id, []))
        for base in tax_bases
    }

    grouped_trade: dict[str, list[Warehouse]] = {}
    display_names: dict[str, str] = {}
    blank_trade_codes: set[str] = set()
    for warehouse in trade_warehouses:
        if not warehouse.accounting_name:
            blank_trade_codes.add(warehouse.code)
            continue
        key = _name_key(warehouse.accounting_name)
        grouped_trade.setdefault(key, []).append(warehouse)
        display_names.setdefault(key, warehouse.accounting_name)

    automatic: list[WarehouseMappingConfig] = []
    unresolved: list[dict[str, object]] = []
    for name_key, trade_group in grouped_trade.items():
        missing_bases: list[str] = []
        ambiguous_bases: list[str] = []
        accounting_warehouses: dict[str, Warehouse] = {}
        for base in tax_bases:
            matches = tax_name_indexes[base.base_id].get(name_key, [])
            if not matches:
                missing_bases.append(base.base_id)
            elif len(matches) > 1:
                ambiguous_bases.append(base.base_id)
            else:
                accounting_warehouses[base.base_id] = matches[0]

        if missing_bases or ambiguous_bases:
            unresolved.append(
                {
                    "accounting_name": display_names[name_key],
                    "trade_codes": [warehouse.code for warehouse in trade_group],
                    "trade_count": len(trade_group),
                    "missing_bases": missing_bases,
                    "ambiguous_bases": ambiguous_bases,
                }
            )
        if not accounting_warehouses:
            continue

        first_tax = next(base for base in tax_bases if base.base_id in accounting_warehouses)
        first_warehouse = accounting_warehouses[first_tax.base_id]
        codes: dict[str, tuple[str, ...]] = {
            trade_id: tuple(warehouse.code for warehouse in trade_group),
            **{
                base.base_id: (accounting_warehouses[base.base_id].code,)
                for base in tax_bases if base.base_id in accounting_warehouses
            },
        }
        automatic.append(
            WarehouseMappingConfig(
                mapping_id=f"auto:{first_tax.base_id}:{first_warehouse.code}",
                label=first_warehouse.name,
                codes=codes,
            )
        )

    effective = _merge_manual_mappings(settings, automatic)
    mapped_codes = {
        base_id: {
            code
            for mapping in effective
            for code in mapping.codes_for(base_id)
        }
        for base_id in [trade_id, *(base.base_id for base in tax_bases)]
    }
    mapped_trade_codes = mapped_codes[trade_id]
    tax_ids = [base.base_id for base in tax_bases]
    unresolved = [
        issue | {
            "trade_codes": [
                code for code in issue["trade_codes"]
                if not any(
                    code in mapping.codes_for(trade_id)
                    and all(mapping.codes_for(base_id) for base_id in tax_ids)
                    for mapping in effective
                )
            ],
        }
        for issue in unresolved
    ]
    unresolved = [
        issue | {"trade_count": len(issue["trade_codes"])}
        for issue in unresolved
    ]
    unresolved = [issue for issue in unresolved if issue["trade_count"]]
    unreferenced_accounting = [
        {
            "base_id": base.base_id,
            "code": warehouse.code,
            "name": warehouse.name,
        }
        for base in tax_bases
        for warehouse in directories.get(base.base_id, [])
        if warehouse.code not in mapped_codes[base.base_id]
    ]
    effective.sort(key=lambda mapping: (_name_key(mapping.label), mapping.mapping_id))
    diagnostics: dict[str, object] = {
        "mapped_logical_count": len(effective),
        "mapped_trade_count": len(mapped_trade_codes),
        "blank_trade_count": len(blank_trade_codes - mapped_trade_codes),
        "unresolved_trade_count": sum(int(issue["trade_count"]) for issue in unresolved),
        "unresolved_links": unresolved,
        "unreferenced_accounting": unreferenced_accounting,
    }
    return WarehouseMappingResolution(mappings=tuple(effective), diagnostics=diagnostics)


def _merge_manual_mappings(
    settings: Settings,
    automatic: list[WarehouseMappingConfig],
) -> list[WarehouseMappingConfig]:
    result = list(automatic)
    trade_id = settings.trade_base.base_id
    tax_ids = tuple(base.base_id for base in settings.tax_bases)

    for manual in settings.warehouse_mappings:
        manual_tax_codes = tuple(manual.codes_for(base_id) for base_id in tax_ids)
        target_indexes = [
            index
            for index, mapping in enumerate(result)
            if all(
                not mapping.codes_for(base_id)
                or mapping.codes_for(base_id) == manual.codes_for(base_id)
                for base_id in tax_ids
            )
            and any(
                code in mapping.codes_for(trade_id)
                for code in manual.codes_for(trade_id)
            )
        ]
        conflicting_trade = {
            code: mapping.mapping_id
            for mapping in result
            for code in manual.codes_for(trade_id)
            if code in mapping.codes_for(trade_id)
            and tuple(mapping.codes_for(base_id) for base_id in tax_ids) != manual_tax_codes
        }
        if conflicting_trade:
            rendered = ", ".join(f"{code} ({mapping_id})" for code, mapping_id in conflicting_trade.items())
            raise ConfigurationError(
                f"Ручное соответствие {manual.mapping_id} противоречит СкладБухгалтерия: {rendered}"
            )

        if target_indexes:
            target_index = target_indexes[0]
            target = result[target_index]
            codes = dict(target.codes)
            codes[trade_id] = tuple(dict.fromkeys((*target.codes_for(trade_id), *manual.codes_for(trade_id))))
            for base_id in tax_ids:
                codes[base_id] = tuple(dict.fromkeys((*target.codes_for(base_id), *manual.codes_for(base_id))))
            result[target_index] = WarehouseMappingConfig(
                mapping_id=manual.mapping_id,
                label=target.label,
                codes=codes,
            )
            continue

        overlaps = [
            f"{base_id}:{code}"
            for mapping in result
            for base_id in manual.codes
            for code in manual.codes_for(base_id)
            if code in mapping.codes_for(base_id)
        ]
        if overlaps:
            raise ConfigurationError(
                f"Ручное соответствие {manual.mapping_id} частично пересекается с автоматическим: "
                f"{', '.join(overlaps)}"
            )
        result.append(manual)

    return result


def _warehouses_by_name(warehouses: list[Warehouse]) -> dict[str, list[Warehouse]]:
    result: dict[str, list[Warehouse]] = {}
    for warehouse in warehouses:
        if warehouse.name:
            result.setdefault(_name_key(warehouse.name), []).append(warehouse)
    return result


def _name_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.strip().split()).casefold()
