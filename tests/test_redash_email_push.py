import io
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import redash_email_push as push  # noqa: E402


class FakeResponse:
    def __init__(self, body):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


class FakeSMTP:
    def __init__(self):
        self.login_args = None
        self.messages = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message):
        self.messages.append(message)


class RedashEmailPushTests(unittest.TestCase):
    def test_select_date_columns_keeps_only_latest_7_mmdd_columns(self):
        columns = [
            "指标分类",
            "一级序号",
            "指标项",
            "二级序号",
            "样本",
            "0427",
            "0427-0503",
            "0503",
            "0616",
            "0617",
            "0618",
            "0619",
            "0620",
            "0621",
            "0622",
            "0622-0628",
            "2026-06-22",
            "06/22",
            "20260622",
            "合计",
            "week",
        ]

        included, excluded = push.select_date_columns(columns)

        self.assertEqual(included, ["0616", "0617", "0618", "0619", "0620", "0621", "0622"])
        self.assertIn("0427-0503", excluded)
        self.assertIn("0622-0628", excluded)
        self.assertIn("2026-06-22", excluded)
        self.assertIn("06/22", excluded)
        self.assertIn("20260622", excluded)
        self.assertIn("合计", excluded)
        self.assertIn("week", excluded)
        self.assertIn("0427", excluded)
        self.assertIn("0503", excluded)

    def test_select_date_columns_reports_original_columns_when_none_valid(self):
        columns = ["指标分类", "2026-06-22", "06/22", "合计", "week"]

        with self.assertRaisesRegex(ValueError, "Original columns"):
            push.select_date_columns(columns)

    def test_extract_query_data_supports_results_fallback_payload(self):
        payload = {
            "query_results": [
                {
                    "data": {
                        "columns": [{"name": "指标分类"}, {"name": "0622"}],
                        "rows": [{"指标分类": "规模-安卓/IOS 综合", "0622": 10}],
                    }
                }
            ]
        }

        result = push.extract_query_data(payload)

        self.assertEqual(result.columns, ["指标分类", "0622"])
        self.assertEqual(result.rows, [{"指标分类": "规模-安卓/IOS 综合", "0622": 10}])

    def test_redash_client_refresh_polls_and_results_fallback(self):
        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request.get_method(), request.full_url))
            url = request.full_url
            if url.endswith("/api/queries/3157/refresh"):
                return FakeResponse('{"job": {"id": "job-1", "status": 1}}')
            if url.endswith("/api/jobs/job-1"):
                return FakeResponse('{"job": {"status": 3}}')
            if url.endswith("/api/queries/3157/results/latest"):
                raise HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"no latest"))
            if url.endswith("/api/queries/3157/results"):
                return FakeResponse(
                    '{"query_results": [{"data": {"columns": [{"name": "指标分类"}, {"name": "0622"}], '
                    '"rows": [{"指标分类": "规模-安卓/IOS 综合", "0622": 1}]}}]}'
                )
            raise AssertionError(f"Unexpected URL: {url}")

        client = push.RedashClient("https://redash.example.com", "secret")
        with mock.patch.object(push, "urlopen", side_effect=fake_urlopen):
            client.refresh_query("3157", timeout_seconds=5, poll_interval=0)
            data = client.fetch_query_results("3157")

        self.assertEqual(data.rows[0]["0622"], 1)
        self.assertEqual(
            calls,
            [
                ("POST", "https://redash.example.com/api/queries/3157/refresh"),
                ("GET", "https://redash.example.com/api/jobs/job-1"),
                ("GET", "https://redash.example.com/api/queries/3157/results/latest"),
                ("GET", "https://redash.example.com/api/queries/3157/results"),
            ],
        )

    def test_build_html_email_formats_values_and_rowspans(self):
        rows = [
            {
                "指标分类": "规模-安卓/IOS 综合",
                "一级序号": "1",
                "指标项": "日均注册人数",
                "二级序号": "1.1",
                "样本": "全量",
                "0621": 123.456,
                "0622": None,
            },
            {
                "指标分类": "规模-安卓/IOS 综合",
                "一级序号": "1",
                "指标项": "安装注册率",
                "二级序号": "1.2",
                "样本": "全量",
                "0621": 0.12345,
                "0622": "17.5%",
            },
        ]

        html = push.build_html_email(
            "CFI Loan T 安卓/ IOS-综合-风控日报/周报(规模、转化、风险)",
            rows,
            ["0621", "0622"],
            datetime(2026, 6, 30, 1, 45, 0),
        )

        self.assertIn("<th>0621</th><th>0622</th>", html)
        self.assertIn('class="group-row"', html)
        self.assertIn('rowspan="2"', html)
        self.assertIn("123.46", html)
        self.assertIn("12.35%", html)
        self.assertIn("17.50%", html)
        self.assertIn("<td>-</td>", html)
        self.assertIn("#f5f5f5", html)
        self.assertIn("#e8f4fc", html)
        self.assertIn("1px solid #ccc", html)

    def test_send_email_uses_smtp_ssl_multipart_alternative(self):
        config = push.Config(
            redash_url="https://redash.example.com/",
            redash_api_key="secret",
            query_id="3157",
            dashboard_id=None,
            dashboard_name="Dashboard",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_user="user",
            smtp_password="password",
            email_from="from@example.com",
            email_to=["a@example.com", "b@example.com"],
        )
        fake_smtp = FakeSMTP()

        with mock.patch.object(push.smtplib, "SMTP_SSL", return_value=fake_smtp) as smtp_ssl:
            push.send_email(config, "Subject", "plain", "<p>html</p>")

        smtp_ssl.assert_called_once()
        self.assertEqual(smtp_ssl.call_args.args[:2], ("smtp.example.com", 465))
        self.assertEqual(fake_smtp.login_args, ("user", "password"))
        self.assertEqual(len(fake_smtp.messages), 1)
        message = fake_smtp.messages[0]
        self.assertEqual(message["Subject"], "Subject")
        self.assertEqual(message["From"], "from@example.com")
        self.assertEqual(message["To"], "a@example.com, b@example.com")
        self.assertTrue(message.is_multipart())

    def test_dashboard_name_mismatch_warns_without_blocking(self):
        config = push.Config(
            redash_url="https://redash.example.com/",
            redash_api_key="secret",
            query_id="3157",
            dashboard_id="dash-1",
            dashboard_name="Expected Dashboard",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_user="user",
            smtp_password="password",
            email_from="from@example.com",
            email_to=["to@example.com"],
        )
        client = mock.Mock()
        client.get_dashboard_name.return_value = "Different Dashboard"

        with self.assertLogs(push.LOGGER, level="WARNING") as captured:
            push.validate_dashboard_name(client, config)

        self.assertIn("Dashboard name mismatch", "\n".join(captured.output))

    def test_load_query_id_defaults_to_3157_and_rejects_other_values(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(push.load_query_id(), "3157")

        with mock.patch.dict(os.environ, {"QUERY_ID": "3155"}, clear=True):
            with self.assertRaisesRegex(push.ConfigError, "3157"):
                push.load_query_id()


if __name__ == "__main__":
    unittest.main()
