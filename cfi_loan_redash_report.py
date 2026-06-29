#!/usr/bin/env python3
"""Fetch a Redash dashboard and send the results as an HTML email."""

from __future__ import annotations

import html
import json
import logging
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any


REPORT_TITLE = "CFI Loan T 安卓/IOS-综合-风控日报/周报（规模、转化、风险）"
REQUIRED_ENV_VARS = (
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
REQUEST_TIMEOUT_SECONDS = 60
JOB_POLL_INTERVAL_SECONDS = 2
JOB_POLL_TIMEOUT_SECONDS = 60


class ReportError(Exception):
    """Base exception for expected report generation failures."""


class RedashError(ReportError):
    """Raised when Redash API access fails."""


class EmailSendError(ReportError):
    """Raised when SMTP delivery fails."""


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

    @classmethod
    def from_env(cls) -> "Config":
        missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
        if missing:
            raise ReportError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        raw_port = os.environ["SMTP_PORT"]
        try:
            smtp_port = int(raw_port)
        except ValueError as exc:
            raise ReportError("SMTP_PORT must be an integer") from exc

        recipients = [
            address.strip()
            for address in os.environ["EMAIL_TO"].split(",")
            if address.strip()
        ]
        if not recipients:
            raise ReportError("EMAIL_TO must contain at least one recipient")

        return cls(
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


@dataclass
class WidgetResult:
    title: str
    query_id: int | str | None
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any] | list[Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class RedashClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key

    def get_json(self, path: str) -> Any:
        return self._request_json("GET", path)

    def post_json(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request_json("POST", path, payload if payload is not None else {})

    def _request_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        body = None
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RedashError(
                f"{method} {path} failed with HTTP {exc.code} {exc.reason}: "
                f"{_truncate(error_body)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RedashError(f"{method} {path} failed: {exc.reason}") from exc

        if not response_body.strip():
            return {}
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise RedashError(
                f"{method} {path} returned invalid JSON: {_truncate(response_body)}"
            ) from exc

    def refresh_query(self, query_id: int | str) -> tuple[bool, str | None]:
        """Best-effort query refresh. Returns (succeeded, warning)."""
        try:
            refresh_response = self.post_json(f"/api/queries/{query_id}/refresh")
        except RedashError as exc:
            return False, f"Refresh request failed; using cached results. {exc}"

        job = refresh_response.get("job") if isinstance(refresh_response, dict) else None
        job_id = job.get("id") if isinstance(job, dict) else None
        if not job_id:
            return True, None

        deadline = time.monotonic() + JOB_POLL_TIMEOUT_SECONDS
        last_status: Any = None
        while time.monotonic() < deadline:
            try:
                job_response = self.get_json(f"/api/jobs/{job_id}")
            except RedashError as exc:
                return False, f"Refresh job polling failed; using cached results. {exc}"

            job_data = job_response.get("job") if isinstance(job_response, dict) else None
            if not isinstance(job_data, dict):
                return False, "Refresh job response was malformed; using cached results."

            last_status = job_data.get("status")
            if last_status == 3:
                return True, None
            if last_status in (4, 5):
                message = job_data.get("error") or job_data.get("result") or "unknown error"
                return False, f"Refresh job failed; using cached results. {message}"
            time.sleep(JOB_POLL_INTERVAL_SECONDS)

        return (
            False,
            f"Refresh job timed out after {JOB_POLL_TIMEOUT_SECONDS}s "
            f"(last status: {last_status}); using cached results.",
        )

    def latest_query_result(self, query_id: int | str) -> tuple[Any, str | None]:
        try:
            return self.get_json(f"/api/queries/{query_id}/results/latest"), None
        except RedashError as latest_error:
            try:
                return self.get_json(f"/api/queries/{query_id}/results"), (
                    f"Latest results endpoint failed; used /results fallback. "
                    f"{latest_error}"
                )
            except RedashError as fallback_error:
                raise RedashError(
                    f"Both latest and fallback result endpoints failed. "
                    f"Latest error: {latest_error}. Fallback error: {fallback_error}"
                ) from fallback_error


def _truncate(value: str, limit: int = 500) -> str:
    value = value.replace("\n", "\\n").replace("\r", "\\r")
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def extract_widgets(dashboard_payload: Any) -> list[dict[str, Any]]:
    if not isinstance(dashboard_payload, dict):
        raise RedashError("Dashboard response was not a JSON object")

    if isinstance(dashboard_payload.get("widgets"), list):
        return dashboard_payload["widgets"]

    dashboard = dashboard_payload.get("dashboard")
    if isinstance(dashboard, dict) and isinstance(dashboard.get("widgets"), list):
        return dashboard["widgets"]

    raise RedashError("Dashboard response did not contain a widgets list")


def get_query_id(widget: dict[str, Any]) -> int | str | None:
    visualization = widget.get("visualization")
    if isinstance(visualization, dict):
        query = visualization.get("query")
        if isinstance(query, dict) and query.get("id") is not None:
            return query["id"]
        if visualization.get("query_id") is not None:
            return visualization["query_id"]

    query = widget.get("query")
    if isinstance(query, dict) and query.get("id") is not None:
        return query["id"]
    if widget.get("query_id") is not None:
        return widget["query_id"]

    return None


def widget_title(widget: dict[str, Any], fallback_query_id: int | str | None) -> str:
    options = widget.get("options")
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except json.JSONDecodeError:
            options = {}
    if isinstance(options, dict):
        title = options.get("title")
        if title:
            return str(title)

    visualization = widget.get("visualization")
    if isinstance(visualization, dict):
        for key in ("name", "title"):
            value = visualization.get(key)
            if value:
                return str(value)
        query = visualization.get("query")
        if isinstance(query, dict) and query.get("name"):
            return str(query["name"])

    if fallback_query_id is not None:
        return f"Query {fallback_query_id}"
    return "Untitled widget"


def parse_query_result(result_payload: Any) -> tuple[list[str], list[dict[str, Any] | list[Any]]]:
    query_result = result_payload
    if isinstance(result_payload, dict) and isinstance(
        result_payload.get("query_result"), dict
    ):
        query_result = result_payload["query_result"]

    if not isinstance(query_result, dict):
        raise RedashError("Query result response was not a JSON object")

    data = query_result.get("data")
    if not isinstance(data, dict):
        raise RedashError("Query result response did not contain data")

    raw_columns = data.get("columns") or []
    rows = data.get("rows") or []
    if not isinstance(raw_columns, list):
        raise RedashError("Query result columns were not a list")
    if not isinstance(rows, list):
        raise RedashError("Query result rows were not a list")

    columns: list[str] = []
    for column in raw_columns:
        if isinstance(column, dict):
            columns.append(str(column.get("name") or column.get("friendly_name") or ""))
        else:
            columns.append(str(column))

    if not columns and rows and isinstance(rows[0], dict):
        columns = [str(key) for key in rows[0].keys()]

    return columns, rows


def fetch_widget_results(client: RedashClient, dashboard_id: str) -> list[WidgetResult]:
    dashboard = client.get_json(f"/api/dashboards/{dashboard_id}")
    widgets = extract_widgets(dashboard)
    results: list[WidgetResult] = []

    for widget in widgets:
        if not isinstance(widget, dict):
            continue
        query_id = get_query_id(widget)
        if query_id is None:
            continue

        result = WidgetResult(title=widget_title(widget, query_id), query_id=query_id)
        logging.info("Fetching widget '%s' (query_id=%s)", result.title, query_id)

        refreshed, refresh_warning = client.refresh_query(query_id)
        if refresh_warning:
            logging.warning("Widget '%s': %s", result.title, refresh_warning)
            result.warnings.append(refresh_warning)
        elif refreshed:
            logging.info("Widget '%s': refresh completed or accepted", result.title)

        try:
            query_payload, latest_warning = client.latest_query_result(query_id)
            if latest_warning:
                logging.warning("Widget '%s': %s", result.title, latest_warning)
                result.warnings.append(latest_warning)
            result.columns, result.rows = parse_query_result(query_payload)
            logging.info(
                "Widget '%s': fetched %d rows and %d columns",
                result.title,
                len(result.rows),
                len(result.columns),
            )
        except RedashError as exc:
            result.error = str(exc)
            logging.error("Widget '%s': %s", result.title, result.error)

        results.append(result)

    if not results:
        raise RedashError("No query widgets were found in the dashboard")
    return results


def build_plain_text(
    subject: str, generated_at: datetime, widget_results: list[WidgetResult]
) -> str:
    lines = [
        subject,
        REPORT_TITLE,
        f"Generated at: {generated_at.isoformat(timespec='seconds')}",
        "",
    ]
    for result in widget_results:
        status = "ERROR" if result.error else f"{len(result.rows)} rows"
        lines.append(f"- {result.title} (query_id={result.query_id}): {status}")
        for warning in result.warnings:
            lines.append(f"  Warning: {warning}")
        if result.error:
            lines.append(f"  Error: {result.error}")
    return "\n".join(lines)


def build_html(generated_at: datetime, widget_results: list[WidgetResult]) -> str:
    sections = []
    for result in widget_results:
        warnings_html = "".join(
            f"<p class=\"warning\">Warning: {html.escape(warning)}</p>"
            for warning in result.warnings
        )
        if result.error:
            table_html = f"<p class=\"error\">API failed: {html.escape(result.error)}</p>"
        elif not result.rows:
            table_html = "<p class=\"no-data\">No data returned for this widget.</p>"
        else:
            table_html = rows_to_html_table(result.columns, result.rows)

        sections.append(
            "\n".join(
                [
                    "<section>",
                    f"<h2>{html.escape(result.title)}</h2>",
                    f"<p class=\"meta\">Query ID: {html.escape(str(result.query_id))}</p>",
                    warnings_html,
                    table_html,
                    "</section>",
                ]
            )
        )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, Helvetica, sans-serif; color: #1f2937; }}
    h1 {{ font-size: 22px; margin-bottom: 4px; }}
    h2 {{ font-size: 16px; margin-top: 28px; border-bottom: 1px solid #d1d5db; padding-bottom: 6px; }}
    .meta {{ color: #6b7280; font-size: 12px; }}
    .warning {{ color: #92400e; background: #fffbeb; padding: 8px; border-left: 4px solid #f59e0b; }}
    .error {{ color: #991b1b; background: #fef2f2; padding: 8px; border-left: 4px solid #ef4444; }}
    .no-data {{ color: #374151; background: #f3f4f6; padding: 8px; border-left: 4px solid #9ca3af; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 8px; font-size: 12px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 6px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f4f6; font-weight: 600; }}
    tr:nth-child(even) td {{ background: #f9fafb; }}
  </style>
</head>
<body>
  <h1>{html.escape(REPORT_TITLE)}</h1>
  <p class="meta">Generated at: {html.escape(generated_at.isoformat(timespec="seconds"))}</p>
  {"".join(sections)}
</body>
</html>"""


def rows_to_html_table(columns: list[str], rows: list[dict[str, Any] | list[Any]]) -> str:
    if not columns:
        columns = [f"column_{index + 1}" for index in range(max_row_width(rows))]

    header = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
    body_rows = []
    for row in rows:
        cells = []
        if isinstance(row, dict):
            values = [row.get(column, "") for column in columns]
        else:
            values = list(row)
        for value in values:
            cells.append(f"<td>{html.escape(format_cell(value))}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")

    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def max_row_width(rows: list[dict[str, Any] | list[Any]]) -> int:
    widths = []
    for row in rows:
        if isinstance(row, dict):
            widths.append(len(row))
        else:
            widths.append(len(row))
    return max(widths, default=0)


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


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

    try:
        if config.smtp_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                config.smtp_host, config.smtp_port, context=context, timeout=60
            ) as smtp:
                smtp.login(config.smtp_user, config.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=60) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(config.smtp_user, config.smtp_password)
                smtp.send_message(message)
    except smtplib.SMTPException as exc:
        raise EmailSendError(f"SMTP send failed: {exc}") from exc
    except OSError as exc:
        raise EmailSendError(f"SMTP connection failed: {exc}") from exc


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def main() -> int:
    configure_logging()
    try:
        config = Config.from_env()
        logging.info(
            "Loaded configuration: REDASH_URL=%s, DASHBOARD_ID=%s, "
            "SMTP_HOST=%s, SMTP_PORT=%s, EMAIL_TO_COUNT=%d",
            config.redash_url,
            config.dashboard_id,
            config.smtp_host,
            config.smtp_port,
            len(config.email_to),
        )

        client = RedashClient(config.redash_url, config.redash_api_key)
        widget_results = fetch_widget_results(client, config.dashboard_id)

        today = datetime.now().date().isoformat()
        generated_at = datetime.now().astimezone()
        subject = f"CFI Loan 风控日报 - {today}"
        plain_text = build_plain_text(subject, generated_at, widget_results)
        html_body = build_html(generated_at, widget_results)

        send_email(config, subject, plain_text, html_body)
        logging.info("Email sent to %d recipient(s)", len(config.email_to))

        failed_widgets = [result for result in widget_results if result.error]
        if failed_widgets:
            logging.error("%d widget(s) failed; report email was sent with errors", len(failed_widgets))
            return 2
        return 0
    except ReportError as exc:
        logging.exception("Report delivery failed: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level guard for automation logs.
        logging.exception("Unexpected failure: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
