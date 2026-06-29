import datetime as dt
import os
import smtplib
import unittest
from unittest import mock

import cfi_loan_redash_report as report


def sample_config() -> report.Config:
    return report.Config(
        redash_url="https://redash.example.com",
        redash_api_key="redash-secret",
        dashboard_id="94",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_user="sender@example.com",
        smtp_password="smtp-secret",
        email_from="sender@example.com",
        email_to=["a@example.com", "b@example.com"],
    )


class RedashReportTests(unittest.TestCase):
    def test_load_config_reads_required_environment(self) -> None:
        env = {
            "REDASH_URL": "https://redash.example.com/",
            "REDASH_API_KEY": "redash-secret",
            "DASHBOARD_ID": "94",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "SMTP_USER": "sender@example.com",
            "SMTP_PASSWORD": "smtp-secret",
            "EMAIL_FROM": "sender@example.com",
            "EMAIL_TO": "a@example.com, b@example.com",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = report.load_config()

        self.assertEqual(config.redash_url, "https://redash.example.com")
        self.assertEqual(config.smtp_port, 465)
        self.assertEqual(config.email_to, ["a@example.com", "b@example.com"])

    def test_build_widget_reports_refreshes_and_falls_back_to_results(self) -> None:
        config = sample_config()
        calls = []

        def fake_request(_config, method, path, payload=None, timeout=60):
            calls.append((method, path))
            if path == "/api/dashboards/94":
                return {
                    "widgets": [
                        {
                            "id": 1,
                            "options": {"title": "Risk table"},
                            "visualization": {"query": {"id": 3155, "name": "Risk query"}},
                        }
                    ]
                }
            if path == "/api/queries/3155/refresh":
                return {"job": {"id": "job-1", "status": 3}}
            if path == "/api/queries/3155/results/latest":
                raise report.ReportError("latest returned invalid JSON")
            if path == "/api/queries/3155/results":
                return {
                    "query_result": {
                        "data": {
                            "columns": [{"name": "loan_count", "friendly_name": "Loan Count"}],
                            "rows": [{"loan_count": 12}],
                        }
                    }
                }
            raise AssertionError(f"unexpected request: {method} {path}")

        with mock.patch.object(report, "redash_json_request", side_effect=fake_request):
            reports, failures = report.build_widget_reports(config)

        self.assertEqual(failures, 0)
        self.assertEqual(reports[0].title, "Risk table")
        self.assertEqual(reports[0].rows, [{"loan_count": 12}])
        self.assertIn(("POST", "/api/queries/3155/refresh"), calls)
        self.assertIn(("GET", "/api/queries/3155/results/latest"), calls)
        self.assertIn(("GET", "/api/queries/3155/results"), calls)

    def test_html_marks_widget_errors_and_empty_data(self) -> None:
        generated_at = dt.datetime(2026, 6, 29, 10, 12, tzinfo=dt.timezone.utc)
        html = report.build_html(
            [
                report.WidgetReport(
                    title="Broken widget",
                    query_id=123,
                    columns=[],
                    rows=[],
                    error="HTTP 500 from Redash",
                ),
                report.WidgetReport(title="Empty widget", query_id=456, columns=[], rows=[]),
            ],
            generated_at,
        )

        self.assertIn("API failed: HTTP 500 from Redash", html)
        self.assertIn("No data returned for this widget.", html)
        self.assertIn(report.REPORT_TITLE, html)

    def test_send_email_once_uses_smtp_ssl_for_port_465(self) -> None:
        config = sample_config()
        smtp_instance = mock.Mock()
        smtp_context = mock.Mock()
        smtp_context.__enter__ = mock.Mock(return_value=smtp_instance)
        smtp_context.__exit__ = mock.Mock(return_value=False)

        with mock.patch.object(report.ssl, "create_default_context", return_value=mock.Mock()):
            with mock.patch.object(report.smtplib, "SMTP_SSL", return_value=smtp_context) as smtp_ssl:
                report.send_email_once(config, "Subject", "plain", "<p>html</p>")

        smtp_ssl.assert_called_once()
        smtp_instance.login.assert_called_once_with("sender@example.com", "smtp-secret")
        smtp_instance.sendmail.assert_called_once()
        args = smtp_instance.sendmail.call_args.args
        self.assertEqual(args[0], "sender@example.com")
        self.assertEqual(args[1], ["a@example.com", "b@example.com"])
        self.assertIn("multipart/alternative", args[2])
        self.assertIn("text/html", args[2])

    def test_send_email_retries_smtp_errors_without_leaking_password(self) -> None:
        config = sample_config()
        with mock.patch.object(report, "SMTP_RETRY_DELAY_SECONDS", 0):
            with mock.patch.object(
                report,
                "send_email_once",
                side_effect=smtplib.SMTPAuthenticationError(535, b"smtp-secret rejected"),
            ):
                with mock.patch.object(report, "log") as log:
                    with self.assertRaises(smtplib.SMTPAuthenticationError):
                        report.send_email(config, "Subject", "plain", "<p>html</p>")

        logged = "\n".join(call.args[0] for call in log.call_args_list)
        self.assertNotIn("smtp-secret", logged)
        self.assertIn("[REDACTED]", logged)


if __name__ == "__main__":
    unittest.main()
