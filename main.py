import logging
import threading
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import ConfigurationError, OneCBaseConfig, Settings, WarehouseMappingConfig, load_settings
from app.models import LedgerRow, ReconciliationResult, Warehouse
from app.onec_client import OneCClient, OneCClientError
from app.reconciliation import reconcile
from app.reporting import build_action_excel_report
from app.warehouse_mapping import (
    WarehouseMappingResolution,
    enrich_transfer_warehouse_codes,
    resolve_warehouse_mappings,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
INDEX_HTML_PATH = Path(__file__).with_name("index.html")

app = FastAPI(title="Сверка документов 1С")


class ReconcileRequest(BaseModel):
    start_date: date = Field(..., description="Начальная дата периода включительно")
    end_date: date = Field(..., description="Конечная дата периода включительно")
    warehouses: list[str] = Field(default_factory=list, description="Список складов для фильтра сверки")

    @field_validator("warehouses")
    @classmethod
    def normalize_warehouses(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in value or []:
            normalized = " ".join(str(item).strip().split())
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    @model_validator(mode="after")
    def validate_date_range(self) -> "ReconcileRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date должен быть больше или равен start_date")
        return self


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/warehouses")
def warehouses() -> dict[str, object]:
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    client = OneCClient(
        timeout_seconds=settings.timeout_seconds,
        max_attempts=settings.retry_max_attempts,
        backoff_seconds=settings.retry_backoff_seconds,
    )
    try:
        directories = _fetch_warehouse_directories(client, settings)
        return _build_warehouse_response(settings, directories)
    except OneCClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ConfigurationError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


_reconciliation_slots: threading.BoundedSemaphore | None = None
_reconciliation_slots_size = 0
_reconciliation_slots_lock = threading.Lock()


def _acquire_reconciliation_slot(limit: int) -> threading.BoundedSemaphore | None:
    """Занять слот на выполнение сверки, не дожидаясь освобождения.

    Пока блокирующие маршруты выполнялись в цикле событий, сверки шли строго
    по одной. После переноса в пул потоков одновременных сверок может быть
    столько, сколько потоков в пуле, и каждая тянет из 1С полную выгрузку под
    одной сервисной учётной записью. Слоты ограничивают это число и сразу
    отвечают отказом вместо того, чтобы копить очередь и удерживать потоки.

    Возвращается сам семафор, а не признак успеха: настройки перечитываются на
    каждый запрос, поэтому изменение лимита в `.env` пересоздаёт семафор. Слот
    нужно вернуть тому объекту, у которого он был взят, иначе уже идущие сверки
    освободили бы новый семафор и его граница перестала бы соблюдаться.
    """
    global _reconciliation_slots, _reconciliation_slots_size
    with _reconciliation_slots_lock:
        if _reconciliation_slots is None or _reconciliation_slots_size != limit:
            _reconciliation_slots = threading.BoundedSemaphore(limit)
            _reconciliation_slots_size = limit
        semaphore = _reconciliation_slots
    return semaphore if semaphore.acquire(blocking=False) else None


def _release_reconciliation_slot(semaphore: threading.BoundedSemaphore) -> None:
    semaphore.release()


@app.post("/reconcile")
def run_reconciliation(payload: ReconcileRequest) -> Response:
    try:
        return _run_reconciliation(payload)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "Unexpected reconciliation error: start_date=%s end_date=%s warehouses=%s",
            payload.start_date,
            payload.end_date,
            len(payload.warehouses),
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Не удалось сформировать Excel-отчёт из-за внутренней ошибки. "
                "Повторите запрос. Подробности записаны в журнал сервера."
            ),
        ) from exc


def _run_reconciliation(payload: ReconcileRequest) -> Response:
    logger.info(
        "Reconciliation started: start_date=%s end_date=%s warehouses=%s",
        payload.start_date,
        payload.end_date,
        len(payload.warehouses),
    )
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        logger.exception("Reconciliation configuration error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    slot = _acquire_reconciliation_slot(settings.max_concurrent_reconciliations)
    if slot is None:
        logger.warning(
            "Reconciliation rejected: concurrency limit reached limit=%s",
            settings.max_concurrent_reconciliations,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Сервис уже выполняет максимальное число сверок "
                f"({settings.max_concurrent_reconciliations}). Повторите запрос позже."
            ),
            headers={"Retry-After": "60"},
        )
    try:
        return _reconcile(payload, settings)
    finally:
        _release_reconciliation_slot(slot)


