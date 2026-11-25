#!/usr/bin/env python3
"""Castore Oracle Red Bull Racing Unisex Motor Jacket (XL) 库存监控脚本。

三级检测链路（任一成功即返回）：
  1. 直连 Shopify 商品 .js 数据接口（干净 IP / 本地 / 配代理时最快）
  2. r.jina.ai 转发通道（数据中心 IP 被 Cloudflare 拦截时使用，实测 ~2.5s）
  3. Playwright（无头 Chromium）打开商品页检测 XL 尺码按钮状态（最后兜底）

仅在检测到 XL 码有货（或传入 --test 参数）时通过 Bark GET 接口推送通知。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
import sys
import time
from typing import Any
from urllib.parse import quote, urlencode

import requests

PRODUCT_NAME = "Oracle Red Bull Racing Unisex Motor Jacket - Egret/Blue"
PRODUCT_URL = (
    "https://castore.com/products/"
    "oracle-red-bull-racing-unisex-motor-jacket-egret-blue"
)
SHOPIFY_API_URL = f"{PRODUCT_URL}.js"
TARGET_SIZE = "XL"

BARK_TITLE = "红牛夹克XL补货"
BARK_BODY = "快去下单"
BARK_TEST_TITLE = "红牛夹克XL补货（测试）"
BARK_TEST_BODY = "测试推送：Bark通知通道正常"
BARK_LEVEL = "timeSensitive"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT_SECONDS = 15
PLAYWRIGHT_TIMEOUT_MS = 30_000

# Playwright 兜底开关（默认启用，便于本地 / 住宅 IP 环境使用）。
# GitHub Actions 的机房 IP 访问 castore.com 必定触发 Cloudflare 托管挑战，
# 该通道在 CI 中从未成功过，只会额外烧掉约 90 秒安装浏览器的时间，
# 且在浏览器未安装时直接抛未捕获异常把整个 run 打红。CI 中请设为 false。
PLAYWRIGHT_FALLBACK_ENV = "PLAYWRIGHT_FALLBACK"
PLAYWRIGHT_FALLBACK_DISABLED_VALUES = ("0", "false", "off", "no", "disabled")

# r.jina.ai 转发通道：从服务端自家出口抓取，绕开本机 IP 信誉问题。
# castore.com 整站受 Cloudflare Managed Challenge 保护（连 robots.txt 都拦），
# 数据中心 IP（GitHub Actions / Azure）即使伪装完整 Chrome TLS 指纹仍返回 403，
# 因此该通道是 Actions 环境下唯一稳定可用的取数方式。
READER_ENDPOINT = "https://r.jina.ai/"
READER_TIMEOUT_SECONDS = 90
# 转发服务的出口 IP 偶尔也会被目标站点临时拦截（实测约 1/8 概率，
# 返回的是渲染后的验证页但 httpStatus 仍报 200），因此必须重试。
READER_MAX_ATTEMPTS = 3
READER_RETRY_BACKOFF_SECONDS = 2.0

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
}

CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "access denied",
    "verify you are human",
    "human verification",
)

CHALLENGE_HTML_MARKERS = (
    "cf-turnstile",
    "cdn-cgi/challenge-platform",
    "challenge-running",
    "_cf_chl_opt",
    "verify you are human",
)

# 转发层返回"人机验证页"时的特征词（含被 Cloudflare 渲染成 markdown 后的措辞）。
# 这类响应 body 不含 JSON，且 httpStatus 仍可能报 200，必须靠正文特征识别。
READER_CHALLENGE_MARKERS = CHALLENGE_TITLE_MARKERS + (
    "performing security verification",
    "verifies you are not a bot",
    "enable javascript and cookies to continue",
    "checking your browser",
    "security service to protect against malicious bots",
)

UNAVAILABLE_CLASS_TOKENS = (
    "disabled",
    "sold-out",
    "soldout",
    "sold_out",
    "out-of-stock",
    "outofstock",
    "out_of_stock",
    "unavailable",
    "is-disabled",
    "is-unavailable",
    "crossed-out",
    "line-through",
)

UNAVAILABLE_TEXT_PATTERNS = (
    re.compile(r"\bsold\s*out\b", re.IGNORECASE),
    re.compile(r"\bout\s*of\s*stock\b", re.IGNORECASE),
    re.compile(r"\bunavailable\b", re.IGNORECASE),
    re.compile(r"\bnotify\s*me\b", re.IGNORECASE),
)

EXACT_XL_DOM_PATTERN = re.compile(
    r"^\s*XL(?:\s*[-–—:(]?\s*(?:SOLD\s*OUT|OUT\s*OF\s*STOCK|UNAVAILABLE)\s*[)]?)?\s*$",
    re.IGNORECASE,
)


class StockCheckError(RuntimeError):
    """库存检测失败基类。"""


class ApiFallbackError(StockCheckError):
    """Shopify 接口失败或被拦截，需要降级到 Playwright 检测。"""


class CloudflareBlockedError(StockCheckError):
    """访问被 Cloudflare 反爬挑战或人机验证拦截。"""


@dataclass(frozen=True)
class StockCheckResult:
    """库存检测结果。"""

    available: bool
    source: str
    detail: str


def now_str() -> str:
    """返回当前本地时间戳字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    """打印带时间戳的日志。"""
    print(f"[{now_str()}] {message}", flush=True)


