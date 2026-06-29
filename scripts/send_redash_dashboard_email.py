#!/usr/bin/env python3
"""Send a styled Redash dashboard report by email.

Configuration is read exclusively from environment variables. The script
refreshes dashboard query widgets, keeps only MMDD date columns, renders the
main report table, and sends it via SMTP_SSL.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Iterable
from urllib import error, parse, request


EXPECTED_DASHBOARD_NAME = "CFI Loan T 安卓/ IOS-综合-风控日报/周报(规模、转化、风险)"
DATE_COLUMN_RE = re.compile(r"^\d{4}$")
DATE_RANGE_RE = re.compile(
    r"^\s*(?:\d{4}|\d{1,2}[/-]\d{1,2}|\d{4}[/-]\d{1,2}[/-]\d{1,2})"
    r"\s*-\s*"
    r"(?:\d{4}|\d{1,2}[/-]\d{1,2}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*$"
)
DATE_LIKE_RE = re.compile(
    r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}|\d{8}|week|周|合计|总计)",
    re.IGNORECASE,
)

LEFT_HEADERS = ["指标分类", "一级序号", "指标项", "二级序号", "样本"]
SECTION_ORDER = [
    ("规模", "规模-安卓/IOS 综合"),
    ("转化", "转化-安卓/IOS 综合"),
    ("风险", "风险"),
]
SECTION_INDEX = {label: str(i + 1) for i, (_, label) in enumerate(SECTION_ORDER)}

ALIAS_MAP = {
    "指标分类": ["指标分类", "分类", "分组", "板块", "section", "category"],
    "一级序号": ["一级序号", "一级", "序号1", "一级编号", "primary_index"],
    "指标项": ["指标项", "指标名称", "指标", "metric", "metric_name", "name"],
    "二级序号": ["二级序号", "二级", "序号2", "二级编号", "secondary_index"],
    "样本": ["样本", "样本类型", "口径", "sample"],
}

REPORT_CSS = """
<style>
  table.report { border-collapse: collapse; width: 100%; font-size: 12px; font-family: Arial, sans-serif; }
  table.report th, table.report td { border: 1px solid #ccc; padding: 6px 8px; text-align: center; }
  table.report th { background: #f5f5f5; font-weight: bold; }
  table.report td.metric-name { text-align: left; min-width: 220px; }
  table.report td.section { background: #e8f4fc; font-weight: bold; text-align: left; }
  table.report td.number, table.report td.percent { text-align: right; }
</style>
""".strip()


class ReportError(RuntimeError):
    """Raised for user-actionable report generation failures."""


@dataclass(frozen=True)
class Settings:
    redash_url: str
    redash_api_key: str
    dashboard_id: str
    dashboard_name: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: list[str]
    job_timeout_seconds: int = 180
    job_poll_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "Settings":
        required = [
            "REDASH_URL",
            "REDASH_API_KEY",
            "DASHBOARD_ID",
            "DASHBOARD_NAME",
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_USER",
            "SMTP_PASSWORD",
            "EMAIL_FROM",
            "EMAIL_TO",
        ]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ReportError(f"Missing required environment variables: {', '.join(missing)}")

        email_to = split_recipients(os.environ["EMAIL_TO"])
        if not email_to:
            raise ReportError("EMAIL_TO did not contain any recipients")

        return cls(
            redash_url=os.environ["REDASH_URL"].rstrip("/"),
            redash_api_key=os.environ["REDASH_API_KEY"],
            dashboard_id=os.environ["DASHBOARD_ID"].strip(),
            dashboard_name=os.environ["DASHBOARD_NAME"].strip(),
            smtp_host=os.environ["SMTP_HOST"],
            smtp_port=int(os.environ["SMTP_PORT"]),
            smtp_user=os.environ["SMTP_USER"],
            smtp_password=os.environ["SMTP_PASSWORD"],
            email_from=os.environ["EMAIL_FROM"],
            email_to=email_to,
            job_timeout_seconds=int(os.getenv("REDASH_JOB_TIMEOUT_SECONDS", "180")),
            job_poll_seconds=float(os.getenv("REDASH_JOB_POLL_SECONDS", "2")),
        )


@dataclass
class QueryWidget:
    widget_id: Any
    query_id: int
    title: str
    query_name: str

    @property
    def combined_name(self) -> str:
        return " ".join(part for part in [self.title, self.query_name] if part).strip()


@dataclass
class QueryWidgetResult:
    widget: QueryWidget
    columns: list[str]
    rows: list[dict[str, Any]]

    @property
    def combined_name(self) -> str:
        return self.widget.combined_name


@dataclass
class ReportRow:
    section: str
    primary_index: str
    metric_name: str
    secondary_index: str
    sample: str
    values: dict[str, Any]


@dataclass
class ReportData:
    rows: list[ReportRow]
    date_columns: list[str]
    excluded_columns: list[str]
    raw_columns: list[str]
    appendices: list[QueryWidgetResult]


class RedashClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._url(path, params)
        return self._json_request("GET", url)

    def post_json(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = self._url(path)
        body = json.dumps(payload or {}).encode("utf-8")
        return self._json_request("POST", url, body)

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{parse.urlencode(params)}"
        return url

    def _json_request(self, method: str, url: str, body: bytes | None = None) -> Any:
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ReportError(f"Redash request failed: {method} {scrub_url(url)} HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise ReportError(f"Redash request failed: {method} {scrub_url(url)}: {exc.reason}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReportError(f"Redash returned non-JSON response for {method} {scrub_url(url)}") from exc

    def locate_dashboard(self, dashboard_id: str, expected_name: str) -> dict[str, Any]:
        dashboard = self.get_json(f"/api/dashboards/{parse.quote(str(dashboard_id), safe='')}")
        actual_name = str(dashboard.get("name", "")).strip()
        if names_match(actual_name, expected_name):
            logging.info("Dashboard name matched configured DASHBOARD_NAME")
            return dashboard

        logging.warning(
            "Dashboard id %s returned name %r, searching dashboards by exact configured name",
            dashboard_id,
            actual_name,
        )
        listing = self.get_json("/api/dashboards", {"page": 1, "page_size": 100})
        dashboards = listing.get("results", listing if isinstance(listing, list) else [])
        for candidate in dashboards:
            if names_match(str(candidate.get("name", "")).strip(), expected_name):
                slug_or_id = candidate.get("slug") or candidate.get("id")
                if not slug_or_id:
                    raise ReportError(f"Dashboard named {expected_name!r} did not include id or slug")
                return self.get_json(f"/api/dashboards/{parse.quote(str(slug_or_id), safe='')}")

        raise ReportError(
            f"DASHBOARD_NAME mismatch: id {dashboard_id!r} returned {actual_name!r}, "
            f"and no exact name match was found on page 1"
        )

    def refresh_query_and_get_result(
        self,
        query_id: int,
        timeout_seconds: int,
        poll_seconds: float,
    ) -> dict[str, Any]:
        logging.info("Refreshing Redash query id %s", query_id)
        refresh = self.post_json(f"/api/queries/{query_id}/refresh", {"max_age": 0})
        job = refresh.get("job") if isinstance(refresh, dict) else None
        if not job or not job.get("id"):
            raise ReportError(f"Redash refresh response for query {query_id} did not include a job id")

        query_result_id = self._poll_job(job["id"], timeout_seconds, poll_seconds)
        if query_result_id:
            return self.get_json(f"/api/queries/{query_id}/results/{query_result_id}.json")
        return self.get_json(f"/api/queries/{query_id}/results.json")

    def _poll_job(self, job_id: str, timeout_seconds: int, poll_seconds: float) -> Any:
        deadline = time.monotonic() + timeout_seconds
        last_status = None
        while time.monotonic() < deadline:
            payload = self.get_json(f"/api/jobs/{parse.quote(str(job_id), safe='')}")
            job = payload.get("job", payload)
            last_status = job.get("status")
            if last_status == 3:
                return job.get("query_result_id")
            if last_status in (4, 5):
                raise ReportError(f"Redash job {job_id} failed or was cancelled: {job}")
            time.sleep(poll_seconds)
        raise ReportError(f"Timed out waiting for Redash job {job_id}; last status={last_status}")


def split_recipients(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;,]", value) if item.strip()]


def names_match(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def scrub_url(url: str) -> str:
    parsed = parse.urlsplit(url)
    return parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def extract_query_widgets(dashboard: dict[str, Any]) -> list[QueryWidget]:
    widgets: list[QueryWidget] = []
    for raw_widget in dashboard.get("widgets", []):
        visualization = raw_widget.get("visualization") or {}
        query = visualization.get("query") or {}
        query_id = query.get("id") or visualization.get("query_id") or raw_widget.get("query_id")
        if not query_id:
            continue
        title = first_text(
            raw_widget.get("text"),
            (raw_widget.get("options") or {}).get("title"),
            visualization.get("name"),
            query.get("name"),
        )
        widgets.append(
            QueryWidget(
                widget_id=raw_widget.get("id"),
                query_id=int(query_id),
                title=title,
                query_name=first_text(query.get("name"), visualization.get("name")),
            )
        )
    return widgets


def first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_query_result(widget: QueryWidget, payload: dict[str, Any]) -> QueryWidgetResult:
    query_result = payload.get("query_result", payload)
    data = query_result.get("data", query_result)
    columns = normalize_columns(data.get("columns", []))
    rows = data.get("rows", [])
    if not isinstance(rows, list):
        raise ReportError(f"Query {widget.query_id} returned rows in an unexpected format")
    normalized_rows = [row for row in rows if isinstance(row, dict)]
    return QueryWidgetResult(widget=widget, columns=columns, rows=normalized_rows)


def normalize_columns(columns: Iterable[Any]) -> list[str]:
    normalized: list[str] = []
    for column in columns:
        if isinstance(column, dict):
            name = column.get("name") or column.get("friendly_name")
        else:
            name = column
        if name is not None:
            normalized.append(str(name).strip())
    return normalized


def prepare_report_data(results: list[QueryWidgetResult]) -> ReportData:
    main_results, appendices = select_main_results(results)
    if not main_results:
        raise ReportError("No query widgets could be mapped to the main report")

    raw_columns = unique_preserving_order(column for result in main_results for column in result.columns)
    date_columns, excluded_columns = filter_date_columns(raw_columns)
    logging.info("Included date columns: %s", date_columns)
    logging.info("Excluded columns: %s", excluded_columns)
    if not date_columns:
        raise ReportError(f"Zero valid 4-digit MMDD date columns after filtering. Raw columns: {raw_columns}")

    rows: list[ReportRow] = []
    for result in main_results:
        section = section_for_name(result.combined_name) or infer_section_from_rows(result.rows) or "未分类"
        primary_default = SECTION_INDEX.get(section, "")
        for idx, row in enumerate(result.rows, start=1):
            rows.append(normalize_report_row(row, section, primary_default, str(idx), date_columns))

    return ReportData(
        rows=rows,
        date_columns=date_columns,
        excluded_columns=excluded_columns,
        raw_columns=raw_columns,
        appendices=appendices,
    )


def select_main_results(results: list[QueryWidgetResult]) -> tuple[list[QueryWidgetResult], list[QueryWidgetResult]]:
    non_empty = [result for result in results if result.rows]
    summaries = [
        result
        for result in non_empty
        if "规模" in result.combined_name and "转化" in result.combined_name
    ]
    if summaries:
        selected = [summaries[0]]
        selected_ids = {id(summaries[0])}
        appendices = [result for result in non_empty if id(result) not in selected_ids]
        return selected, appendices

    categorized: list[QueryWidgetResult] = []
    selected_ids: set[int] = set()
    for keyword, _ in SECTION_ORDER:
        for result in non_empty:
            if id(result) in selected_ids:
                continue
            if keyword in result.combined_name:
                categorized.append(result)
                selected_ids.add(id(result))

    if categorized:
        appendices = [result for result in non_empty if id(result) not in selected_ids]
        return categorized, appendices

    if len(non_empty) == 1:
        return non_empty, []

    return [], non_empty


def filter_date_columns(columns: Iterable[str]) -> tuple[list[str], list[str]]:
    included: list[str] = []
    excluded: list[str] = []
    for column in unique_preserving_order(columns):
        trimmed = column.strip()
        if DATE_COLUMN_RE.fullmatch(trimmed):
            included.append(trimmed)
        elif is_left_header(trimmed):
            continue
        else:
            excluded.append(trimmed)

    included = sorted(set(included), key=mmdd_sort_key)
    if len(included) > 7:
        included = included[-7:]
    return included, excluded


def is_left_header(column: str) -> bool:
    normalized = column.strip().casefold()
    for canonical, aliases in ALIAS_MAP.items():
        if normalized == canonical.casefold():
            return True
        if any(normalized == alias.casefold() for alias in aliases):
            return True
    return False


def mmdd_sort_key(value: str) -> tuple[int, int]:
    month = int(value[:2])
    day = int(value[2:])
    return month, day


def is_date_like_excluded_column(column: str) -> bool:
    trimmed = column.strip()
    return bool(DATE_RANGE_RE.match(trimmed) or DATE_LIKE_RE.search(trimmed))


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def section_for_name(name: str) -> str | None:
    for keyword, label in SECTION_ORDER:
        if keyword in name:
            return label
    return None


def infer_section_from_rows(rows: list[dict[str, Any]]) -> str | None:
    for row in rows:
        raw = lookup_alias(row, "指标分类")
        if raw:
            section = normalize_section(str(raw))
            if section:
                return section
    return None


def normalize_section(value: str) -> str:
    text = value.strip()
    for keyword, label in SECTION_ORDER:
        if keyword in text:
            return label
    return text


def normalize_report_row(
    row: dict[str, Any],
    default_section: str,
    default_primary: str,
    default_secondary: str,
    date_columns: list[str],
) -> ReportRow:
    section = normalize_section(str(lookup_alias(row, "指标分类") or default_section))
    primary_index = str(lookup_alias(row, "一级序号") or default_primary or SECTION_INDEX.get(section, ""))
    metric_name = str(lookup_alias(row, "指标项") or lookup_first_non_date_value(row) or "")
    secondary_index = str(lookup_alias(row, "二级序号") or default_secondary)
    sample = str(lookup_alias(row, "样本") or "-")
    values = {date: row.get(date) for date in date_columns}
    return ReportRow(section, primary_index, metric_name, secondary_index, sample, values)


def lookup_alias(row: dict[str, Any], canonical: str) -> Any:
    aliases = ALIAS_MAP[canonical]
    normalized_keys = {str(key).strip().casefold(): key for key in row}
    for alias in aliases:
        raw_key = normalized_keys.get(alias.casefold())
        if raw_key is not None:
            return row.get(raw_key)
    return None


def lookup_first_non_date_value(row: dict[str, Any]) -> Any:
    for key, value in row.items():
        key_text = str(key).strip()
        if DATE_COLUMN_RE.fullmatch(key_text) or is_date_like_excluded_column(key_text):
            continue
        if is_left_header(key_text):
            continue
        if value not in (None, ""):
            return value
    return None


def render_email_html(dashboard_name: str, report: ReportData, now: datetime | None = None) -> str:
    if not report.date_columns:
        raise ReportError("Cannot render email without valid date columns")
    now = now or datetime.now()
    date_range = f"{report.date_columns[0]} - {report.date_columns[-1]}"
    rows_html = render_main_rows(report.rows, report.date_columns)
    appendices_html = render_appendices(report.appendices, report.date_columns)
    escaped_title = html.escape(dashboard_name)
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
{REPORT_CSS}
</head>
<body>
  <h2>{escaped_title}</h2>
  <p>数据日期范围：{html.escape(date_range)}</p>
  <p>生成时间：{html.escape(now.strftime("%Y-%m-%d %H:%M:%S"))}</p>
  <table class="report">
    <thead>
      <tr>{''.join(f'<th>{html.escape(header)}</th>' for header in LEFT_HEADERS + report.date_columns)}</tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
{appendices_html}
</body>
</html>"""


def render_main_rows(rows: list[ReportRow], date_columns: list[str]) -> str:
    if not rows:
        return '      <tr><td colspan="{}">无数据</td></tr>'.format(len(LEFT_HEADERS) + len(date_columns))

    section_spans: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row.section, row.primary_index)
        section_spans[key] = section_spans.get(key, 0) + 1

    rendered: list[str] = []
    emitted_sections: set[tuple[str, str]] = set()
    for row in rows:
        cells: list[str] = []
        key = (row.section, row.primary_index)
        if key not in emitted_sections:
            rowspan = section_spans[key]
            cells.append(
                f'<td class="section" rowspan="{rowspan}">{html.escape(row.section or "-")}</td>'
            )
            cells.append(f'<td rowspan="{rowspan}">{html.escape(row.primary_index or "-")}</td>')
            emitted_sections.add(key)
        cells.append(f'<td class="metric-name">{html.escape(row.metric_name or "-")}</td>')
        cells.append(f"<td>{html.escape(row.secondary_index or '-')}</td>")
        cells.append(f"<td>{html.escape(row.sample or '-')}</td>")
        for date in date_columns:
            formatted, css_class = format_value(row.values.get(date), row.metric_name)
            cells.append(f'<td class="{css_class}">{html.escape(formatted)}</td>')
        rendered.append(f"      <tr>{''.join(cells)}</tr>")
    return "\n".join(rendered)


def render_appendices(appendices: list[QueryWidgetResult], main_date_columns: list[str]) -> str:
    if not appendices:
        return ""

    parts = ["  <h3>附录：无法映射的 widget</h3>"]
    for appendix in appendices:
        appendix_columns = columns_for_appendix(appendix.columns, main_date_columns)
        if not appendix_columns:
            continue
        title = html.escape(appendix.combined_name or f"Query {appendix.widget.query_id}")
        header = "".join(f"<th>{html.escape(column)}</th>" for column in appendix_columns)
        body_rows: list[str] = []
        for row in appendix.rows:
            cells = "".join(
                f"<td>{html.escape(format_appendix_value(row.get(column)))}</td>"
                for column in appendix_columns
            )
            body_rows.append(f"      <tr>{cells}</tr>")
        if not body_rows:
            body_rows.append(f'      <tr><td colspan="{len(appendix_columns)}">无数据</td></tr>')
        parts.append(
            f"""  <h4>{title}</h4>
  <table class="report">
    <thead><tr>{header}</tr></thead>
    <tbody>
{chr(10).join(body_rows)}
    </tbody>
  </table>"""
        )
    return "\n".join(parts)


def columns_for_appendix(columns: list[str], main_date_columns: list[str]) -> list[str]:
    output: list[str] = []
    for column in columns:
        if DATE_COLUMN_RE.fullmatch(column):
            if column in main_date_columns:
                output.append(column)
        elif is_date_like_excluded_column(column):
            continue
        else:
            output.append(column)
    return unique_preserving_order(output)


def format_appendix_value(value: Any) -> str:
    if value in (None, ""):
        return "-"
    return str(value)


def format_value(value: Any, metric_name: str = "") -> tuple[str, str]:
    if value in (None, ""):
        return "-", "number"

    raw_text = str(value).strip()
    if raw_text in {"-", "—"}:
        return "-", "number"

    is_percent = is_percent_metric(metric_name) or raw_text.endswith("%")
    number = parse_number(raw_text)
    if number is None:
        return raw_text, "percent" if is_percent else "number"

    if is_percent:
        if raw_text.endswith("%"):
            percent_value = number
        else:
            percent_value = number * 100 if abs(number) <= 1 else number
        return f"{percent_value:.2f}%", "percent"
    return f"{number:.2f}", "number"


def is_percent_metric(metric_name: str) -> bool:
    text = metric_name.casefold()
    return any(token in text for token in ["率", "ratio", "rate", "%"])


def parse_number(value: str) -> float | None:
    cleaned = value.replace(",", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def build_subject(dashboard_name: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{dashboard_name} - {now.strftime('%Y-%m-%d')}"


def send_email(settings: Settings, subject: str, html_body: str) -> None:
    message = MIMEMultipart("alternative")
    message["Subject"] = str(Header(subject, "utf-8"))
    message["From"] = settings.email_from
    message["To"] = ", ".join(settings.email_to)
    message.attach(MIMEText(html_body, "html", "utf-8"))

    logging.info("Sending report email to %d recipient(s) via SMTP_SSL %s:%s", len(settings.email_to), settings.smtp_host, settings.smtp_port)
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(settings.email_from, settings.email_to, message.as_string())
    logging.info("Report email sent successfully")


def fetch_dashboard_results(client: RedashClient, settings: Settings) -> tuple[dict[str, Any], list[QueryWidgetResult]]:
    dashboard = client.locate_dashboard(settings.dashboard_id, settings.dashboard_name)
    final_id = dashboard.get("id") or dashboard.get("slug") or settings.dashboard_id
    final_name = dashboard.get("name", "")
    logging.info("Final dashboard id=%s name=%s", final_id, final_name)
    if not names_match(str(final_name), settings.dashboard_name):
        raise ReportError(f"Final dashboard name {final_name!r} does not match DASHBOARD_NAME {settings.dashboard_name!r}")

    widgets = extract_query_widgets(dashboard)
    if not widgets:
        raise ReportError("Dashboard did not contain any query widgets")
    logging.info("Found %d query widget(s)", len(widgets))

    results: list[QueryWidgetResult] = []
    for widget in widgets:
        payload = client.refresh_query_and_get_result(
            widget.query_id,
            timeout_seconds=settings.job_timeout_seconds,
            poll_seconds=settings.job_poll_seconds,
        )
        result = normalize_query_result(widget, payload)
        logging.info(
            "Fetched widget id=%s query_id=%s name=%r rows=%d columns=%d",
            widget.widget_id,
            widget.query_id,
            widget.combined_name,
            len(result.rows),
            len(result.columns),
        )
        results.append(result)
    return dashboard, results


def run(settings: Settings, *, dry_run: bool = False, html_output: str | None = None) -> None:
    client = RedashClient(settings.redash_url, settings.redash_api_key)
    _, query_results = fetch_dashboard_results(client, settings)
    report = prepare_report_data(query_results)
    body = render_email_html(settings.dashboard_name, report)
    if html_output:
        with open(html_output, "w", encoding="utf-8") as fp:
            fp.write(body)
        logging.info("Wrote rendered HTML to %s", html_output)
    if dry_run:
        logging.info("Dry run enabled; SMTP send skipped")
        return
    send_email(settings, build_subject(settings.dashboard_name), body)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send Redash dashboard data as an HTML email")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and render the report without sending email")
    parser.add_argument("--html-output", help="Optional path to write rendered HTML")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), help="Python logging level")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        settings = Settings.from_env()
        if not names_match(settings.dashboard_name, EXPECTED_DASHBOARD_NAME):
            logging.warning(
                "Configured DASHBOARD_NAME differs from the requested dashboard name; using environment value"
            )
        run(settings, dry_run=args.dry_run, html_output=args.html_output)
        return 0
    except ReportError as exc:
        logging.error("%s", exc)
        return 1
    except Exception:
        logging.exception("Unexpected failure while sending Redash dashboard email")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
