# Optional tests for local-only pilot utilities. Public CI remains portable.
import importlib.util as _import_util
import unittest as _unittest
_missing_local_tools = [name for name in (
    'scripts.pilot_review_manifest',
) if _import_util.find_spec(name) is None]
if _missing_local_tools:
    raise _unittest.SkipTest("Local pilot utilities are not shipped: " + ", ".join(_missing_local_tools))

import csv
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.models import LedgerRow
from scripts.pilot_review_manifest import build_review_manifest, write_review_manifest


def make_row(document, *, product="Widget", code="P1", article="A1", warehouse="Main",
             wh_code="W1", qty="1", doc_type="Реализация товаров и услуг",
             issued="INV-1", party="", comment="", waybill="", counterparty="",
             order="", sender="", receiver="", mapping="main", is_import=""):
    return LedgerRow(
        period="01.08.2026", document=document, nomenclature=product,
        opening_quantity=Decimal("0"), opening_amount=Decimal("0"),
        turnover_quantity=Decimal(qty), turnover_amount=Decimal("0"),
        closing_quantity=Decimal("0"), closing_amount=Decimal("0"),
        nomenclature_code=code, article=article, warehouse=warehouse,
        warehouse_code=wh_code, warehouse_mapping_id=mapping,
        unit="шт", document_type=doc_type, issued_invoice_number=issued,
        party=party, party_comment=comment, party_waybill=waybill,
        counterparty=counterparty, supplier_order=order,
        sender_warehouse=sender, receiver_warehouse=receiver,
        is_import=is_import,
    )


