import unittest
from datetime import datetime

from scripts.send_redash_dashboard_email import (
    QueryWidget,
    QueryWidgetResult,
    ReportError,
    columns_for_appendix,
    filter_date_columns,
    format_value,
    prepare_report_data,
    render_email_html,
)


def widget_result(title, columns, rows):
    return QueryWidgetResult(
        widget=QueryWidget(widget_id=1, query_id=101, title=title, query_name=title),
        columns=columns,
        rows=rows,
    )


class DateColumnFilteringTests(unittest.TestCase):
    def test_keeps_only_last_seven_four_digit_mmdd_columns_sorted_ascending(self):
        columns = [
            "指标分类",
            "指标项",
            "0628",
            "0427-0503",
            "0622",
            "06/23",
            "0627",
            "20260624",
            "0624",
            "0625",
            "0623",
            "week",
            "0626",
            "合计",
            "0619",
        ]

        included, excluded = filter_date_columns(columns)

        self.assertEqual(included, ["0622", "0623", "0624", "0625", "0626", "0627", "0628"])
        self.assertIn("0427-0503", excluded)
        self.assertIn("06/23", excluded)
        self.assertIn("20260624", excluded)
        self.assertIn("week", excluded)
        self.assertIn("合计", excluded)
        self.assertNotIn("指标分类", excluded)

    def test_zero_valid_date_columns_raises_with_raw_columns(self):
        result = widget_result(
            "规模",
            ["指标分类", "指标项", "0427-0503", "2026-06-22", "总计"],
            [{"指标项": "日均注册人数", "0427-0503": 100}],
        )

        with self.assertRaisesRegex(ReportError, "Zero valid 4-digit MMDD date columns"):
            prepare_report_data([result])


class ReportRenderingTests(unittest.TestCase):
    def test_rendered_main_table_contains_only_four_digit_dates(self):
        result = widget_result(
            "规模 转化 汇总",
            ["指标分类", "一级序号", "指标项", "二级序号", "样本", "0622", "0623", "0622-0628", "2026-06-24"],
            [
                {
                    "指标分类": "规模",
                    "一级序号": "1",
                    "指标项": "日均注册人数",
                    "二级序号": "1",
                    "样本": "安卓/IOS",
                    "0622": 6765,
                    "0623": 7000,
                    "0622-0628": 6800,
                },
                {
                    "指标分类": "转化",
                    "一级序号": "2",
                    "指标项": "安装注册率",
                    "二级序号": "1",
                    "样本": "安卓/IOS",
                    "0622": 0.8523,
                    "0623": "86.2%",
                },
            ],
        )

        report = prepare_report_data([result])
        html = render_email_html(
            "CFI Loan T 安卓/ IOS-综合-风控日报/周报(规模、转化、风险)",
            report,
            now=datetime(2026, 6, 29, 10, 42, 0),
        )

        self.assertIn("<th>0622</th>", html)
        self.assertIn("<th>0623</th>", html)
        self.assertNotIn("0622-0628", html)
        self.assertNotIn("2026-06-24", html)
        self.assertIn("数据日期范围：0622 - 0623", html)
        self.assertIn("6765.00", html)
        self.assertIn("85.23%", html)
        self.assertIn("86.20%", html)
        self.assertIn('class="section" rowspan="1"', html)

    def test_multiple_widgets_merge_by_title_and_appendix_filters_date_like_columns(self):
        scale = widget_result(
            "规模",
            ["指标项", "0622", "0623"],
            [{"指标项": "日均注册人数", "0622": 1, "0623": 2}],
        )
        conversion = widget_result(
            "转化",
            ["指标项", "0622", "0623"],
            [{"指标项": "安装注册率", "0622": 0.1, "0623": 0.2}],
        )
        unmapped = widget_result(
            "其他",
            ["指标项", "0622", "0427-0503", "总计", "备注"],
            [{"指标项": "其他指标", "0622": 3, "0427-0503": 4, "总计": 7, "备注": "ok"}],
        )

        report = prepare_report_data([scale, conversion, unmapped])
        html = render_email_html("Dashboard", report, now=datetime(2026, 6, 29, 10, 42, 0))

        self.assertEqual([row.section for row in report.rows], ["规模-安卓/IOS 综合", "转化-安卓/IOS 综合"])
        self.assertIn("附录：无法映射的 widget", html)
        self.assertIn("<th>备注</th>", html)
        self.assertNotIn("0427-0503", html)
        self.assertNotIn("总计", html)

    def test_summary_widget_is_preferred_over_other_widgets(self):
        summary = widget_result(
            "规模 转化 汇总",
            ["指标项", "0622"],
            [{"指标项": "日均注册人数", "0622": 1}],
        )
        scale = widget_result(
            "规模",
            ["指标项", "0622"],
            [{"指标项": "不应进入主表", "0622": 2}],
        )

        report = prepare_report_data([summary, scale])

        self.assertEqual([row.metric_name for row in report.rows], ["日均注册人数"])
        self.assertEqual([appendix.combined_name for appendix in report.appendices], ["规模 规模"])

    def test_appendix_column_filtering_drops_range_and_total_columns(self):
        columns = columns_for_appendix(["指标项", "0622", "0623", "0622-0628", "06/22", "合计"], ["0622"])

        self.assertEqual(columns, ["指标项", "0622"])


class ValueFormattingTests(unittest.TestCase):
    def test_numbers_percentages_and_empty_values_are_formatted(self):
        self.assertEqual(format_value(6765, "日均注册人数"), ("6765.00", "number"))
        self.assertEqual(format_value(0.8523, "安装注册率"), ("85.23%", "percent"))
        self.assertEqual(format_value("85.23%", "安装注册率"), ("85.23%", "percent"))
        self.assertEqual(format_value(None, "日均注册人数"), ("-", "number"))


if __name__ == "__main__":
    unittest.main()
