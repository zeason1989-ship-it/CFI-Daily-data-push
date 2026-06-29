#!/usr/bin/env python3
"""Fetch the CFI Loan Redash dashboard and send it as an HTML email.

All Redash, SMTP, and email configuration is read from environment variables.
Do not add defaults here: this script is intended to run with secrets/config
provided by the automation environment.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import smtplib
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any


REQUIRED_ENV = (
    "REDASH_URL",
    "REDASH_API_KEY",
    "DASHBOARD_ID",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "EMAIL_FROM",
    "EMAIL_TO",
)

REPORT_TITLE = "CFI Loan T 安卓/IOS-综合-风控日报/周报（规模、转化、风险）"
SMTP_SEND_ATTEMPTS = 3
SMTP_RETRY_DELAY_SECONDS = 5
JOB_POLL_ATTEMPTS = 12
JOB_POLL_SECONDS = 5


class ReportError(Exception):
    """Raised when the report cannot be generated or delivered."""


def log(message: str) -> None:
    timestamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    print(f"[{timestamp}] {message}", flush=True)


@dataclass(frozen=True)
class Config:
    redash_url: str
    redash_api_key: str
    dashboard_id: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: list[str]


@dataclass
class WidgetReport:
    title: str
    query_id: int | str | None
    columns: list[dict[str, Any]]
    rows: list[Any]
    error: str | None = None
    note: str | None = None


def load_config() -> Config:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise ReportError("Missing required environment variables: " + ", ".join(missing))

    try:
        smtp_port = int(os.environ["SMTP_PORT"])
    except ValueError as exc:
        raise ReportError("SMTP_PORT must be an integer") from exc

    recipients = [
        address.strip()
        for address in os.environ["EMAIL_TO"].split(",")
        if address.strip()
    ]
    if not recipients:
        raise ReportError("EMAIL_TO must contain at least one recipient")

    return Config(
        redash_url=os.environ["REDASH_URL"].rstrip("/"),
        redash_api_key=os.environ["REDASH_API_KEY"],
        dashboard_id=os.environ["DASHBOARD_ID"],
        smtp_host=os.environ["SMTP_HOST"],
        smtp_port=smtp_port,
        smtp_user=os.environ["SMTP_USER"],
        smtp_password=os.environ["SMTP_PASSWORD"],
        email_from=os.environ["EMAIL_FROM"],
        email_to=recipients,
    )


def redacted(message: str, config: Config | None = None) -> str:
    """Mask secrets if an exception message unexpectedly includes them."""
    secret_values = [
        os.environ.get("REDASH_API_KEY"),
        os.environ.get("SMTP_PASSWORD"),
    ]
    if config:
        secret_values.extend([config.redash_api_key, config.smtp_password])

    sanitized = message
    for secret in secret_values:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def redash_json_request(
    config: Config,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> Any:
    data = None
    headers = {
        "Authorization": f"Key {config.redash_api_key}",
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        config.redash_url + path,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        excerpt = body[:500].replace("\n", " ")
        raise ReportError(
            redacted(f"{method} {path} failed with HTTP {exc.code} {exc.reason}: {excerpt}", config)
        ) from exc
    except urllib.error.URLError as exc:
        raise ReportError(redacted(f"{method} {path} failed: {exc.reason}", config)) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        excerpt = body[:500].replace("\n", " ")
        raise ReportError(redacted(f"{method} {path} returned invalid JSON: {excerpt}", config)) from exc


def get_dashboard_widgets(config: Config) -> list[dict[str, Any]]:
    dashboard = redash_json_request(
        config,
        "GET",
        f"/api/dashboards/{config.dashboard_id}",
    )
    if isinstance(dashboard, dict) and isinstance(dashboard.get("dashboard"), dict):
        dashboard = dashboard["dashboard"]

    widgets = dashboard.get("widgets") if isinstance(dashboard, dict) else None
    if not isinstance(widgets, list):
        raise ReportError("Dashboard response does not contain a widgets list")
    return widgets


def extract_query(widget: dict[str, Any]) -> dict[str, Any] | None:
    visualization = widget.get("visualization") or {}
    query = visualization.get("query")
    if isinstance(query, dict):
        return query

    query_id = visualization.get("query_id") or widget.get("query_id")
    if query_id:
        return {"id": query_id}
    return None


def widget_title(widget: dict[str, Any], query: dict[str, Any] | None) -> str:
    visualization = widget.get("visualization") or {}
    options = widget.get("options") or {}
    candidates = (
        options.get("title"),
        visualization.get("name"),
        visualization.get("description"),
        query.get("name") if query else None,
        f"Widget {widget.get('id')}" if widget.get("id") else None,
    )
    for candidate in candidates:
        if candidate:
            return str(candidate)
    return "Untitled widget"


def extract_result_data(payload: Any) -> tuple[list[dict[str, Any]], list[Any]]:
    if not isinstance(payload, dict):
        raise ReportError("Query result response is not a JSON object")

    query_result = payload.get("query_result")
    if isinstance(query_result, dict):
        data = query_result.get("data")
    else:
        data = payload.get("data")

    if not isinstance(data, dict):
        raise ReportError("Query result response does not contain data")

    columns = data.get("columns") or []
    rows = data.get("rows") or []
    if not isinstance(columns, list):
        raise ReportError("Query result columns are not a list")
    if not isinstance(rows, list):
        raise ReportError("Query result rows are not a list")

    normalized_columns: list[dict[str, Any]] = []
    for column in columns:
        if isinstance(column, dict):
            normalized_columns.append(column)
        else:
            normalized_columns.append({"name": str(column), "friendly_name": str(column)})

    if not normalized_columns and rows:
        first_row = rows[0]
        if isinstance(first_row, dict):
            normalized_columns = [{"name": key, "friendly_name": key} for key in first_row]
        elif isinstance(first_row, list):
            normalized_columns = [
                {"name": str(index), "friendly_name": f"Column {index + 1}"}
                for index in range(len(first_row))
            ]

    return normalized_columns, rows


def job_status(payload: Any) -> tuple[str | int | None, int | None]:
    if not isinstance(payload, dict):
        return None, None

    job = payload.get("job")
    if isinstance(job, dict):
        return job.get("id"), job.get("status")
    return payload.get("id"), payload.get("status")


def wait_for_refresh_job(config: Config, refresh_payload: Any, query_id: int | str) -> str | None:
    job_id, status = job_status(refresh_payload)
    if not job_id:
        return "Refresh requested before fetching results."

    for attempt in range(1, JOB_POLL_ATTEMPTS + 1):
        if status == 3:
            return f"Refresh job {job_id} completed before fetching results."
        if status == 4:
            return f"Refresh job {job_id} failed; fetched cached results instead."
        if attempt < JOB_POLL_ATTEMPTS:
            time.sleep(JOB_POLL_SECONDS)
        payload = redash_json_request(config, "GET", f"/api/jobs/{job_id}", timeout=30)
        _, status = job_status(payload)

    return f"Refresh job {job_id} did not finish after polling; fetched latest available results."


def fetch_query_result(config: Config, query_id: int | str) -> tuple[list[dict[str, Any]], list[Any], str | None]:
    refresh_note = None
    try:
        refresh_payload = redash_json_request(config, "POST", f"/api/queries/{query_id}/refresh", {})
        refresh_note = wait_for_refresh_job(config, refresh_payload, query_id)
    except ReportError as exc:
        refresh_note = f"Refresh was skipped or failed; fetched cached results instead. Detail: {exc}"

    try:
        latest_payload = redash_json_request(
            config,
            "GET",
            f"/api/queries/{query_id}/results/latest",
        )
        columns, rows = extract_result_data(latest_payload)
        return columns, rows, refresh_note
    except ReportError as latest_error:
        fallback_payload = redash_json_request(
            config,
            "GET",
            f"/api/queries/{query_id}/results",
        )
        columns, rows = extract_result_data(fallback_payload)
        note_parts = [f"Latest result endpoint failed; used cached results endpoint. Detail: {latest_error}"]
        if refresh_note:
            note_parts.insert(0, refresh_note)
        return columns, rows, " ".join(note_parts)


def build_widget_reports(config: Config) -> tuple[list[WidgetReport], int]:
    widgets = get_dashboard_widgets(config)
    log(f"Fetched dashboard {config.dashboard_id}; found {len(widgets)} total widget(s).")
    reports: list[WidgetReport] = []
    failures = 0

    for widget in widgets:
        if not isinstance(widget, dict):
            continue
        query = extract_query(widget)
        if not query:
            continue

        title = widget_title(widget, query)
        query_id = query.get("id")
        if query_id is None:
            log(f"Widget '{title}' has query metadata but no query id.")
            reports.append(
                WidgetReport(
                    title=title,
                    query_id=None,
                    columns=[],
                    rows=[],
                    error="Widget contains query metadata but no query id.",
                )
            )
            failures += 1
            continue

        try:
            log(f"Fetching widget '{title}' (query {query_id}).")
            columns, rows, note = fetch_query_result(config, query_id)
            log(
                f"Fetched widget '{title}' (query {query_id}): "
                f"{len(rows)} row(s), {len(columns)} column(s)."
            )
            reports.append(
                WidgetReport(
                    title=title,
                    query_id=query_id,
                    columns=columns,
                    rows=rows,
                    note=note,
                )
            )
        except ReportError as exc:
            log(f"Failed to fetch widget '{title}' (query {query_id}): {exc}")
            reports.append(
                WidgetReport(
                    title=title,
                    query_id=query_id,
                    columns=[],
                    rows=[],
                    error=str(exc),
                )
            )
            failures += 1

    if not reports:
        log("No widgets containing queries were found on this dashboard.")
        reports.append(
            WidgetReport(
                title="Dashboard widgets",
                query_id=None,
                columns=[],
                rows=[],
                error="No widgets containing queries were found on this dashboard.",
            )
        )
        failures += 1

    return reports, failures


def failure_report(error: Exception) -> tuple[list[WidgetReport], int]:
    return (
        [
            WidgetReport(
                title="Redash dashboard extraction",
                query_id=None,
                columns=[],
                rows=[],
                error=redacted(str(error)),
            )
        ],
        1,
    )


def column_label(column: dict[str, Any]) -> str:
    return str(column.get("friendly_name") or column.get("name") or "")


def column_key(column: dict[str, Any], index: int) -> str:
    return str(column.get("name") or column.get("friendly_name") or index)


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def row_value(row: Any, columns: list[dict[str, Any]], index: int, column: dict[str, Any]) -> Any:
    if isinstance(row, dict):
        key = column_key(column, index)
        if key in row:
            return row[key]
        friendly = column.get("friendly_name")
        if friendly in row:
            return row[friendly]
        return ""
    if isinstance(row, list) and index < len(row):
        return row[index]
    return row if len(columns) == 1 else ""


def render_table(report: WidgetReport) -> str:
    if report.error:
        return f'<p class="error">API failed: {html.escape(report.error)}</p>'

    if not report.rows:
        return '<p class="empty">No data returned for this widget.</p>'

    header = "".join(
        f"<th>{html.escape(column_label(column))}</th>" for column in report.columns
    )
    body_rows = []
    for row in report.rows:
        cells = "".join(
            "<td>{}</td>".format(
                html.escape(format_cell(row_value(row, report.columns, index, column)))
            )
            for index, column in enumerate(report.columns)
        )
        body_rows.append(f"<tr>{cells}</tr>")

    return (
        '<div class="table-wrap"><table>'
        f"<thead><tr>{header}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )


def build_html(reports: list[WidgetReport], generated_at: dt.datetime) -> str:
    sections = []
    for index, report in enumerate(reports, start=1):
        query_label = f"Query ID: {html.escape(str(report.query_id))}" if report.query_id else "Query ID: N/A"
        note = f'<p class="note">{html.escape(report.note)}</p>' if report.note else ""
        sections.append(
            "<section>"
            f"<h2>{index}. {html.escape(report.title)}</h2>"
            f'<p class="meta">{query_label} | Rows: {len(report.rows)}</p>'
            f"{note}"
            f"{render_table(report)}"
            "</section>"
        )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, "Microsoft YaHei", sans-serif; color: #222; line-height: 1.4; }}
    h1 {{ font-size: 22px; margin-bottom: 8px; }}
    h2 {{ font-size: 17px; margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
    .meta, .note {{ color: #666; font-size: 13px; }}
    .error {{ color: #b00020; font-weight: 600; }}
    .empty {{ color: #8a5a00; font-weight: 600; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 13px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f5f7fa; font-weight: 600; }}
    tr:nth-child(even) td {{ background: #fafafa; }}
  </style>
</head>
<body>
  <h1>{html.escape(REPORT_TITLE)}</h1>
  <p class="meta">Generated at: {html.escape(generated_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip())}</p>
  {''.join(sections)}
</body>
</html>"""