class ReviewManifestTests(TestCase):
    def test_manifest_samples_per_document_type_and_keeps_pending_review_fields(self):
        rows = [make_row(f"Реализация товаров и услуг R{i} от 01.08.2026", issued=f"ДЖ{i + 1:04}") for i in range(7)]
        rows += [make_row(f"Поступление товаров G{i} от 01.08.2026", doc_type="Поступление товаров") for i in range(2)]

        records = build_review_manifest({"buh": rows}, per_family=5)

        samples = [r for r in records if "sample" in r["selection_reason"]]
        self.assertEqual(len(samples), 7)
        self.assertEqual(sum("Реализация" in r["source_document"] for r in samples), 5)
        self.assertEqual(sum("Поступление" in r["source_document"] for r in samples), 2)
        self.assertTrue(all(r["reviewer"] == r["review_date"] == r["review_comment"] == "" for r in records))
        self.assertTrue(all(r["review_status"] == "Не проверено" for r in records))
        self.assertTrue(all("один товар" in r["selection_reason"] for r in samples))


    def test_manifest_includes_anomalies_for_duplicates_missing_keys_and_forced_controls(self):
        doc = "Реализация товаров и услуг S1 от 01.08.2026"
        duplicate = make_row(doc)
        rows = [duplicate, duplicate, make_row("Реализация товаров и услуг S2 от 02.08.2026", code="", article=""),
            make_row("Реализация товаров и услуг S3 от 03.08.2026", issued=""),
            make_row("unparsed source document", issued="")]

        records = build_review_manifest({"trade": rows}, forced_documents={"trade": {"Реализация товаров и услуг S2 от 02.08.2026"}})

        self.assertEqual(len(records), len(rows))  # duplicate physical rows retain distinct source indexes
        reasons = [r["diagnostic"] for r in records]
        self.assertTrue(any("повтор" in reason.casefold() or "дубликат" in reason.casefold() for reason in reasons))
        self.assertTrue(any("код" in reason.casefold() and "артикул" in reason.casefold() for reason in reasons))
        self.assertTrue(any("счёт-фактур" in reason.casefold() or "счет-фактур" in reason.casefold() for reason in reasons))
        self.assertTrue(any("не разобран" in reason.casefold() or "unparsed" in reason.casefold() for reason in reasons))
        forced = next(r for r in records if r["source_document"].endswith("S2 от 02.08.2026"))
        self.assertIn("forced", forced["selection_reason"])
        self.assertEqual(forced["source_row_index"], "3")


    def test_invoice_key_duplicates_across_dates_and_import_source_party_rules(self):
        a = make_row("Реализация товаров и услуг X1 от 01.08.2026", issued="ДЖ0007")
        b = make_row("Реализация товаров и услуг X2 от 02.08.2026", issued="ДЖ7")
        receipt = make_row("Поступление товаров G1 от 03.08.2026", doc_type="Поступление товаров",
                       party="Партия УТ", comment="Другая партия", counterparty="Supplier",
                       order="Заказ 1", is_import="Да")
        records = build_review_manifest({"trade": [a, b], "buh2019": [receipt]})
        self.assertTrue(all("счёт-фактур" in r["diagnostic"].casefold() or "счет-фактур" in r["diagnostic"].casefold()
                        for r in records[:2]))
        import_record = next(r for r in records if "G1" in r["source_document"])
        self.assertTrue("парт" in import_record["diagnostic"].casefold() or "ручн" in import_record["diagnostic"].casefold())


    def test_writer_uses_private_bom_semicolon_csv_and_neutralizes_formulas(self):
        records = build_review_manifest({"buh": [make_row("=HYPERLINK(1)", product="+SUM(A1)")]})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "review.csv"
            write_review_manifest(path, records)
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            parsed = list(csv.DictReader(raw.decode("utf-8-sig").splitlines(), delimiter=";"))
            self.assertTrue(parsed[0]["source_document"].startswith("'"))
            self.assertTrue(parsed[0]["product_name"].startswith("'"))
            self.assertEqual(parsed[0]["signed_turnover_quantity"], "1")

    def test_writer_keeps_negative_quantity_numeric_while_protecting_text(self):
        records = build_review_manifest({"trade": [make_row(
            "Реализация товаров и услуг ДЖ1 от 01.08.2026", issued="", qty="-537",
            product="-formula?",)]})
        record = records[0]
        self.assertEqual(record["signed_turnover_quantity"], "-537")
        self.assertTrue(record["product_name"].startswith("'"))

    def test_invoice_keys_canonicalize_dzh_zeroes_and_flag_unknown_or_multiple_values(self):
        one = make_row("Реализация товаров и услуг S1 от 01.08.2026", issued="ДЖ000001589")
        two = make_row("Реализация товаров и услуг S2 от 02.08.2026", issued="ДЖ1589")
        unknown = make_row("Реализация товаров и услуг S3 от 03.08.2026", issued="INV-3")
        first = make_row("Реализация товаров и услуг S4 от 04.08.2026", issued="ДЖ44")
        second = make_row("Реализация товаров и услуг S4 от 04.08.2026", issued="ДЖ45")
        records = build_review_manifest({"trade": [one, two, unknown, first, second]})
        duplicate = [r for r in records if r["source_document"].endswith(("S1 от 01.08.2026", "S2 от 02.08.2026"))]
        self.assertTrue(all("дубликат" in r["diagnostic"].casefold() for r in duplicate))
        self.assertIn("формат", records[2]["diagnostic"].casefold())
        self.assertTrue(all("несколько" in r["diagnostic"].casefold() for r in records[3:]))

    def test_transfer_requires_both_endpoint_codes_and_import_checks_only_ut_and_buh2019(self):
        transfer = make_row("Перемещение товаров T1 от 01.08.2026", doc_type="Перемещение товаров",
                            sender="Named source", receiver="Named destination")
        receipt = make_row("Поступление товаров G1 от 01.08.2026", doc_type="Поступление товаров",
                           is_import="Да", party="", comment="", waybill="", counterparty="Supplier")
        records = build_review_manifest({"trade": [transfer, receipt], "buh2019": [receipt], "buh2026": [receipt]})
        transfer_record = next(r for r in records if "T1" in r["source_document"])
        self.assertIn("код", transfer_record["diagnostic"].casefold())
        self.assertTrue(any(r["source_base"] == "trade" and "парт" in r["diagnostic"].casefold() for r in records))
        self.assertTrue(any(r["source_base"] == "buh2019" and "парт" in r["diagnostic"].casefold() for r in records))
        self.assertFalse(any(r["source_base"] == "buh2026" and r["diagnostic"] for r in records))

    def test_sampling_takes_first_five_documents_including_anomalies_and_forced_rows(self):
        rows = [make_row(f"Реализация товаров и услуг S{i} от 01.08.2026", issued="" if i == 0 else f"ДЖ{i+1}")
                for i in range(7)]
        forced_doc = rows[5].document
        records = build_review_manifest({"trade": rows}, forced_documents={"trade": {forced_doc}})
        sampled = [r for r in records if "sample" in r["selection_reason"]]
        self.assertEqual([r["source_document"] for r in sampled], [row.document for row in rows[:5]])
        self.assertIn("forced", next(r for r in records if r["source_document"] == forced_doc)["selection_reason"])

    def test_key_details_include_resolved_and_raw_import_party_fields_and_organization(self):
        row = make_row("Поступление товаров G1 от 01.08.2026", doc_type="Поступление товаров",
                       is_import="Да", party="Партия 42", comment="Партия 42",
                       waybill="Партия 42/УПД 8", counterparty="Supplier")
        row = LedgerRow(**{**row.__dict__, "organization": "ООО Компания", "organization_inn": "123"})
        record = build_review_manifest({"buh2019": [row]})[0]
        for value in ("resolved_party=Партия 42", "party=Партия 42", "party_comment=Партия 42",
                      "party_waybill=Партия 42/УПД 8", "organization=ООО Компания", "organization_inn=123"):
            self.assertIn(value, record["key_details"])

    def test_import_mapping_and_buh2019_warehouse_detect_import_without_flag(self):
        ut = make_row("Поступление товаров U1 от 01.08.2026", doc_type="Поступление товаров",
                      is_import="", mapping="import-map", party="", order="", counterparty="Supplier")
        buh = make_row("Поступление товаров B1 от 01.08.2026", doc_type="Поступление товаров",
                       is_import="", wh_code="БД-000003", party="", counterparty="Supplier")
        records = build_review_manifest({"trade": [ut], "buh2019": [buh]}, import_mapping_ids={"import-map"})
        self.assertTrue(any(r["source_base"] == "trade" and "парт" in r["diagnostic"].casefold() for r in records))
        self.assertTrue(any(r["source_base"] == "buh2019" and "парт" in r["diagnostic"].casefold() for r in records))

    def test_duplicate_ordinary_document_keys_mark_all_distinct_source_groups(self):
        first = make_row("Поступление товаров R-17 от 01.08.2026", doc_type="Поступление товаров",
                         mapping="main", wh_code="W1")
        second = make_row("Поступление товаров r-17 от 01.08.2026", doc_type="Поступление товаров",
                          mapping="main", wh_code="W1")
        second = LedgerRow(**{**second.__dict__, "organization_inn": "3164864916"})
        records = build_review_manifest({"buh2019": [first, second]})
        self.assertEqual(len(records), 2)
        self.assertTrue(all("дубликат ключа документа" in r["diagnostic"].casefold() for r in records))

    def test_eligible_import_receipts_are_excluded_from_ordinary_document_duplicate_check(self):
        first = make_row("Поступление товаров I-1 от 01.08.2026", doc_type="Поступление товаров",
                         mapping="import-map", wh_code="W1")
        second = make_row("Поступление товаров i-1 от 01.08.2026", doc_type="Поступление товаров",
                          mapping="import-map", wh_code="W1")
        records = build_review_manifest({"trade": [first, second]}, import_mapping_ids={"import-map"})
        self.assertTrue(all("дубликат ключа документа" not in r["diagnostic"].casefold() for r in records))

    def test_missing_document_key_components_are_explicit_manual_anomalies(self):
        row = make_row("Поступление товаров K1 от 01.08.2026", doc_type="Поступление товаров",
                       mapping="", wh_code="", warehouse="")
        record = build_review_manifest({"buh2019": [row]})[0]
        self.assertIn("ключа документа", record["diagnostic"].casefold())

    def test_unparsed_document_reports_missing_canonical_family_number_and_day(self):
        row = make_row("Not a parsed document")
        row = LedgerRow(**{**row.__dict__, "period": "Август 2026"})
        record = build_review_manifest({"buh2019": [row]})[0]
        self.assertIn("тип", record["diagnostic"].casefold())
        self.assertIn("номер", record["diagnostic"].casefold())
        self.assertIn("день", record["diagnostic"].casefold())


