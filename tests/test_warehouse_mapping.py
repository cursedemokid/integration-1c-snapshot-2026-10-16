from dataclasses import replace
from decimal import Decimal
from unittest import TestCase

from app.config import ConfigurationError, OneCBaseConfig, Settings, WarehouseMappingConfig
from app.models import LedgerRow, Warehouse
from app.warehouse_mapping import enrich_transfer_warehouse_codes, resolve_warehouse_mappings


TRADE = OneCBaseConfig(name="Trade", url="trade", base_id="trade")
BUH2019 = OneCBaseConfig(name="BUH2019", url="buh2019", base_id="buh2019")
BUH2026 = OneCBaseConfig(name="BUH2026", url="buh2026", base_id="buh2026")


def settings(*mappings: WarehouseMappingConfig) -> Settings:
    return Settings(TRADE, [BUH2019, BUH2026], warehouse_mappings=list(mappings))


class WarehouseMappingTests(TestCase):
    def test_transfer_endpoint_names_resolve_only_when_exact_name_is_unique(self):
        row = LedgerRow(
            period="period", document="Перемещение", nomenclature="Товар",
            opening_quantity=Decimal(0), opening_amount=Decimal(0),
            turnover_quantity=Decimal(1), turnover_amount=Decimal(1),
            closing_quantity=Decimal(1), closing_amount=Decimal(1),
            sender_warehouse="  Секция   1 ", receiver_warehouse="Секция 2",
        )
        directories = [
            Warehouse("T1", "Секция 1", accounting_name="Общий бухгалтерский склад"),
            Warehouse("T2", "Секция 2", accounting_name="Общий бухгалтерский склад"),
            Warehouse("T3", "Другая секция", accounting_name="Accounting 3"),
        ]

        enriched = enrich_transfer_warehouse_codes([row], directories)[0]

        self.assertEqual((enriched.sender_warehouse, enriched.receiver_warehouse),
                         (row.sender_warehouse, row.receiver_warehouse))
        self.assertEqual((enriched.sender_warehouse_code, enriched.receiver_warehouse_code), ("T1", "T2"))
        self.assertEqual((enriched.sender_accounting_warehouse, enriched.receiver_accounting_warehouse),
                         ("Общий бухгалтерский склад", "Общий бухгалтерский склад"))

    def test_transfer_endpoint_ambiguity_missing_names_and_existing_codes_are_preserved(self):
        row = LedgerRow(
            period="period", document="Перемещение", nomenclature="Товар",
            opening_quantity=Decimal(0), opening_amount=Decimal(0),
            turnover_quantity=Decimal(1), turnover_amount=Decimal(1),
            closing_quantity=Decimal(1), closing_amount=Decimal(1),
            sender_warehouse="Секция", receiver_warehouse="Нет такого склада",
            sender_warehouse_code="", receiver_warehouse_code="",
        )
        directories = [Warehouse("T1", "Секция", accounting_name="Accounting 1"), Warehouse("T2", "секция", accounting_name="Accounting 2")]

        enriched = enrich_transfer_warehouse_codes([row], directories)[0]

        self.assertEqual(enriched.sender_warehouse_code, "")
        self.assertEqual(enriched.receiver_warehouse_code, "")

        with_source_code = replace(row, sender_warehouse_code="SOURCE-CODE")
        preserved = enrich_transfer_warehouse_codes([with_source_code], directories)[0]
        self.assertEqual(preserved.sender_warehouse_code, "SOURCE-CODE")

    def test_blank_transfer_label_does_not_resolve_to_blank_directory_name(self):
        row = LedgerRow(
            period="period", document="Перемещение", nomenclature="Товар",
            opening_quantity=Decimal(0), opening_amount=Decimal(0),
            turnover_quantity=Decimal(1), turnover_amount=Decimal(1),
            closing_quantity=Decimal(1), closing_amount=Decimal(1),
        )
        enriched = enrich_transfer_warehouse_codes([row], [Warehouse("T1", "")])[0]
        self.assertEqual((enriched.sender_warehouse_code, enriched.receiver_warehouse_code), ("", ""))

    def test_groups_trade_warehouses_and_resolves_each_accounting_base(self):
        directories = {
            "trade": [
                Warehouse("T1", "Section 1", accounting_name=" Main  warehouse "),
                Warehouse("T2", "Section 2", accounting_name="main warehouse"),
                Warehouse("T3", "Not mapped"),
                Warehouse("T4", "Unknown", accounting_name="Missing warehouse"),
            ],
            "buh2019": [Warehouse("B19", " Main  warehouse ")],
            "buh2026": [Warehouse("B26", "MAIN WAREHOUSE")],
        }

        resolution = resolve_warehouse_mappings(settings(), directories)

        self.assertEqual(len(resolution.mappings), 1)
        mapping = resolution.mappings[0]
        self.assertEqual(mapping.mapping_id, "auto:buh2019:B19")
        self.assertEqual(mapping.codes_for("trade"), ("T1", "T2"))
        self.assertEqual(mapping.codes_for("buh2019"), ("B19",))
        self.assertEqual(mapping.codes_for("buh2026"), ("B26",))
        self.assertEqual(
            {key: resolution.diagnostics[key] for key in (
                "mapped_logical_count", "mapped_trade_count", "blank_trade_count", "unresolved_trade_count"
            )},
            {
                "mapped_logical_count": 1,
                "mapped_trade_count": 2,
                "blank_trade_count": 1,
                "unresolved_trade_count": 1,
            },
        )

    def test_partial_mapping_survives_missing_or_ambiguous_other_tax_base(self):
        for b19 in ([], [Warehouse("B19a", "Main"), Warehouse("B19b", "Main")]):
            with self.subTest(b19=b19):
                resolution = resolve_warehouse_mappings(settings(), {
                    "trade": [Warehouse("T1", "Trade display", accounting_name="Main")],
                    "buh2019": b19,
                    "buh2026": [Warehouse("B26", "Main")],
                })
                self.assertEqual(len(resolution.mappings), 1)
                self.assertEqual(resolution.mappings[0].codes_for("trade"), ("T1",))
                self.assertEqual(resolution.mappings[0].codes_for("buh2019"), ())
                self.assertEqual(resolution.mappings[0].codes_for("buh2026"), ("B26",))
                issue = resolution.diagnostics["unresolved_links"][0]
                self.assertEqual(issue["trade_codes"], ["T1"])
                self.assertEqual(issue["missing_bases"] + issue["ambiguous_bases"], ["buh2019"])

    def test_manual_mapping_can_fill_a_partial_automatic_mapping(self):
        manual = WarehouseMappingConfig("manual-main", "Main", {
            "trade": "T1", "buh2019": "B19", "buh2026": "B26",
        })
        resolution = resolve_warehouse_mappings(settings(manual), {
            "trade": [Warehouse("T1", "Trade", accounting_name="Main")],
            "buh2019": [Warehouse("B19", "Main")],
            "buh2026": [Warehouse("B26", "Main")],
        })
        self.assertEqual(len(resolution.mappings), 1)
        self.assertEqual(resolution.mappings[0].mapping_id, "manual-main")
        self.assertEqual(resolution.mappings[0].codes_for("buh2019"), ("B19",))
        self.assertEqual(resolution.diagnostics["unresolved_links"], [])

    def test_transfer_name_enrichment_uses_accounting_name(self):
        row = LedgerRow(
            period="period", document="Перемещение", nomenclature="Товар",
            opening_quantity=Decimal(0), opening_amount=Decimal(0),
            turnover_quantity=Decimal(1), turnover_amount=Decimal(1),
            closing_quantity=Decimal(1), closing_amount=Decimal(1),
            sender_warehouse="Display sender", receiver_warehouse="Display receiver",
        )
        enriched = enrich_transfer_warehouse_codes([row], [
            Warehouse("T1", "Display sender", accounting_name="Accounting sender"),
            Warehouse("T2", "Display receiver", accounting_name="Accounting receiver"),
        ])[0]
        self.assertEqual((enriched.sender_warehouse_code, enriched.receiver_warehouse_code), ("T1", "T2"))
        self.assertEqual((enriched.sender_accounting_warehouse, enriched.receiver_accounting_warehouse),
                         ("Accounting sender", "Accounting receiver"))

    def test_transfer_scope_infers_from_exactly_two_document_warehouses(self):
        rows = [
            LedgerRow(period="p", document="Перемещение товаров 1 от 01.01.2026", nomenclature="A",
                      opening_quantity=Decimal(0), opening_amount=Decimal(0), turnover_quantity=Decimal(1),
                      turnover_amount=Decimal(1), closing_quantity=Decimal(1), closing_amount=Decimal(1), warehouse_code=code,
                      warehouse=code, organization="Org")
            for code in ("T1", "T2")
        ]
        warehouses = [Warehouse("T1", "Section 1", accounting_name="Accounting A"),
                      Warehouse("T2", "Section 2", accounting_name=" accounting a ")]
        result = enrich_transfer_warehouse_codes(rows[:1], warehouses, context_rows=rows)[0]
        self.assertEqual(result.transfer_scope, "internal")
        self.assertEqual((result.sender_warehouse_code, result.receiver_warehouse_code), ("", ""))

    def test_transfer_scope_external_and_unknown_cases(self):
        def transfer(code, document="Перемещение товаров 2 от 01.01.2026"):
            return LedgerRow(period="p", document=document, nomenclature="A", opening_quantity=Decimal(0),
                             opening_amount=Decimal(0), turnover_quantity=Decimal(1), turnover_amount=Decimal(1),
                             closing_quantity=Decimal(1), closing_amount=Decimal(1), warehouse_code=code,
                             warehouse=code, organization="Org")
        context = [transfer("T1"), transfer("T2")]
        result = enrich_transfer_warehouse_codes(context[:1], [
            Warehouse("T1", "A", accounting_name="Accounting A"),
            Warehouse("T2", "B", accounting_name="Accounting B"),
        ], context_rows=context)[0]
        self.assertEqual(result.transfer_scope, "external")

        single = enrich_transfer_warehouse_codes([transfer("T1")], [
            Warehouse("T1", "A", accounting_name="Accounting A"),
        ], context_rows=[transfer("T1")])[0]
        self.assertEqual(single.transfer_scope, "")

    def test_transfer_scope_rejects_ambiguous_or_more_than_two_context_codes(self):
        def transfer(code):
            return LedgerRow(period="p", document="Перемещение товаров 3 от 01.01.2026", nomenclature="A",
                             opening_quantity=Decimal(0), opening_amount=Decimal(0), turnover_quantity=Decimal(1),
                             turnover_amount=Decimal(1), closing_quantity=Decimal(1), closing_amount=Decimal(1), warehouse_code=code,
                             warehouse=code, organization="Org")
        context = [transfer(code) for code in ("T1", "T2", "T3")]
        warehouses = [Warehouse(code, code, accounting_name=code) for code in ("T1", "T2", "T3")]
        result = enrich_transfer_warehouse_codes(context[:1], warehouses, context_rows=context)[0]
        self.assertEqual(result.transfer_scope, "")
        duplicate_directory = [Warehouse("T1", "A", accounting_name="A"),
                              Warehouse("T1", "A duplicate", accounting_name="B"),
                              Warehouse("T2", "B", accounting_name="B")]
        result = enrich_transfer_warehouse_codes(context[:1], duplicate_directory,
                                                 context_rows=[transfer("T1"), transfer("T2")])[0]
        self.assertEqual(result.transfer_scope, "")

    def test_transfer_scope_does_not_infer_when_explicit_endpoint_contradicts_context(self):
        first = LedgerRow(period="p", document="Перемещение товаров 4 от 01.01.2026", nomenclature="A",
                          opening_quantity=Decimal(0), opening_amount=Decimal(0), turnover_quantity=Decimal(1),
                          turnover_amount=Decimal(1), closing_quantity=Decimal(1), closing_amount=Decimal(1),
                          warehouse_code="T1", organization="Org", sender_warehouse_code="T3")
        second = replace(first, warehouse_code="T2", sender_warehouse_code="")
        result = enrich_transfer_warehouse_codes([first], [
            Warehouse("T1", "A", accounting_name="A"), Warehouse("T2", "B", accounting_name="B"),
            Warehouse("T3", "C", accounting_name="C"),
        ], context_rows=[first, second])[0]
        self.assertEqual(result.transfer_scope, "")

    def test_transfer_scope_does_not_mix_same_named_organizations_with_different_inns(self):
        def transfer(code, inn):
            return LedgerRow(period="p", document="Перемещение товаров 5 от 01.01.2026", nomenclature="A",
                             opening_quantity=Decimal(0), opening_amount=Decimal(0), turnover_quantity=Decimal(1),
                             turnover_amount=Decimal(1), closing_quantity=Decimal(1), closing_amount=Decimal(1),
                             warehouse_code=code, organization="Org", organization_inn=inn)
        context = [transfer("T1", "111"), transfer("T2", "222")]
        enriched = enrich_transfer_warehouse_codes(context[:1], [
            Warehouse("T1", "A", accounting_name="Accounting A"),
            Warehouse("T2", "B", accounting_name="Accounting B"),
        ], context_rows=context)[0]
        self.assertEqual(enriched.transfer_scope, "")

    def test_transfer_scope_rejects_mismatched_existing_endpoint_code_and_name(self):
        row = LedgerRow(period="p", document="Перемещение товаров 6 от 01.01.2026", nomenclature="A",
                        opening_quantity=Decimal(0), opening_amount=Decimal(0), turnover_quantity=Decimal(1),
                        turnover_amount=Decimal(1), closing_quantity=Decimal(1), closing_amount=Decimal(1),
                        sender_warehouse_code="T1", sender_warehouse="Warehouse B",
                        sender_accounting_warehouse="Account A", receiver_accounting_warehouse="Account B")
        result = enrich_transfer_warehouse_codes([row], [
            Warehouse("T1", "Warehouse A", accounting_name="Account A"),
            Warehouse("T2", "Warehouse B", accounting_name="Account B"),
        ], context_rows=[replace(row, warehouse_code="T1"), replace(row, warehouse_code="T2")])[0]
        self.assertEqual(result.transfer_scope, "")

    def test_manual_mapping_preserves_id_and_extends_automatic_group(self):
        manual = WarehouseMappingConfig(
            "legacy-main",
            "Legacy",
            {"trade": ["T1", "T3"], "buh2019": "B19", "buh2026": "B26"},
        )
        directories = {
            "trade": [
                Warehouse("T1", "Section 1", accounting_name="Main"),
                Warehouse("T2", "Section 2", accounting_name="Main"),
                Warehouse("T3", "Manual exception"),
            ],
            "buh2019": [Warehouse("B19", "Main")],
            "buh2026": [Warehouse("B26", "Main")],
        }

        resolution = resolve_warehouse_mappings(settings(manual), directories)

        self.assertEqual(len(resolution.mappings), 1)
        self.assertEqual(resolution.mappings[0].mapping_id, "legacy-main")
        self.assertEqual(resolution.mappings[0].label, "Main")
        self.assertEqual(resolution.mappings[0].codes_for("trade"), ("T1", "T2", "T3"))
        self.assertEqual(resolution.diagnostics["blank_trade_count"], 0)

    def test_ambiguous_accounting_name_is_reported_and_not_mapped(self):
        directories = {
            "trade": [Warehouse("T1", "Trade", accounting_name="Main")],
            "buh2019": [Warehouse("B1", "Main"), Warehouse("B2", "main")],
            "buh2026": [Warehouse("B3", "Main")],
        }

        resolution = resolve_warehouse_mappings(settings(), directories)

        self.assertEqual(len(resolution.mappings), 1)
        self.assertEqual(resolution.mappings[0].codes_for("buh2019"), ())
        self.assertEqual(resolution.mappings[0].codes_for("buh2026"), ("B3",))
        self.assertEqual(resolution.mappings[0].codes_for("buh2019"), ())

    def test_conflicting_manual_mapping_is_rejected(self):
        manual = WarehouseMappingConfig(
            "wrong",
            "Wrong",
            {"trade": "T1", "buh2019": "B2", "buh2026": "B4"},
        )
        directories = {
            "trade": [Warehouse("T1", "Trade", accounting_name="Main")],
            "buh2019": [Warehouse("B1", "Main"), Warehouse("B2", "Other")],
            "buh2026": [Warehouse("B3", "Main"), Warehouse("B4", "Other")],
        }

        with self.assertRaisesRegex(ConfigurationError, "противоречит СкладБухгалтерия"):
            resolve_warehouse_mappings(settings(manual), directories)