def _reconcile(payload: ReconcileRequest, settings: Settings) -> Response:
    client = OneCClient(
        timeout_seconds=settings.timeout_seconds,
        max_attempts=settings.retry_max_attempts,
        backoff_seconds=settings.retry_backoff_seconds,
    )

    selected_mappings: list[WarehouseMappingConfig] = []
    trade_warehouse_directory: list[Warehouse] = []
    all_mode_mapping_error: str | None = None
    if payload.warehouses:
        if not settings.trade_base.supports_warehouse_filter:
            raise HTTPException(status_code=422, detail=f"Фильтр складов не поддерживается: {settings.trade_base.name}")
        configured = {mapping.mapping_id: mapping for mapping in settings.warehouse_mappings}
        if all(mapping_id in configured for mapping_id in payload.warehouses):
            unsupported = [base.name for base in settings.tax_bases
                           if not base.supports_warehouse_filter and any(
                               configured[mapping_id].codes_for(base.base_id) for mapping_id in payload.warehouses)]
            if unsupported:
                raise HTTPException(status_code=422, detail=f"Фильтр складов не поддерживается: {', '.join(unsupported)}")
        try:
            directories = _fetch_warehouse_directories(client, settings)
            trade_warehouse_directory = directories.get(settings.trade_base.base_id, [])
            resolution = resolve_warehouse_mappings(settings, directories)
            mapping_by_id = {mapping.mapping_id: mapping for mapping in resolution.mappings}
            unknown = [mapping_id for mapping_id in payload.warehouses if mapping_id not in mapping_by_id]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Неизвестные соответствия складов: {', '.join(unknown)}. "
                        "Обновите список складов."
                    ),
                )
            selected_mappings = [mapping_by_id[mapping_id] for mapping_id in payload.warehouses]
            unsupported = [base.name for base in _all_bases(settings)
                           if any(mapping.codes_for(base.base_id) for mapping in selected_mappings)
                           and not base.supports_warehouse_filter]
            if unsupported:
                raise HTTPException(status_code=422, detail=f"Фильтр складов не поддерживается: {', '.join(unsupported)}")
            _validate_mapping_codes(selected_mappings, directories, settings)
        except OneCClientError as exc:
            logger.exception("Warehouse directory loading error")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ConfigurationError as exc:
            logger.exception("Warehouse mapping configuration error")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    else:
        try:
            directories = _fetch_warehouse_directories(client, settings, skip_unconfigured=True)
            trade_warehouse_directory = directories.get(settings.trade_base.base_id, [])
            resolution = resolve_warehouse_mappings(settings, directories)
            selected_mappings = list(resolution.mappings)
        except OneCClientError as exc:
            all_mode_mapping_error = (
                "Не удалось загрузить справочник складов: "
                f"{exc}. Склад и соответствие требуют ручной проверки."
            )
            logger.error("Warehouse directory unavailable in all-warehouse mode: error=%s", exc)
        except ConfigurationError as exc:
            logger.exception("Warehouse mapping configuration error")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    trade_candidate_rows: list[LedgerRow] = []
    try:
        trade_rows = _fetch_rows_for_base(
            client, settings.trade_base, payload, selected_mappings,
            preserve_unmapped=not payload.warehouses,
            mapping_error=all_mode_mapping_error,
            source_rows=trade_candidate_rows,
        )
    except OneCClientError as exc:
        logger.exception("Reconciliation data loading error")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    trade_rows = enrich_transfer_warehouse_codes(trade_rows, trade_warehouse_directory,
                                                context_rows=trade_candidate_rows)

    trade_export_error = _document_export_error(settings.trade_base.name, trade_rows)
    if trade_export_error:
        logger.error("Trade base export is invalid: error=%s", trade_export_error)
        raise HTTPException(status_code=502, detail=trade_export_error)

    tax_exports: dict[str, list[LedgerRow]] = {}
    tax_candidate_exports: dict[str, list[LedgerRow]] = {}
    source_errors: dict[str, str] = {}
    for tax_base in settings.tax_bases:
        if payload.warehouses and not any(mapping.codes_for(tax_base.base_id) for mapping in selected_mappings):
            source_errors[tax_base.name] = (
                f"Для выбранных складов нет однозначного соответствия в справочнике {tax_base.name}. "
                "Проверка этой базы не выполнена."
            )
            continue
        candidate_rows: list[LedgerRow] = []
        try:
            rows = _fetch_rows_for_base(
                client, tax_base, payload, selected_mappings,
                preserve_unmapped=not payload.warehouses,
                mapping_error=all_mode_mapping_error,
                source_rows=candidate_rows,
            )
        except OneCClientError as exc:
            source_errors[tax_base.name] = str(exc)
            logger.error("Accounting base was not checked: base=%s error=%s", tax_base.name, exc)
            continue
        export_error = _document_export_error(tax_base.name, rows)
        if export_error:
            source_errors[tax_base.name] = export_error
            logger.error("Accounting base was not checked: base=%s error=%s", tax_base.name, export_error)
            continue
        tax_exports[tax_base.name] = rows
        tax_candidate_exports[tax_base.name] = candidate_rows

    period = _format_period(payload.start_date, payload.end_date)
    mapping_errors = {
        base.name: {
            mapping.mapping_id: (
                f"В свойстве склада УТ указан бухгалтерский склад «{mapping.label}», "
                f"но в справочнике {base.name} он не найден однозначно. Проверить соответствие склада."
            )
            for mapping in selected_mappings if not mapping.codes_for(base.base_id)
        }
        for base in settings.tax_bases
    }
    if tax_exports:
        checked_result = reconcile(
            trade_rows=trade_rows,
            tax_exports=tax_exports,
            period=period,
            amount_tolerance=settings.amount_tolerance,
            quantity_tolerance=settings.quantity_tolerance,
            start_date=payload.start_date,
            end_date=payload.end_date,
            trade_candidate_rows=trade_candidate_rows,
            tax_candidate_exports=tax_candidate_exports,
            **({"warehouse_mapping_errors": mapping_errors} if any(mapping_errors.values()) else {}),
        )
    else:
        checked_result = ReconciliationResult(discrepancies=[], summaries=[])
    result = ReconciliationResult(
        discrepancies=checked_result.discrepancies,
        summaries=checked_result.summaries,
        source_errors=source_errors,
        start_date=payload.start_date,
        end_date=payload.end_date,
        notices=checked_result.notices,
    )
    logger.info(
        "Reconciliation completed: start_date=%s end_date=%s trade_rows=%s checked_bases=%s "
        "unchecked_bases=%s discrepancies=%s",
        payload.start_date,
        payload.end_date,
        len(trade_rows),
        len(tax_exports),
        len(source_errors),
        len(result.discrepancies),
    )
    content = build_action_excel_report(result)
    filename = (
        f"Список_документов_для_исправления_"
        f"{payload.start_date.isoformat()}_{payload.end_date.isoformat()}.xlsx"
    )
    fallback_filename = f"report_{payload.start_date.isoformat()}_{payload.end_date.isoformat()}.xlsx"

    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{fallback_filename}"; filename*=UTF-8\'\'{quote(filename)}'
            ),
            "X-Unchecked-Base-Count": str(len(source_errors)),
        },
    )