def proxy_config() -> dict[str, str] | None:
    """从环境变量 SHOPIFY_PROXY 读取可选代理（配了住宅代理时直连通道即可恢复使用）。"""
    proxy = os.environ.get("SHOPIFY_PROXY", "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def playwright_fallback_enabled() -> bool:
    """判断是否允许走 Playwright 兜底通道（默认启用，可用环境变量关闭）。"""
    flag = os.environ.get(PLAYWRIGHT_FALLBACK_ENV, "").strip().lower()
    return flag not in PLAYWRIGHT_FALLBACK_DISABLED_VALUES


def normalize_size_text(value: Any) -> str:
    """标准化尺码文本（去首尾空白、合并多余空格、转大写）。"""
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().upper().split())


def is_exact_target_size(value: Any, target_size: str = TARGET_SIZE) -> bool:
    """精确匹配目标尺码，严格排除 2XL/3XL/4XL/XXL 等尺码。"""
    return normalize_size_text(value) == normalize_size_text(target_size)


def is_target_variant(
    variant: dict[str, Any], target_size: str = TARGET_SIZE
) -> bool:
    """检查 Shopify variant 的 title、option1 或 option2 是否精确等于目标尺码。"""
    for field in ("title", "option1", "option2"):
        if is_exact_target_size(variant.get(field), target_size):
            return True
    return False


def parse_shopify_variants(
    payload: dict[str, Any], target_size: str = TARGET_SIZE
) -> StockCheckResult:
    """解析 Shopify .js 接口 JSON，精确匹配目标尺码并判断库存状态。"""
    variants = payload.get("variants")
    if not isinstance(variants, list):
        raise ApiFallbackError("Shopify 接口返回的 JSON 中缺少有效的 variants 列表")

    matched = [
        variant
        for variant in variants
        if isinstance(variant, dict) and is_target_variant(variant, target_size)
    ]

    if not matched:
        raise ApiFallbackError(
            f"Shopify 接口 variants 中未找到精确匹配的 {target_size} 尺码"
        )

    is_available = any(variant.get("available") is True for variant in matched)
    matched_titles = ", ".join(
        str(v.get("title") or v.get("option1") or v.get("option2")) for v in matched
    )
    return StockCheckResult(
        available=is_available,
        source="shopify_api",
        detail=f"匹配变体: {matched_titles} (available={is_available})",
    )


