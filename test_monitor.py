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

    def test_check_stock_falls_back_to_playwright(self) -> None:
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
                monitor, "check_stock_via_playwright", return_value=expected
            ) as mock_pw,
        ):
            result = monitor.check_stock()
            assert result == expected
            mock_pw.assert_called_once()


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