def _format_period(start_date: date, end_date: date) -> str:
    return f"{start_date:%d.%m.%Y}–{end_date:%d.%m.%Y}"


def _document_export_error(base_name: str, rows: list[LedgerRow]) -> str | None:
    if not rows:
        return (
            f"Выгрузка «{base_name}» вернула пустой ответ без подтверждения полноты. "
            "База не проверена; отсутствие документов не установлено."
        )
    rows_without_document = [
        row
        for row in rows
        if not row.document and (row.turnover_quantity != 0 or row.turnover_amount != 0)
    ]
    if not rows_without_document:
        return None
    return (
        f"Выгрузка «{base_name}» содержит {len(rows_without_document)} строк движений "
        "с ненулевым оборотом и пустым полем «Документ». Остатки без движения "
        "могут не содержать документ, но строки движений должны его содержать."
    )


def _all_bases(settings: Settings) -> list[OneCBaseConfig]:
    return [settings.trade_base, *settings.tax_bases]


def _fetch_warehouse_directories(
    client: OneCClient,
    settings: Settings,
    *,
    skip_unconfigured: bool = False,
) -> dict[str, list[Warehouse]]:
    return {
        base.base_id: (
            [] if skip_unconfigured and not base.warehouses_url else client.fetch_warehouses(base)
        )
        for base in _all_bases(settings)
    }


