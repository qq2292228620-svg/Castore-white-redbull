"""monitor.py 核心逻辑单元测试。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest
import requests

import monitor


class TestSizeMatching:
    @pytest.mark.parametrize("text", ["XL", "xl", " XL ", "Xl"])
    def test_exact_xl_matches(self, text: str) -> None:
        assert monitor.is_exact_target_size(text) is True

    @pytest.mark.parametrize(
        "text",
        ["2XL", "3XL", "4XL", "XXL", "XXXL", "L", "M", "S", "XS", "XL2", "", None, 123],
    )
    def test_non_xl_strictly_excluded(self, text) -> None:
        assert monitor.is_exact_target_size(text) is False

    def test_parse_shopify_variants_ignores_2xl_3xl_4xl_in_stock(self) -> None:
        payload = {
            "variants": [
                {"id": 1, "title": "S", "option1": "S", "option2": None, "available": True},
                {"id": 2, "title": "XL", "option1": "XL", "option2": None, "available": False},
                {"id": 3, "title": "2XL", "option1": "2XL", "option2": None, "available": True},
                {"id": 4, "title": "3XL", "option1": "3XL", "option2": None, "available": True},
                {"id": 5, "title": "4XL", "option1": "4XL", "option2": None, "available": True},
            ]
        }
        result = monitor.parse_shopify_variants(payload)
        assert result.available is False
        assert result.source == "shopify_api"

    def test_parse_shopify_variants_matches_option2_xl_available(self) -> None:
        payload = {
            "variants": [
                {
                    "id": 10,
                    "title": "Egret/Blue / 2XL",
                    "option1": "Egret/Blue",
                    "option2": "2XL",
                    "available": False,
                },
                {
                    "id": 11,
                    "title": "Egret/Blue / XL",
                    "option1": "Egret/Blue",
                    "option2": "XL",
                    "available": True,
                },
            ]
        }
        result = monitor.parse_shopify_variants(payload)
        assert result.available is True
        assert result.source == "shopify_api"

    def test_parse_shopify_variants_raises_when_missing_xl(self) -> None:
        payload = {
            "variants": [
                {"id": 1, "title": "2XL", "option1": "2XL", "available": True},
                {"id": 2, "title": "3XL", "option1": "3XL", "available": True},
            ]
        }
        with pytest.raises(monitor.ApiFallbackError):
            monitor.parse_shopify_variants(payload)


class TestShopifyApiFallback:
    def test_403_triggers_fallback(self) -> None:
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.status_code = 403
        fake_resp.headers = {"cf-mitigated": "challenge", "Content-Type": "text/html"}
        fake_resp.text = "<html><title>Just a moment...</title></html>"

        fake_session = MagicMock()
        fake_session.get.return_value = fake_resp

        with pytest.raises(monitor.ApiFallbackError):
            monitor.check_stock_via_shopify_api(session=fake_session)

    def test_timeout_triggers_fallback(self) -> None:
        fake_session = MagicMock()
        fake_session.get.side_effect = requests.Timeout("timed out")

        with pytest.raises(monitor.ApiFallbackError):
            monitor.check_stock_via_shopify_api(session=fake_session)

    def test_check_stock_prefers_reader_when_direct_blocked(self) -> None:
        """直连被 Cloudflare 拦截时应先走转发通道，而不是直接上 Playwright。"""
        expected = monitor.StockCheckResult(
            available=True, source="reader", detail="XL available=True"
        )
        with (
            patch.object(
                monitor,
                "check_stock_via_shopify_api",
                side_effect=monitor.ApiFallbackError("403"),
            ),
            patch.object(
                monitor, "check_stock_via_reader", return_value=expected
            ) as mock_reader,
            patch.object(monitor, "check_stock_via_playwright") as mock_pw,
        ):
            result = monitor.check_stock()
            assert result == expected
            mock_reader.assert_called_once()
            mock_pw.assert_not_called()

    def test_check_stock_falls_back_to_playwright_when_reader_fails(self) -> None:
        expected = monitor.StockCheckResult(
            available=False, source="playwright", detail="XL disabled"
        )
        with (
            patch.object(
                monitor,
                "check_stock_via_shopify_api",
                side_effect=monitor.ApiFallbackError("403"),
            ),
            patch.object(
                monitor,
                "check_stock_via_reader",
                side_effect=monitor.ApiFallbackError("429"),
            ),
            patch.object(
                monitor, "check_stock_via_playwright", return_value=expected
            ) as mock_pw,
        ):
            result = monitor.check_stock()
            assert result == expected
            mock_pw.assert_called_once()

    def test_check_stock_raises_when_all_channels_fail(self) -> None:
        with (
            patch.object(
                monitor,
                "check_stock_via_shopify_api",
                side_effect=monitor.ApiFallbackError("403"),
            ),
            patch.object(
                monitor,
                "check_stock_via_reader",
                side_effect=monitor.ApiFallbackError("429"),
            ),
            patch.object(
                monitor,
                "check_stock_via_playwright",
                side_effect=monitor.CloudflareBlockedError("cf"),
            ),
        ):
            with pytest.raises(monitor.CloudflareBlockedError) as excinfo:
                monitor.check_stock()
        message = str(excinfo.value)
        assert "全部通道" in message
        assert "直连" in message and "转发" in message and "playwright" in message


class TestReaderChannel:
    """r.jina.ai 转发通道的解析、验证页识别与重试。"""

    GOOD_XL_OUT = (
        '{"variants":[{"id":2,"title":"XL","option1":"XL","available":false},'
        '{"id":3,"title":"4XL","option1":"4XL","available":true}]}'
    )

    # 真实抓到的转发层响应：Cloudflare 验证页被渲染成 markdown，httpStatus 仍是 200
    CHALLENGE_MARKDOWN = (
        "![Image 1: Icon for castore.com](https://castore.com/favicon.ico)\n\n"
        "## castore.com\n\n## Performing security verification\n\n"
        "This website uses a security service to protect against malicious bots. "
        "This page is displayed while the website verifies you are not a bot."
    )

    @staticmethod
    def _envelope(content: str, http_status: int = 200, code: int = 200) -> MagicMock:
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "code": code,
            "status": 20000 if code == 200 else 40000,
            "data": {"content": content, "httpStatus": http_status},
        }
        return fake_resp

    def test_reader_parses_available_xl(self) -> None:
        content = (
            '{"variants":['
            '{"id":1,"title":"L","option1":"L","available":true},'
            '{"id":2,"title":"XL","option1":"XL","available":true},'
            '{"id":3,"title":"4XL","option1":"4XL","available":true}]}'
        )
        session = MagicMock()
        session.get.return_value = self._envelope(content)
        result = monitor.check_stock_via_reader(session=session)
        assert result.available is True
        assert result.source == "reader"

    def test_reader_ignores_4xl_in_stock(self) -> None:
        session = MagicMock()
        session.get.return_value = self._envelope(self.GOOD_XL_OUT)
        result = monitor.check_stock_via_reader(session=session)
        assert result.available is False

    def test_reader_uses_cache_buster_and_no_cache_header(self) -> None:
        session = MagicMock()
        session.get.return_value = self._envelope(self.GOOD_XL_OUT)
        monitor.check_stock_via_reader(api_url="https://example.com/p.js", session=session)

        called_url = session.get.call_args.args[0]
        kwargs = session.get.call_args.kwargs
        assert called_url.startswith("https://r.jina.ai/https://example.com/p.js?v=")
        assert kwargs["headers"]["X-Cache-Tolerance"] == "0"

    def test_reader_detects_markdown_challenge_page(self) -> None:
        """转发出口被拦时返回的是 markdown 验证页且 httpStatus=200，必须识别为可重试故障。"""
        session = MagicMock()
        session.get.return_value = self._envelope(self.CHALLENGE_MARKDOWN)
        with pytest.raises(monitor._ReaderTransientError):
            monitor._reader_attempt(
                monitor.SHOPIFY_API_URL, monitor.TARGET_SIZE, 30, session
            )

    def test_reader_retries_on_transient_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(monitor.time, "sleep", lambda *_: None)
        session = MagicMock()
        session.get.side_effect = [
            self._envelope(self.CHALLENGE_MARKDOWN),
            self._envelope(self.GOOD_XL_OUT),
        ]
        result = monitor.check_stock_via_reader(session=session, max_attempts=3)
        assert result.available is False
        assert session.get.call_count == 2

    def test_reader_gives_up_after_max_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(monitor.time, "sleep", lambda *_: None)
        session = MagicMock()
        session.get.return_value = self._envelope(self.CHALLENGE_MARKDOWN)
        with pytest.raises(monitor.ApiFallbackError) as excinfo:
            monitor.check_stock_via_reader(session=session, max_attempts=3)
        assert session.get.call_count == 3
        assert "重试 3 次" in str(excinfo.value)

    def test_reader_rate_limited_is_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(monitor.time, "sleep", lambda *_: None)
        limited = MagicMock(spec=requests.Response)
        limited.status_code = 429
        session = MagicMock()
        session.get.side_effect = [limited, self._envelope(self.GOOD_XL_OUT)]
        result = monitor.check_stock_via_reader(session=session, max_attempts=3)
        assert result.available is False

    def test_reader_upstream_403(self) -> None:
        session = MagicMock()
        session.get.return_value = self._envelope("", http_status=403)
        with pytest.raises(monitor.ApiFallbackError):
            monitor.check_stock_via_reader(session=session, max_attempts=1)

    def test_reader_missing_xl_raises_without_retry(self) -> None:
        """尺码缺失是确定性结果，不应浪费重试次数。"""
        content = '{"variants":[{"id":1,"title":"2XL","option1":"2XL","available":true}]}'
        session = MagicMock()
        session.get.return_value = self._envelope(content)
        with pytest.raises(monitor.ApiFallbackError):
            monitor.check_stock_via_reader(session=session, max_attempts=3)
        assert session.get.call_count == 1


class TestDomEvaluation:
    def test_dom_xl_disabled_while_2xl_enabled(self) -> None:
        candidates = [
            {"text": "2XL", "disabled": False, "className": "size-btn"},
            {"text": "3XL", "disabled": False, "className": "size-btn"},
            {"text": "XL", "disabled": True, "className": "size-btn sold-out"},
        ]
        result = monitor.evaluate_dom_size_candidates(candidates)
        assert result is not None
        assert result.available is False

    def test_dom_xl_sold_out_class_on_label(self) -> None:
        candidates = [
            {
                "text": "XL",
                "disabled": False,
                "inputDisabled": False,
                "className": "swatch-label is-unavailable",
            }
        ]
        result = monitor.evaluate_dom_size_candidates(candidates)
        assert result is not None
        assert result.available is False

    def test_dom_xl_clickable_in_stock(self) -> None:
        candidates = [
            {"tag": "LABEL", "text": "XL", "disabled": False, "className": "prd-Options_Button fz-Heading_Five"},
        ]
        result = monitor.evaluate_dom_size_candidates(candidates)
        assert result is not None
        assert result.available is True

    def test_dom_castore_radio_not_disabled_but_paired_label_disabled(self) -> None:
        """回归测试：Castore 缺货尺码的隐藏 radio input 无 disabled，但关联 label 带 prd-Options_Button-disabled。"""
        candidates = [
            {
                "tag": "LABEL",
                "text": "XL\n- Out of stock",
                "value": "XL",
                "disabled": False,
                "inputDisabled": False,
                "className": "prd-Options_Button fz-Heading_Five prd-Options_Button-disabled",
            },
            {
                "tag": "INPUT",
                "text": "",
                "value": "XL",
                "disabled": False,
                "className": "prd-Options_Input util-ScreenReaderOnly",
                "hasPairedLabel": True,
                "labelText": "XL\n- Out of stock",
                "labelClassName": "prd-Options_Button fz-Heading_Five prd-Options_Button-disabled",
            },
            {
                "tag": "LABEL",
                "text": "3XL",
                "value": "3XL",
                "disabled": False,
                "className": "prd-Options_Button fz-Heading_Five",
            },
            {
                "tag": "LABEL",
                "text": "4XL",
                "value": "4XL",
                "disabled": False,
                "className": "prd-Options_Button fz-Heading_Five",
            },
        ]
        xl_result = monitor.evaluate_dom_size_candidates(candidates, target_size="XL")
        assert xl_result is not None
        assert xl_result.available is False

        three_xl_result = monitor.evaluate_dom_size_candidates(
            candidates, target_size="3XL"
        )
        assert three_xl_result is not None
        assert three_xl_result.available is True


class TestBarkNotification:
    def test_build_bark_push_url_encoding_and_trailing_slash(self) -> None:
        url = monitor.build_bark_push_url(
            bark_url="https://api.day.app/MySecretKey/",
            title=monitor.BARK_TITLE,
            body=monitor.BARK_BODY,
        )
        expected_title = quote("红牛夹克XL补货", safe="")
        expected_body = quote("快去下单", safe="")
        assert url.startswith(
            f"https://api.day.app/MySecretKey/{expected_title}/{expected_body}?"
        )
        assert "level=timeSensitive" in url
        assert (
            "url=https%3A%2F%2Fcastore.com%2Fproducts%2F"
            "oracle-red-bull-racing-unisex-motor-jacket-egret-blue"
            in url
        )

    def test_main_out_of_stock_prints_and_exits_0_without_bark(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BARK_URL", raising=False)
        with (
            patch.object(
                monitor,
                "check_stock",
                return_value=monitor.StockCheckResult(
                    available=False, source="shopify_api", detail="XL available=False"
                ),
            ),
            patch.object(monitor, "send_bark_notification") as mock_bark,
        ):
            code = monitor.main([])
            assert code == 0
            mock_bark.assert_not_called()
            captured = capsys.readouterr()
            assert "XL 码当前无货" in captured.out

    def test_main_in_stock_sends_bark(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BARK_URL", "https://api.day.app/DummyKey")
        with (
            patch.object(
                monitor,
                "check_stock",
                return_value=monitor.StockCheckResult(
                    available=True, source="shopify_api", detail="XL available=True"
                ),
            ),
            patch.object(monitor, "send_bark_notification") as mock_bark,
        ):
            code = monitor.main([])
            assert code == 0
            mock_bark.assert_called_once_with(
                bark_url="https://api.day.app/DummyKey",
                title=monitor.BARK_TITLE,
                body=monitor.BARK_BODY,
            )

    def test_main_test_flag_ignores_stock_and_sends_test_push(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BARK_URL", "https://api.day.app/DummyKey")
        with (
            patch.object(monitor, "check_stock") as mock_check,
            patch.object(monitor, "send_bark_notification") as mock_bark,
        ):
            code = monitor.main(["--test"])
            assert code == 0
            mock_check.assert_not_called()
            mock_bark.assert_called_once_with(
                bark_url="https://api.day.app/DummyKey",
                title=monitor.BARK_TEST_TITLE,
                body=monitor.BARK_TEST_BODY,
            )
