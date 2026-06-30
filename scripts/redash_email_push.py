#!/usr/bin/env python3
"""Pull Redash Query 3157 results and send a formatted HTML email report."""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("redash_email_push")
DATE_COLUMN_RE = re.compile(r"^\d{4}$")
FIXED_QUERY_ID = "3157"

BASE_COLUMNS = ["指标分类", "一级序号", "指标项", "二级序号", "样本"]
RATE_HINTS = ("率", "占比", "比例", "rate", "ratio", "%")


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


class RedashError(RuntimeError):
    """Raised when Redash refresh or result retrieval fails."""


@dataclass(frozen=True)
class Config:
    redash_url: str
    redash_api_key: str
    query_id: str
    dashboard_id: Optional[str]
    dashboard_name: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: List[str]
    refresh_timeout_seconds: int = 120


@dataclass(frozen=True)
class QueryData:
    columns: List[str]
    rows: List[Dict[str, Any]]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ConfigError(f"Missing required environment variable: {name}")
    return value.strip()


def parse_email_recipients(value: str) -> List[str]:
    recipients = [part.strip() for chunk in value.split(";") for part in chunk.split(",")]
    recipients = [recipient for recipient in recipients if recipient]
    if not recipients:
        raise ConfigError("EMAIL_TO must contain at least one recipient")
    return recipients


def load_query_id() -> str:
    query_id = os.getenv("QUERY_ID", FIXED_QUERY_ID).strip() or FIXED_QUERY_ID
    if query_id != FIXED_QUERY_ID:
        raise ConfigError(
            f"QUERY_ID must be {FIXED_QUERY_ID}; refusing to use Query ID {query_id}"
        )
    return FIXED_QUERY_ID


def load_config() -> Config:
    smtp_port_raw = os.getenv("SMTP_PORT", "465").strip() or "465"
    try:
        smtp_port = int(smtp_port_raw)
    except ValueError as exc:
        raise ConfigError("SMTP_PORT must be an integer") from exc

    return Config(
        redash_url=required_env("REDASH_URL").rstrip("/") + "/",
        redash_api_key=required_env("REDASH_API_KEY"),
        query_id=load_query_id(),
        dashboard_id=os.getenv("DASHBOARD_ID", "").strip() or None,
        dashboard_name=required_env("DASHBOARD_NAME"),
        smtp_host=required_env("SMTP_HOST"),
        smtp_port=smtp_port,
        smtp_user=required_env("SMTP_USER"),
        smtp_password=required_env("SMTP_PASSWORD"),
        email_from=required_env("EMAIL_FROM"),
        email_to=parse_email_recipients(required_env("EMAIL_TO")),
    )


class RedashClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _request_json(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> Any:
        data = None
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = _read_http_error_body(exc)
            raise RedashError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RedashError(f"{method} {path} failed: {exc.reason}") from exc

        if raw.strip() == "":
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RedashError(f"{method} {path} returned invalid JSON") from exc

    def refresh_query(self, query_id: str, timeout_seconds: int = 120, poll_interval: float = 2.0) -> None:
        payload = self._request_json("POST", f"/api/queries/{query_id}/refresh", body={})
        job_id = _extract_job_id(payload)
        if not job_id:
            raise RedashError(f"Refresh response for Query {query_id} did not include a job id")

        LOGGER.info("Refresh job started: %s", job_id)
        deadline = time.monotonic() + timeout_seconds
        last_status: Any = None
        while time.monotonic() < deadline:
            job_payload = self._request_json("GET", f"/api/jobs/{job_id}")
            job = job_payload.get("job", job_payload) if isinstance(job_payload, dict) else {}
            last_status = job.get("status")
            if _job_succeeded(last_status):
                LOGGER.info("Refresh job completed successfully")
                return
            if _job_failed(last_status):
                error_message = job.get("error") or job.get("message") or "unknown Redash job error"
                raise RedashError(f"Refresh job {job_id} failed with status {last_status}: {error_message}")
            time.sleep(poll_interval)

        raise RedashError(
            f"Refresh job {job_id} timed out after {timeout_seconds}s; last status: {last_status}"
        )

    def get_dashboard_name(self, dashboard_id: str) -> str:
        payload = self._request_json("GET", f"/api/dashboards/{dashboard_id}")
        if not isinstance(payload, dict) or not payload.get("name"):
            raise RedashError(f"Dashboard {dashboard_id} response did not include a name")
        return str(payload["name"])

    def fetch_query_results(self, query_id: str) -> QueryData:
        try:
            latest_payload = self._request_json("GET", f"/api/queries/{query_id}/results/latest")
            return extract_query_data(latest_payload)
        except RedashError as exc:
            LOGGER.warning("Latest results unavailable for Query %s; falling back to result list: %s", query_id, exc)

        payload = self._request_json("GET", f"/api/queries/{query_id}/results")
        return extract_query_data(payload)


def _read_http_error_body(exc: HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return "unable to read response body"
    return raw[:1000] if raw else "empty response body"


def _extract_job_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    job = payload.get("job")
    if isinstance(job, dict) and job.get("id") is not None:
        return str(job["id"])
    if payload.get("job_id") is not None:
        return str(payload["job_id"])
    return None


def _normalize_status(status: Any) -> str:
    return str(status).strip().lower()


def _job_succeeded(status: Any) -> bool:
    normalized = _normalize_status(status)
    return normalized in {"3", "success", "succeeded", "done", "completed"}


def _job_failed(status: Any) -> bool:
    normalized = _normalize_status(status)
    return normalized in {"4", "5", "failure", "failed", "error", "cancelled", "canceled"}


def extract_query_data(payload: Any) -> QueryData:
    data = _find_result_data(payload)
    if not isinstance(data, dict):
        raise RedashError("Redash results response did not include query result data")

    columns = [_column_name(column) for column in data.get("columns", [])]
    rows = data.get("rows", [])
    if not columns and rows:
        columns = list(rows[0].keys())
    if not columns:
        raise RedashError("Redash query result did not include any columns")
    if not isinstance(rows, list):
        raise RedashError("Redash query result rows are not a list")

    normalized_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise RedashError("Redash query result row is not an object")
        normalized_rows.append(dict(row))

    return QueryData(columns=columns, rows=normalized_rows)


def _find_result_data(payload: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(payload, dict):
        return None

    query_result = payload.get("query_result")
    if isinstance(query_result, dict) and isinstance(query_result.get("data"), dict):
        return query_result["data"]

    if isinstance(payload.get("data"), dict):
        return payload["data"]

    query_results = payload.get("query_results")
    if isinstance(query_results, list):
        for result in query_results:
            if isinstance(result, dict):
                data = result.get("data")
                if isinstance(data, dict):
                    return data
                nested_query_result = result.get("query_result")
                if isinstance(nested_query_result, dict) and isinstance(nested_query_result.get("data"), dict):
                    return nested_query_result["data"]
    return None


def _column_name(column: Any) -> str:
    if isinstance(column, dict):
        return str(column.get("name", "")).strip()
    return str(column).strip()


def select_date_columns(columns: Sequence[str], limit: int = 7) -> Tuple[List[str], List[str]]:
    legal_date_columns = sorted(column for column in columns if DATE_COLUMN_RE.fullmatch(column))
    selected = legal_date_columns[-limit:]
    if not selected:
        raise ValueError(f"No valid MMDD date columns found. Original columns: {list(columns)}")

    selected_set = set(selected)
    excluded = [column for column in columns if column not in selected_set]
    return selected, excluded


def validate_dashboard_name(client: RedashClient, config: Config) -> None:
    if not config.dashboard_id:
        LOGGER.info("DASHBOARD_ID not set; skipping dashboard name validation")
        return

    try:
        actual_name = client.get_dashboard_name(config.dashboard_id)
    except RedashError as exc:
        LOGGER.warning("Dashboard validation skipped: %s", exc)
        return

    if actual_name != config.dashboard_name:
        LOGGER.warning(
            "Dashboard name mismatch; continuing with DASHBOARD_NAME. Expected %r, got %r",
            config.dashboard_name,
            actual_name,
        )
        return
    LOGGER.info("Dashboard name validated: %s", actual_name)


def build_html_email(
    dashboard_name: str,
    rows: Sequence[Mapping[str, Any]],
    date_columns: Sequence[str],
    generated_at: datetime,
) -> str:
    date_range = f"{date_columns[0]}-{date_columns[-1]}"
    total_columns = len(BASE_COLUMNS) + len(date_columns)
    table_header = "".join(f"<th>{escape(column)}</th>" for column in [*BASE_COLUMNS, *date_columns])

    body_parts = []
    for group_name, group_rows in group_rows_by_category(rows):
        body_parts.append(
            f'<tr class="group-row"><td colspan="{total_columns}">{escape(group_name)}</td></tr>'
        )
        rowspan = len(group_rows)
        first_row = True
        for row in group_rows:
            metric_item = display_value(row.get("指标项"))
            cells = []
            if first_row:
                cells.append(f'<td rowspan="{rowspan}">{escape(display_value(row.get("指标分类"), group_name))}</td>')
                cells.append(f'<td rowspan="{rowspan}">{escape(display_value(row.get("一级序号")))}</td>')
                first_row = False
            cells.extend(
                [
                    f"<td>{escape(metric_item)}</td>",
                    f"<td>{escape(display_value(row.get('二级序号')))}</td>",
                    f"<td>{escape(display_value(row.get('样本')))}</td>",
                ]
            )
            for date_column in date_columns:
                cells.append(f"<td>{escape(format_metric_value(row.get(date_column), metric_item))}</td>")
            body_parts.append("<tr>" + "".join(cells) + "</tr>")

    if not body_parts:
        body_parts.append(f'<tr><td colspan="{total_columns}">No data returned</td></tr>')

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; font-size: 12px; color: #222; }}
    h2 {{ font-size: 16px; margin: 0 0 8px 0; }}
    p {{ margin: 4px 0; }}
    table {{ border-collapse: collapse; font-family: Arial, sans-serif; font-size: 12px; }}
    th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: center; }}
    th {{ background: #f5f5f5; font-weight: bold; }}
    .group-row td {{ background: #e8f4fc; font-weight: bold; text-align: left; }}
  </style>
</head>
<body>
  <h2>{escape(dashboard_name)}</h2>
  <p>日期范围：{escape(date_range)}</p>
  <p>生成时间：{escape(generated_at.strftime("%Y-%m-%d %H:%M:%S"))}</p>
  <table>
    <thead><tr>{table_header}</tr></thead>
    <tbody>
      {"".join(body_parts)}
    </tbody>
  </table>
</body>
</html>"""


def group_rows_by_category(rows: Sequence[Mapping[str, Any]]) -> Iterable[Tuple[str, List[Mapping[str, Any]]]]:
    grouped: Dict[str, List[Mapping[str, Any]]] = {}
    order: List[str] = []
    for row in rows:
        group_name = display_value(row.get("指标分类"), "未分类")
        if group_name not in grouped:
            grouped[group_name] = []
            order.append(group_name)
        grouped[group_name].append(row)
    for group_name in order:
        yield group_name, grouped[group_name]


def display_value(value: Any, default: str = "-") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else default
    return str(value)


def format_metric_value(value: Any, metric_item: str) -> str:
    if value is None:
        return "-"
    if isinstance(value, str) and value.strip() == "":
        return "-"

    is_rate = is_rate_metric(metric_item)
    parsed = parse_number(value)
    if parsed is None:
        return display_value(value)

    if is_rate:
        percentage = parsed * 100 if abs(parsed) <= 1 else parsed
        return f"{percentage:.2f}%"
    return f"{parsed:.2f}"


def is_rate_metric(metric_item: str) -> bool:
    lower_metric = metric_item.lower()
    return any(hint in lower_metric for hint in RATE_HINTS)


def parse_number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().replace(",", "")
        if normalized.endswith("%"):
            normalized = normalized[:-1]
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def build_plain_text(dashboard_name: str, date_columns: Sequence[str], generated_at: datetime) -> str:
    return (
        f"{dashboard_name}\n"
        f"日期范围：{date_columns[0]}-{date_columns[-1]}\n"
        f"生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        "请使用支持 HTML 的邮件客户端查看主表。"
    )


def send_email(
    config: Config,
    subject: str,
    plain_text: str,
    html_body: str,
) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.email_from
    message["To"] = ", ".join(config.email_to)
    message.set_content(plain_text)
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context, timeout=30) as smtp:
        smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)


def run(config: Config) -> None:
    LOGGER.info("Using Query ID: %s", config.query_id)
    client = RedashClient(config.redash_url, config.redash_api_key)

    validate_dashboard_name(client, config)
    client.refresh_query(config.query_id, timeout_seconds=config.refresh_timeout_seconds)
    query_data = client.fetch_query_results(config.query_id)

    date_columns, excluded_columns = select_date_columns(query_data.columns)
    LOGGER.info("Included date columns: %s", date_columns)
    LOGGER.info("Excluded columns: %s", excluded_columns)

    generated_at = datetime.now()
    html_body = build_html_email(config.dashboard_name, query_data.rows, date_columns, generated_at)
    plain_text = build_plain_text(config.dashboard_name, date_columns, generated_at)
    subject = f"{config.dashboard_name} - {generated_at.strftime('%Y-%m-%d')}"

    send_email(config, subject, plain_text, html_body)
    LOGGER.info("Email sent to %d recipient(s)", len(config.email_to))


def main() -> int:
    setup_logging()
    try:
        run(load_config())
    except (ConfigError, RedashError, ValueError, smtplib.SMTPException, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