def _validate_mapping_codes(
    mappings: list[WarehouseMappingConfig],
    directories: dict[str, list[Warehouse]],
    settings: Settings,
) -> None:
    available = {
        base_id: {warehouse.code for warehouse in warehouses}
        for base_id, warehouses in directories.items()
    }
    base_names = {base.base_id: base.name for base in _all_bases(settings)}
    for mapping in mappings:
        for base_id in mapping.codes:
            for code in mapping.codes_for(base_id):
                if code not in available.get(base_id, set()):
                    raise OneCClientError(
                        f"Склад {code} из соответствия {mapping.mapping_id} отсутствует в базе "
                        f"«{base_names.get(base_id, base_id)}»"
                    )


def _build_warehouse_response(
    settings: Settings,
    directories: dict[str, list[Warehouse]],
    resolution: WarehouseMappingResolution | None = None,
) -> dict[str, object]:
    resolution = resolution or resolve_warehouse_mappings(settings, directories)
    mappings_config = list(resolution.mappings)
    _validate_mapping_codes(mappings_config, directories, settings)
    warehouse_by_code = {
        base_id: {warehouse.code: warehouse for warehouse in warehouses}
        for base_id, warehouses in directories.items()
    }
    bases = [
        {
            "id": base.base_id,
            "name": base.name,
            "warehouses": [
                {
                    "code": warehouse.code,
                    "name": warehouse.name,
                    "group": warehouse.group,
                    "accounting_name": warehouse.accounting_name,
                }
                for warehouse in directories[base.base_id]
            ],
        }
        for base in _all_bases(settings)
    ]
    mappings = [
        {
            "id": mapping.mapping_id,
            "label": mapping.label,
            "sources": {
                base.base_id: [
                    {
                        "code": code,
                        "name": warehouse_by_code[base.base_id][code].name,
                        "group": warehouse_by_code[base.base_id][code].group,
                    }
                    for code in mapping.codes_for(base.base_id)
                ]
                for base in _all_bases(settings)
            },
        }
        for mapping in mappings_config
    ]
    return {
        "bases": bases,
        "warehouses": mappings,
        "mapping_diagnostics": resolution.diagnostics,
    }


def _fetch_rows_for_base(
    client: OneCClient,
    base: OneCBaseConfig,
    payload: ReconcileRequest,
    mappings: list[WarehouseMappingConfig],
    *,
    preserve_unmapped: bool = False,
    mapping_error: str | None = None,
    source_rows: list[LedgerRow] | None = None,
) -> list[LedgerRow]:
    mapping_by_code: dict[str, str] = {}
    for mapping in mappings:
        for warehouse_code in mapping.codes_for(base.base_id):
            existing = mapping_by_code.get(warehouse_code)
            if existing is not None and existing != mapping.mapping_id:
                raise ConfigurationError(
                    f"Склад {warehouse_code} базы {base.base_id} входит в несколько соответствий"
                )
            mapping_by_code[warehouse_code] = mapping.mapping_id

    if len(mapping_by_code) == 1 and not preserve_unmapped and not mapping_error:
        warehouse_code, mapping_id = next(iter(mapping_by_code.items()))
        rows = client.fetch_rows(
            base,
            payload.start_date,
            payload.end_date,
            warehouse=warehouse_code,
            warehouse_mapping_id=mapping_id,
        )
        if source_rows is not None:
            source_rows.extend(rows)
        return rows

    rows = client.fetch_rows(base, payload.start_date, payload.end_date)
    if source_rows is not None:
        source_rows.extend(rows)
    if not mappings and not preserve_unmapped and not mapping_error:
        return rows
    if not preserve_unmapped and not mapping_error:
        return [
            replace(row, warehouse_mapping_id=mapping_by_code[row.warehouse_code])
            for row in rows
            if row.warehouse_code in mapping_by_code
        ]

    result: list[LedgerRow] = []
    for row in rows:
        mapping_id = mapping_by_code.get(row.warehouse_code)
        if mapping_error:
            result.append(replace(row, warehouse_mapping_error=mapping_error))
        elif mapping_id:
            result.append(replace(row, warehouse_mapping_id=mapping_id))
        else:
            result.append(replace(
                row,
                warehouse_mapping_error=(
                    f"Склад {row.warehouse_code or row.warehouse or '(не указан)'} "
                    f"базы «{base.name}» не входит в настроенные соответствия. "
                    "Требуется ручная проверка."
                ),
            ))
    return result
