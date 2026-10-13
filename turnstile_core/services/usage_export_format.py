from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from turnstile_core.persistence.repository_contract import QueryRepository
from turnstile_core.persistence.repository_support import UsageFilters

CSV_COLUMNS = (
    "usage_id",
    "request_id",
    "correlation_id",
    "timestamp",
    "team",
    "organization_id",
    "organization_name",
    "department_id",
    "department_name",
    "project_id",
    "project_name",
    "user_id",
    "user_name",
    "agent_id",
    "agent_name",
    "workflow",
    "run_id",
    "turn_index",
    "model_id",
    "model_name",
    "provider",
    "runtime",
    "request_source",
    "input_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "total_tokens",
    "latency_ms",
    "status_code",
    "input_price_per_million_usd",
    "cached_price_per_million_usd",
    "cache_write_price_per_million_usd",
    "output_price_per_million_usd",
    "estimated_cost_usd",
    "usage_estimated",
    "ingest_source",
    "ingest_error",
    "reconciled_at",
    "budget_admission",
    "model_admission",
    "error_message",
)

_REPORT_DIMENSIONS = (
    ("organization", "组织用量分布"),
    ("department", "部门用量分布"),
    ("project", "项目用量分布"),
    ("agent", "智能体用量分布"),
    ("model", "模型用量分布"),
)


def _safe_csv_value(value: object) -> object:
    if isinstance(value, str) and value.lstrip(" \t\r\n")[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    if isinstance(value, datetime):
        return value.isoformat()
    return "" if value is None else value


def _markdown_cell(value: object) -> str:
    return (
        str(value or "—")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _money(value: object) -> str:
    return f"${float(str(value or 0)):,.6f}"


def _report_markdown(
    repository: QueryRepository,
    from_: datetime,
    to: datetime,
    filters: UsageFilters,
) -> str:
    overview = repository.executive_overview(from_, to, filters)
    totals = overview["totals"]
    changes = overview["changes_percent"]
    lines = [
        "# Turnstile 用量与费用分析",
        "",
        f"- 统计区间（含起点、不含终点）：{from_.isoformat()} 至 {to.isoformat()}",
        f"- 报告生成时间：{overview['generated_at'].isoformat()}",
        "- 数据范围：Turnstile 记录的 APIM 模型请求元数据。",
        "- 费用口径：按入库时模型价格计算的估算费用，单位 USD；不包含 Azure 资源实际账单。",
        "- 隐私口径：不采集或导出请求正文及模型回复。",
        f"- 归属筛选：{_filter_description(filters)}",
        "",
        "## 汇总",
        "",
        "| 指标 | 当前区间 | 较上一同期区间 |",
        "| --- | ---: | ---: |",
        (
            f"| 请求数 | {int(totals['total_requests']):,} | "
            f"{_change(changes.get('total_requests'))} |"
        ),
        (
            f"| Token 总量 | {int(totals['total_tokens']):,} | "
            f"{_change(changes.get('total_tokens'))} |"
        ),
        (
            f"| 估算费用（USD） | {_money(totals['estimated_cost'])} | "
            f"{_change(changes.get('estimated_cost'))} |"
        ),
        (
            f"| 平均延迟 | {float(totals['average_latency_ms']):,.1f} ms | "
            f"{_change(changes.get('average_latency_ms'))} |"
        ),
        (
            f"| P50 / P95 / P99 延迟 | {float(totals['p50_latency_ms']):,.1f} / "
            f"{float(totals['p95_latency_ms']):,.1f} / "
            f"{float(totals['p99_latency_ms']):,.1f} ms | — |"
        ),
        f"| 错误率 | {float(totals['error_rate']):.2f}% | {_change(changes.get('error_rate'))} |",
        (
            f"| 成功 / 4xx / 5xx | {int(totals['success_requests']):,} / "
            f"{int(totals['client_error_requests']):,} / "
            f"{int(totals['server_error_requests']):,} | — |"
        ),
        "",
    ]

    for dimension, label in _REPORT_DIMENSIONS:
        items = repository.distribution(from_, to, dimension, filters, 100)
        ranked = list(items[:10])
        lines.extend(
            [
                f"## {label}（按 Token 排序）",
                "",
                "| 名称 | 请求数 | Token | 估算费用（USD） |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        if ranked:
            lines.extend(
                (
                    f"| {_markdown_cell(item['name'])} | "
                    f"{int(item['total_requests']):,} | {int(item['total_tokens']):,} | "
                    f"{_money(item['estimated_cost'])} |"
                )
                for item in ranked
            )
        else:
            lines.append("| — | 0 | 0 | $0.000000 |")
        lines.append("")

    lines.extend(
        [
            "## 明细字段说明",
            "",
            (
                "`requests.csv` 包含请求与关联 ID、时间、组织/部门/项目/用户/智能体归属、"
                "模型与 Runtime、Token 分项、状态、延迟、估算费用及对账/准入元数据。"
            ),
            (
                "费用是模型价格表估算值，缓存 Token 可能尚未实测；请结合 "
                "`usage_estimated`、`ingest_error` 和 `reconciled_at` 判断数据完整性。"
            ),
            (
                "若单价快照列为空，对应请求可能未计价；请勿将汇总估算费用当作 Azure "
                "账单或完整实际支出。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _change(value: object) -> str:
    return "—" if value is None else f"{float(str(value)):+.2f}%"


def _filter_description(filters: UsageFilters) -> str:
    fields = (
        ("组织", filters.organization_id),
        ("部门", filters.department_id),
        ("项目", filters.project_id),
        ("智能体", filters.agent_id),
        ("模型", filters.model_id),
        ("人员", filters.user_id),
        ("Runtime", ", ".join(filters.runtime) if filters.runtime else None),
        ("状态码", filters.status_code),
    )
    selected = [f"{label}={_markdown_cell(value)}" for label, value in fields if value is not None]
    return "；".join(selected) if selected else "全部"


def build_usage_report_archive(
    repository: QueryRepository,
    from_: datetime,
    to: datetime,
    filters: UsageFilters,
) -> SpooledTemporaryFile[bytes]:
    # This temporary file is returned to the HTTP response, which owns closing it after download.
    archive_buffer = SpooledTemporaryFile(  # noqa: SIM115
        max_size=8 * 1024 * 1024, mode="w+b"
    )
    try:
        with ZipFile(archive_buffer, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("analysis.md", _report_markdown(repository, from_, to, filters))
            with archive.open("requests.csv", "w") as compressed_csv:
                text_csv = io.TextIOWrapper(
                    compressed_csv, encoding="utf-8-sig", newline="", write_through=True
                )
                try:
                    writer = csv.DictWriter(text_csv, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                    writer.writeheader()
                    for row in repository.iter_usage_requests(from_, to, filters):
                        writer.writerow(
                            {
                                column: _safe_csv_value(row.get(column))
                                for column in CSV_COLUMNS
                            }
                        )
                    text_csv.flush()
                finally:
                    text_csv.detach()
        archive_buffer.seek(0)
        return archive_buffer
    except Exception:
        archive_buffer.close()
        raise


def usage_report_filename(from_: datetime, to: datetime) -> str:
    start = re.sub(r"[^0-9]", "", from_.date().isoformat())
    end = re.sub(r"[^0-9]", "", to.date().isoformat())
    return f"turnstile_usage_report_{start}_{end}.zip"