def _is_cloudflare_challenge_response(response: requests.Response) -> bool:
    """判断 requests 响应是否命中 Cloudflare 反爬拦截/挑战。"""
    if response.headers.get("cf-mitigated"):
        return True
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type:
        body_lower = response.text[:4096].lower()
        if any(marker in body_lower for marker in CHALLENGE_TITLE_MARKERS) or any(
            marker in body_lower for marker in CHALLENGE_HTML_MARKERS
        ):
            return True
    return False


def check_stock_via_shopify_api(
    session: requests.Session | None = None,
    api_url: str = SHOPIFY_API_URL,
    target_size: str = TARGET_SIZE,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> StockCheckResult:
    """优先请求 Shopify 商品 .js 接口检测目标尺码库存。"""
    client = session or requests
    try:
        response = client.get(
            api_url,
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            proxies=proxy_config(),
        )
    except requests.Timeout as exc:
        raise ApiFallbackError(f"Shopify 接口请求超时（>{timeout}s）") from exc
    except requests.RequestException as exc:
        raise ApiFallbackError(f"Shopify 接口网络请求异常: {exc}") from exc

    if _is_cloudflare_challenge_response(response):
        cf_mitigated = response.headers.get("cf-mitigated", "html-challenge")
        raise ApiFallbackError(
            f"Shopify 接口被 Cloudflare 拦截 (HTTP {response.status_code}, "
            f"cf-mitigated={cf_mitigated})"
        )

    if response.status_code in (403, 429, 503):
        raise ApiFallbackError(f"Shopify 接口返回 HTTP {response.status_code}")

    if response.status_code != 200:
        raise ApiFallbackError(f"Shopify 接口返回非 200 状态码: HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiFallbackError("Shopify 接口响应不是有效的 JSON 格式") from exc

    if not isinstance(payload, dict):
        raise ApiFallbackError("Shopify 接口 JSON 根节点不是对象")

    return parse_shopify_variants(payload, target_size=target_size)


class _ReaderTransientError(ApiFallbackError):
    """转发通道的瞬时故障（可重试），例如出口 IP 被临时拦截、限流、内容为空。"""


def _reader_attempt(
    api_url: str,
    target_size: str,
    timeout: int,
    session: requests.Session | None,
) -> StockCheckResult:
    """转发通道的单次尝试。瞬时故障抛 _ReaderTransientError，其余抛 ApiFallbackError。"""
    separator = "&" if "?" in api_url else "?"
    busted_url = f"{api_url}{separator}v={int(time.time() * 1000)}"

    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-Cache-Tolerance": "0",
    }
    if api_key := os.environ.get("JINA_API_KEY", "").strip():
        headers["Authorization"] = f"Bearer {api_key}"

    client = session or requests
    try:
        response = client.get(
            READER_ENDPOINT + busted_url, headers=headers, timeout=timeout
        )
    except requests.Timeout as exc:
        raise _ReaderTransientError(f"转发通道超时（>{timeout}s）") from exc
    except requests.RequestException as exc:
        raise _ReaderTransientError(f"转发通道网络异常: {exc}") from exc

    if response.status_code == 429:
        raise _ReaderTransientError(
            "转发通道被限流（HTTP 429），可配置 JINA_API_KEY 提高配额"
        )
    if response.status_code >= 500:
        raise _ReaderTransientError(f"转发通道服务端错误 HTTP {response.status_code}")
    if response.status_code != 200:
        raise ApiFallbackError(f"转发通道返回 HTTP {response.status_code}")

    try:
        envelope = response.json()
    except ValueError as exc:
        raise _ReaderTransientError("转发通道响应不是有效 JSON") from exc

    if not isinstance(envelope, dict):
        raise ApiFallbackError("转发通道响应根节点不是对象")
    if envelope.get("code") != 200:
        raise _ReaderTransientError(f"转发通道上游错误: {str(envelope)[:200]}")

    data = envelope.get("data")
    if not isinstance(data, dict):
        raise ApiFallbackError("转发通道响应缺少 data 字段")

    upstream_status = data.get("httpStatus")
    if upstream_status not in (None, 200):
        raise _ReaderTransientError(f"转发通道上游站点返回 HTTP {upstream_status}")

    content = str(data.get("content") or "").strip()
    if not content:
        raise _ReaderTransientError("转发通道返回空内容")

    # 转发层可能把验证页渲染成 markdown 却仍报 httpStatus=200，
    # 且正文不含 JSON —— 仅靠状态码无法识别，必须看正文特征。
    if not content.startswith("{"):
        head_lower = content[:2048].lower()
        if content.startswith("<") or any(
            marker in head_lower for marker in READER_CHALLENGE_MARKERS
        ):
            raise _ReaderTransientError("转发出口被 Cloudflare 临时拦截（拿到人机验证页）")
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end <= start:
            raise _ReaderTransientError(f"转发通道返回非商品数据: {content[:120]!r}")
        content = content[start : end + 1]

    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise _ReaderTransientError(f"转发通道商品 JSON 解析失败: {exc}") from exc

    if not isinstance(payload, dict):
        raise ApiFallbackError("转发通道商品 JSON 根节点不是对象")

    # 尺码缺失属于确定性结果，不重试，直接上抛
    parsed = parse_shopify_variants(payload, target_size=target_size)
    return StockCheckResult(
        available=parsed.available,
        source="reader",
        detail=f"经 r.jina.ai 转发取数；{parsed.detail}",
    )


def check_stock_via_reader(
    api_url: str = SHOPIFY_API_URL,
    target_size: str = TARGET_SIZE,
    timeout: int = READER_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
    max_attempts: int = READER_MAX_ATTEMPTS,
) -> StockCheckResult:
    """通过 r.jina.ai 转发通道抓取 Shopify 商品 .js 数据并判断库存。

    关键点：
      * 带毫秒时间戳 cache-buster，并置 X-Cache-Tolerance: 0 —— 库存监控绝不能读缓存，
        否则补货后可能拿到几分钟前的旧数据而漏报。
      * 转发层可能以 HTTP 200 返回"上游失败/人机验证页"的正文，必须逐层校验
        code / httpStatus / 正文是否为商品 JSON，不能只看状态码。
      * 该故障是瞬时的（实测约 1/8 概率），必须重试；单次失败就放弃会导致漏报。
    """
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return _reader_attempt(api_url, target_size, timeout, session)
        except _ReaderTransientError as exc:
            last_error = exc
            if attempt < max_attempts:
                log(f"转发通道第 {attempt}/{max_attempts} 次尝试失败（{exc}），重试中...")
                time.sleep(READER_RETRY_BACKOFF_SECONDS * attempt)
            else:
                log(f"转发通道连续 {max_attempts} 次失败（{exc}）")

    raise ApiFallbackError(f"转发通道重试 {max_attempts} 次仍失败: {last_error}")



def _is_candidate_unavailable(item: dict[str, Any]) -> bool:
    """判断单个尺码候选项（含关联 input/label 状态）是否处于不可点击或无货状态。"""
    if item.get("disabled") is True:
        return True
    if item.get("inputDisabled") is True:
        return True
    if str(item.get("ariaDisabled", "")).lower() == "true":
        return True
    if str(item.get("labelAriaDisabled", "")).lower() == "true":
        return True
    if str(item.get("dataAvailable", "")).lower() == "false":
        return True

    combined_classes = " ".join(
        str(item.get(k, "")) for k in ("className", "labelClassName")
    ).lower()
    class_tokens = set(re.split(r"[\s,]+", combined_classes))
    if any(
        token in class_tokens or token in combined_classes
        for token in UNAVAILABLE_CLASS_TOKENS
    ):
        return True

    combined_text = " ".join(
        str(item.get(k, "")) for k in ("text", "labelText", "ariaLabel", "title")
    )
    if any(pattern.search(combined_text) for pattern in UNAVAILABLE_TEXT_PATTERNS):
        return True

    for pe_key in ("pointerEvents", "labelPointerEvents"):
        if str(item.get(pe_key, "")).lower() == "none":
            return True

    return False


def evaluate_dom_size_candidates(
    candidates: list[dict[str, Any]], target_size: str = TARGET_SIZE
) -> StockCheckResult | None:
    """根据页面提取出的尺码按钮候选信息判断目标尺码是否有货。"""
    normalized_target = normalize_size_text(target_size)

    def _matches_dom_candidate(item: dict[str, Any]) -> bool:
        for attr_key in ("value", "dataValue", "ariaLabel"):
            if is_exact_target_size(item.get(attr_key), normalized_target):
                return True
        for text_key in ("text", "labelText"):
            raw_text = item.get(text_key)
            if isinstance(raw_text, str):
                collapsed = " ".join(raw_text.strip().split())
                if is_exact_target_size(collapsed, normalized_target):
                    return True
                if normalized_target == "XL" and EXACT_XL_DOM_PATTERN.match(collapsed):
                    return True
        return False

    matched = [item for item in candidates if _matches_dom_candidate(item)]
    if not matched:
        return None

    # 若页面存在可见尺码按钮/标签（如 label / button），优先采用这些交互元素；
    # 避免被无 disabled 属性的隐藏 screen-reader <input> 干扰。
    labeled_or_visible = [
        item
        for item in matched
        if item.get("tag") != "INPUT" or item.get("hasPairedLabel") is True
    ]
    effective = labeled_or_visible or matched

    # 若任意匹配项明确带有无货/禁用状态（例如 label 带 prd-Options_Button-disabled
    # 或 "- Out of stock"），说明目标尺码当前不可购买。
    unavailable_items = [item for item in effective if _is_candidate_unavailable(item)]
    if unavailable_items:
        sample = unavailable_items[0]
        label_display = " ".join(
            str(
                sample.get("labelText")
                or sample.get("text")
                or sample.get("value")
                or target_size
            ).split()
        )
        return StockCheckResult(
            available=False,
            source="playwright",
            detail=f"页面尺码按钮 '{label_display}' 处于不可选/无货状态",
        )

    sample = effective[0]
    label_display = " ".join(
        str(
            sample.get("labelText")
            or sample.get("text")
            or sample.get("value")
            or target_size
        ).split()
    )
    return StockCheckResult(
        available=True,
        source="playwright",
        detail=f"页面尺码按钮 '{label_display}' 可点击/有货",
    )


def _page_has_antibot_challenge(title: str, html_snippet: str) -> bool:
    """检测已加载页面是否为 Cloudflare/CAPTCHA 人机验证页。"""
    title_lower = title.lower()
    html_lower = html_snippet[:16384].lower()
    return any(m in title_lower for m in CHALLENGE_TITLE_MARKERS) or any(
        m in html_lower for m in CHALLENGE_HTML_MARKERS
    )


def check_stock_via_playwright(
    product_url: str = PRODUCT_URL,
    target_size: str = TARGET_SIZE,
    timeout_ms: int = PLAYWRIGHT_TIMEOUT_MS,
) -> StockCheckResult:
    """使用标准 Playwright（无头 Chromium）访问商品页检测 XL 尺码按钮状态。

    注意：若浏览器访问同样触发 Cloudflare 反爬挑战（HTTP 403 / cf-mitigated /
    人机验证页面），将立即停止并抛出 CloudflareBlockedError，不会尝试绕过或等待验证码。
    """
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise StockCheckError(
            "未安装 playwright，请先执行: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            # 浏览器未安装 / 版本不匹配（Executable doesn't exist）时，
            # playwright 抛的是内部错误类型而非 StockCheckError，
            # 若不转换会导致整个 run 直接崩栈，掩盖真正原因。
            raise StockCheckError(
                "Playwright 浏览器未安装或启动失败，请先执行 "
                "`python -m playwright install --with-deps chromium`；"
                f"原始错误: {exc}"
            ) from exc
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale="en-GB",
            )
            page = context.new_page()

            try:
                response = page.goto(
                    product_url, wait_until="commit", timeout=timeout_ms
                )
            except PlaywrightTimeoutError as exc:
                raise StockCheckError(
                    f"Playwright 访问商品页超时（>{timeout_ms // 1000}s）"
                ) from exc

            if response is None:
                raise StockCheckError("Playwright 未收到商品页 HTTP 响应")

            cf_mitigated = response.headers.get("cf-mitigated", "")
            if response.status in (403, 429, 503) or cf_mitigated:
                raise CloudflareBlockedError(
                    f"Playwright 访问商品页同样被 Cloudflare 反爬挑战拦截 "
                    f"(HTTP {response.status}, cf-mitigated={cf_mitigated or 'none'})"
                )

            if response.status >= 400:
                raise StockCheckError(
                    f"Playwright 访问商品页返回异常状态码 HTTP {response.status}"
                )

            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

            title = page.title()
            html_content = page.content()
            if _page_has_antibot_challenge(title, html_content):
                raise CloudflareBlockedError(
                    f"Playwright 页面检测到人机验证/反爬挑战（标题: {title!r}）"
                )

            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass

            # 1) 优先从页面 DOM 尺码选择区提取尺码按钮/标签状态（关联配对的 radio input 与 label）
            candidates: list[dict[str, Any]] = page.evaluate(
                """() => {
                    const selectors = [
                        '[data-product-options-el="option"] label',
                        '[data-product-options-el="option"] input[type="radio"]',
                        'button',
                        'label',
                        'input[type="radio"]',
                        'a',
                        '[role="radio"]',
                        '[role="button"]',
                        'option',
                        '[data-value]'
                    ];
                    const seen = new Set();
                    const nodes = [];
                    for (const el of document.querySelectorAll(selectors.join(','))) {
                        if (!seen.has(el)) {
                            seen.add(el);
                            nodes.push(el);
                        }
                    }
                    return nodes.map((el) => {
                        const style = window.getComputedStyle(el);
                        let inputDisabled = false;
                        let linkedValue = '';
                        let hasPairedLabel = false;
                        let labelText = '';
                        let labelClassName = '';
                        let labelAriaDisabled = '';
                        let labelPointerEvents = '';

                        if (el.tagName === 'LABEL') {
                            const forId = el.getAttribute('for');
                            const linked = forId
                                ? document.getElementById(forId)
                                : el.querySelector('input');
                            if (linked) {
                                inputDisabled = Boolean(linked.disabled || linked.hasAttribute('disabled'));
                                linkedValue = linked.getAttribute('value') || '';
                            }
                        } else if (el.tagName === 'INPUT') {
                            const id = el.getAttribute('id');
                            let pairedLabel = id
                                ? document.querySelector(`label[for="${CSS.escape(id)}"]`)
                                : null;
                            if (!pairedLabel && el.parentElement) {
                                pairedLabel = el.parentElement.querySelector('label');
                            }
                            if (pairedLabel) {
                                hasPairedLabel = true;
                                const lStyle = window.getComputedStyle(pairedLabel);
                                labelText = (pairedLabel.innerText || pairedLabel.textContent || '').trim();
                                labelClassName = typeof pairedLabel.className === 'string' ? pairedLabel.className : '';
                                labelAriaDisabled = pairedLabel.getAttribute('aria-disabled') || '';
                                labelPointerEvents = lStyle ? lStyle.pointerEvents : '';
                            }
                        }

                        return {
                            tag: el.tagName,
                            text: (el.innerText || el.textContent || '').trim(),
                            value: el.getAttribute('value') || linkedValue || '',
                            dataValue: el.getAttribute('data-value') || el.getAttribute('data-option-value') || '',
                            dataAvailable: el.getAttribute('data-available') || '',
                            ariaLabel: el.getAttribute('aria-label') || '',
                            ariaDisabled: el.getAttribute('aria-disabled') || '',
                            title: el.getAttribute('title') || '',
                            className: typeof el.className === 'string' ? el.className : '',
                            disabled: Boolean(el.disabled || el.hasAttribute('disabled')),
                            inputDisabled,
                            hasPairedLabel,
                            labelText,
                            labelClassName,
                            labelAriaDisabled,
                            labelPointerEvents,
                            pointerEvents: style ? style.pointerEvents : ''
                        };
                    });
                }"""
            )

            dom_result = evaluate_dom_size_candidates(
                candidates, target_size=target_size
            )
            if dom_result is not None:
                # 若 DOM 按钮显示可点击，进一步通过同源 .js 接口二次核验，避免“可选但仅支持缺货订阅”误报
                if dom_result.available:
                    in_page_payload = page.evaluate(
                        """async (apiPath) => {
                            try {
                                const res = await fetch(apiPath, { credentials: 'same-origin' });
                                if (res.ok) {
                                    return await res.json();
                                }
                            } catch (_) {}
                            return null;
                        }""",
                        f"{product_url}.js",
                    )
                    if isinstance(in_page_payload, dict):
                        try:
                            verified = parse_shopify_variants(
                                in_page_payload, target_size=target_size
                            )
                            return StockCheckResult(
                                available=verified.available,
                                source="playwright",
                                detail=f"{dom_result.detail}；同源接口核验: {verified.detail}",
                            )
                        except ApiFallbackError:
                            pass
                return dom_result

            # 2) 若 DOM 按钮未匹配，尝试通过同源 Shopify .js 接口或页面内嵌 JSON 解析
            in_page_payload = page.evaluate(
                """async (apiPath) => {
                    try {
                        const res = await fetch(apiPath, { credentials: 'same-origin' });
                        if (res.ok) {
                            return await res.json();
                        }
                    } catch (_) {}
                    const scripts = Array.from(
                        document.querySelectorAll('script[type="application/json"]')
                    );
                    for (const s of scripts) {
                        try {
                            const data = JSON.parse(s.textContent || '');
                            if (data && Array.isArray(data.variants)) {
                                return data;
                            }
                            if (Array.isArray(data) && data.length > 0 && data[0] && 'available' in data[0]) {
                                return { variants: data };
                            }
                        } catch (_) {}
                    }
                    return null;
                }""",
                f"{product_url}.js",
            )
            if isinstance(in_page_payload, dict):
                try:
                    parsed = parse_shopify_variants(
                        in_page_payload, target_size=target_size
                    )
                    return StockCheckResult(
                        available=parsed.available,
                        source="playwright",
                        detail=f"同源商品数据 {parsed.detail}",
                    )
                except ApiFallbackError:
                    pass

            raise StockCheckError(
                f"Playwright 成功加载商品页，但未在页面中找到尺码 '{target_size}' 对应的按钮或数据"
            )
        finally:
            browser.close()


def check_stock() -> StockCheckResult:
    """执行完整库存检测：直连 Shopify 接口 → r.jina.ai 转发 → Playwright 兜底。"""
    reasons: list[str] = []

    try:
        return check_stock_via_shopify_api()
    except ApiFallbackError as exc:
        reasons.append(f"直连: {exc}")
        log(f"直连 Shopify 接口受阻（{exc}），切换到 r.jina.ai 转发通道...")

    try:
        return check_stock_via_reader()
    except ApiFallbackError as exc:
        reasons.append(f"转发: {exc}")
        log(f"转发通道不可用（{exc}）")

    if not playwright_fallback_enabled():
        reasons.append(f"playwright: 已通过 {PLAYWRIGHT_FALLBACK_ENV} 关闭兜底")
        raise StockCheckError("全部通道均检测失败 → " + " | ".join(reasons))

    log("最后尝试 Playwright（无头 Chromium）...")
    try:
        return check_stock_via_playwright()
    except CloudflareBlockedError as exc:
        reasons.append(f"playwright: {exc}")
        raise CloudflareBlockedError(
            "全部通道均被 Cloudflare 拦截 → " + " | ".join(reasons)
        ) from exc
    except StockCheckError as exc:
        reasons.append(f"playwright: {exc}")
        raise StockCheckError("全部通道均检测失败 → " + " | ".join(reasons)) from exc


def build_bark_push_url(
    bark_url: str,
    title: str,
    body: str,
    product_url: str = PRODUCT_URL,
    level: str = BARK_LEVEL,
) -> str:
    """构造带 URL 编码的 Bark GET 推送链接。"""
    base = bark_url.strip().rstrip("/")
    if not base:
        raise ValueError("环境变量 BARK_URL 为空")
    encoded_title = quote(title, safe="")
    encoded_body = quote(body, safe="")
    query = urlencode({"url": product_url, "level": level})
    return f"{base}/{encoded_title}/{encoded_body}?{query}"


def send_bark_notification(
    bark_url: str,
    title: str = BARK_TITLE,
    body: str = BARK_BODY,
    product_url: str = PRODUCT_URL,
    level: str = BARK_LEVEL,
    session: requests.Session | None = None,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> None:
    """通过 Bark GET 请求发送 iPhone 推送通知（不在日志中泄露 Bark Key）。"""
    push_url = build_bark_push_url(
        bark_url=bark_url,
        title=title,
        body=body,
        product_url=product_url,
        level=level,
    )
    client = session or requests
    response = client.get(push_url, timeout=timeout)
    response.raise_for_status()

    try:
        data = response.json()
    except ValueError:
        return

    if isinstance(data, dict) and data.get("code") not in (None, 200):
        raise RuntimeError(f"Bark 服务返回异常: {data}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description=f"监控 Castore {PRODUCT_NAME} ({TARGET_SIZE}) 库存并通过 Bark 推送通知"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="忽略库存状态，强制发送一条 Bark 测试推送以验证通知通道",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """主入口函数。"""
    args = parse_args(argv)
    bark_url = os.environ.get("BARK_URL", "").strip()

    if args.test:
        if not bark_url:
            log("错误：已启用 --test 模式，但未配置环境变量 BARK_URL。")
            return 1
        try:
            send_bark_notification(
                bark_url=bark_url,
                title=BARK_TEST_TITLE,
                body=BARK_TEST_BODY,
            )
            log("Bark 测试推送已发送成功！")
            return 0
        except Exception as exc:
            log(f"Bark 测试推送失败: {exc}")
            return 1

    try:
        result = check_stock()
    except CloudflareBlockedError as exc:
        log(f"检测受阻：{exc}")
        log(
            "排查建议：① 检查 r.jina.ai 是否可达（免费额度约 20 次/分钟，超限会返回 429）；"
            "② 配置 JINA_API_KEY 提高配额；③ 配置 SHOPIFY_PROXY 住宅代理后可走直连通道。"
        )
        return 1
    except StockCheckError as exc:
        log(f"库存检测失败：{exc}")
        return 1

    if result.available:
        log(f"检测到 {TARGET_SIZE} 码有货（来源: {result.source}，{result.detail}），准备发送 Bark 推送...")
        if not bark_url:
            log("错误：检测到有货，但未配置环境变量 BARK_URL，无法发送 Bark 推送。")
            return 1
        try:
            send_bark_notification(
                bark_url=bark_url,
                title=BARK_TITLE,
                body=BARK_BODY,
            )
            log("Bark 补货推送已发送成功！")
            return 0
        except Exception as exc:
            log(f"Bark 推送发送失败: {exc}")
            return 1

    log(f"{TARGET_SIZE} 码当前无货（来源: {result.source}，{result.detail}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