def build_plain_text(reports: list[WidgetReport], generated_at: dt.datetime) -> str:
    lines = [
        REPORT_TITLE,
        f"Generated at: {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z').strip()}",
        "",
    ]
    for index, report in enumerate(reports, start=1):
        status = "ERROR" if report.error else "OK"
        lines.append(
            f"{index}. {report.title} | Query ID: {report.query_id or 'N/A'} | Rows: {len(report.rows)} | {status}"
        )
        if report.error:
            lines.append(f"   API failed: {report.error}")
        elif not report.rows:
            lines.append("   No data returned for this widget.")
    return "\n".join(lines)


def send_email_once(
    config: Config,
    subject: str,
    plain_text: str,
    html_body: str,
) -> None:
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = config.email_from
    message["To"] = ", ".join(config.email_to)
    message.attach(MIMEText(plain_text, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    if config.smtp_port == 465:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context, timeout=60) as smtp:
            smtp.login(config.smtp_user, config.smtp_password)
            smtp.sendmail(config.email_from, config.email_to, message.as_string())
    else:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=60) as smtp:
            smtp.starttls(context=context)
            smtp.login(config.smtp_user, config.smtp_password)
            smtp.sendmail(config.email_from, config.email_to, message.as_string())


def send_email(
    config: Config,
    subject: str,
    plain_text: str,
    html_body: str,
) -> None:
    for attempt in range(1, SMTP_SEND_ATTEMPTS + 1):
        try:
            log(
                f"Sending email attempt {attempt}/{SMTP_SEND_ATTEMPTS} "
                f"to {len(config.email_to)} recipient(s)."
            )
            send_email_once(config, subject, plain_text, html_body)
            return
        except smtplib.SMTPException as exc:
            if attempt == SMTP_SEND_ATTEMPTS:
                raise
            log(
                f"SMTP send attempt {attempt} failed: {redacted(str(exc), config)}. "
                f"Retrying in {SMTP_RETRY_DELAY_SECONDS} seconds."
            )
            time.sleep(SMTP_RETRY_DELAY_SECONDS)


def run() -> int:
    config = load_config()
    generated_at = dt.datetime.now().astimezone()
    subject = f"CFI Loan 风控日报 - {generated_at:%Y-%m-%d}"

    log("Starting CFI Loan Redash dashboard extraction.")
    try:
        reports, widget_failures = build_widget_reports(config)
    except Exception as exc:
        log(f"Dashboard extraction failed: {redacted(str(exc), config)}")
        reports, widget_failures = failure_report(exc)

    plain_text = build_plain_text(reports, generated_at)
    html_body = build_html(reports, generated_at)
    log(
        f"Built report email with {len(reports)} widget section(s) "
        f"and {widget_failures} widget failure(s)."
    )

    send_email(config, subject, plain_text, html_body)
    log(
        f"Sent report to {len(config.email_to)} recipient(s): "
        f"{len(reports)} widget section(s), {widget_failures} widget failure(s)."
    )

    return 1 if widget_failures else 0


def main() -> int:
    try:
        return run()
    except Exception as exc:
        sanitized = redacted(str(exc))
        print(f"ERROR: {sanitized}", file=sys.stderr)
        print(redacted(traceback.format_exc()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
