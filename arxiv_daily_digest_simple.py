import os
import re
import time
import smtplib
import feedparser
import unicodedata

from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


# ============================================================
# 配置
# ============================================================

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")

RECEIVER_EMAILS = [
    "foggymature@gmail.com",
    "mobius3516@gmail.com",
]

LOCAL_TIMEZONE = ZoneInfo("Asia/Singapore")

# 检索最近多少天内首次发布或更新的论文
DAYS_BACK = 2

# 是否严格限制 arXiv 分类
STRICT_CATEGORY_MODE = True

CATEGORIES = [
    "physics.optics",
    "quant-ph",
    "physics.app-ph",
    "eess.SP",
    "cs.CV",
    "cs.LG",
]

# 每页结果数
PAGE_SIZE = 100

# 每次查询包含的关键词数。
# 原来为 3，这里改为 6，可明显减少 API 请求次数，
# 同时仍保持 URL 长度在较安全的范围内。
KEYWORDS_PER_QUERY = 6

# 每批关键词最多读取多少页
MAX_PAGES_PER_QUERY = 5

# 单次 HTTP 请求最多尝试次数。
# 若连续多次失败，会把该关键词批次标记为失败，而不是立刻让整个脚本崩溃。
MAX_RETRIES = 4

# 任意两次 arXiv API 请求之间至少间隔 10 秒。
REQUEST_INTERVAL_SECONDS = 10

# 单次 HTTP 请求超时
HTTP_TIMEOUT_SECONDS = 45

# 如果整个运行中已有两个关键词批次在完整重试后仍失败，
# 说明 arXiv API 很可能处于持续故障/限流状态。
# 此时停止继续轰炸 API，并发送“不完整检索”邮件。
MAX_FAILED_BATCHES_BEFORE_ABORT = 2

BASE_URL = "https://export.arxiv.org/api/query?"

USER_AGENT = (
    "PhotonicsArxivDigest/2.0 "
    "contact:foggymature@gmail.com"
)


# ============================================================
# 关键词组
# ============================================================

KEYWORD_GROUPS = {
    "Integrated Photonic Materials": [
        "SiN",
        "silicon nitride",
        "AlN",
        "aluminum nitride",
        "TFLN",
        "thin-film lithium niobate",
        "thin film lithium niobate",
        "LNOI",
        "lithium niobate",
        "LiNbO3",
        "PPLN",
        "MgO:PPLN",
        "BTO",
        "barium titanate",
        "TFLT",
        "thin-film lithium tantalate",
        "thin film lithium tantalate",
        "lithium tantalate",
        "heterogeneous integration",
    ],

    "Nonlinear & Electro-Optic Devices": [
        "nonlinear frequency conversion",
        "frequency conversion",
        "quantum frequency conversion",
        "electro-optic",
        "electrooptic",
        "electro-optic modulator",
        "electrooptic modulator",
        "Pockels effect",
        "on-chip comb",
        "microcomb",
        "Kerr comb",
        "acousto-optic",
        "acousto-optic modulator",
        "optical nonreciprocity",
        "nonreciprocal optics",
        "Bragg reflection",
        "Bragg reflector",
    ],

    "Quantum Sources & SPDC": [
        "SPDC",
        "spontaneous parametric down-conversion",
        "parametric down-conversion",
        "photon pair",
        "photon-pair",
        "photon pair source",
        "photon-pair source",
        "single photon",
        "single-photon",
        "heralded photon",
        "heralded single photon",
        "heralded single-photon",
        "quantum light source",
        "quantum source",
        "entangled photon",
        "entangled photons",
        "entangled-photon",
    ],

    "Frequency-Domain Quantum Information": [
        "frequency bin",
        "frequency-bin",
        "frequency bins",
        "frequency-bin qubit",
        "frequency-bin qudit",
        "frequency domain quantum",
        "frequency-domain quantum",
        "frequency beam splitter",
        "frequency beamsplitter",
        "frequency-bin beamsplitter",
        "Hong-Ou-Mandel",
        "Hong Ou Mandel",
        "HOM interference",
        "frequency-domain interference",
        "spectral quantum interference",
        "electro-optic frequency conversion",
        "synthetic frequency dimension",
        "synthetic dimension",
        "synthetic photonics",
    ],

    "Modes, Couplers & Interfaces": [
        "TE-TM",
        "TE TM",
        "mode coupling",
        "mode converter",
        "mode conversion",
        "polarization conversion",
        "intermodal coupling",
        "multimode waveguide",
        "adiabatic coupler",
        "directional coupler",
        "tapered waveguide",
        "waveguide taper",
        "edge coupler",
        "grating coupler",
        "fiber-chip interface",
        "fibre-chip interface",
        "low-loss interface",
        "photonic interface",
    ],

    "Microresonators & Integrated Quantum Systems": [
        "microresonator",
        "microring",
        "micro-ring",
        "ring resonator",
        "high-Q resonator",
        "high Q resonator",
        "cavity-enhanced",
        "cavity enhanced",
        "quantum memory",
        "integrated quantum photonics",
        "quantum photonic circuit",
        "source manipulation integration",
        "source-manipulation integration",
        "programmable quantum photonics",
        "spectral multiplexing",
        "frequency multiplexing",
    ],

    "Multimodal Imaging & Photonic Computing": [
        "microsphere imaging",
        "endomicroscopy",
        "super-resolution imaging",
        "optical fiber endoscope",
        "optical fibre endoscope",
        "photonic neural network",
        "on-chip machine learning",
        "optical computing",
        "photonic computing",
    ],
}


# ============================================================
# 基础工具
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def local_now():
    return datetime.now(LOCAL_TIMEZONE)


def get_cutoff_datetime():
    return utc_now() - timedelta(days=DAYS_BACK)


def parse_arxiv_datetime(value):
    """
    将 arXiv 时间转换为带 UTC 时区的 datetime。
    示例：2026-07-15T12:34:56Z
    """
    if not value:
        return None

    value = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def normalize_text(text):
    """
    统一大小写、Unicode 字符、连字符和空格。

    single-photon
    single photon
    single–photon

    会被统一为相近形式。
    """
    if not text:
        return ""

    text = unicodedata.normalize("NFKC", text)
    text = text.lower()

    text = re.sub(r"[-‐‑‒–—_/]+", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def keyword_matched(text, keyword):
    return normalize_text(keyword) in normalize_text(text)


def chunk_list(items, chunk_size):
    for index in range(0, len(items), chunk_size):
        yield items[index:index + chunk_size]


def get_entry_categories(entry):
    categories = []

    for tag in getattr(entry, "tags", []):
        if isinstance(tag, dict):
            term = tag.get("term")
        else:
            term = getattr(tag, "term", None)

        if term:
            categories.append(term)

    return categories


def is_category_allowed(entry):
    if not STRICT_CATEGORY_MODE:
        return True

    entry_categories = get_entry_categories(entry)

    return any(
        category in CATEGORIES
        for category in entry_categories
    )


def get_arxiv_id(entry):
    """
    返回带版本号的 arXiv ID，例如 2605.14777v2。
    """
    entry_id = getattr(entry, "id", "")
    return entry_id.rstrip("/").rsplit("/", 1)[-1]


def get_base_arxiv_id(entry):
    """
    返回不带版本号的 ID，用于去重，例如 2605.14777。
    """
    arxiv_id = get_arxiv_id(entry)
    return re.sub(r"v\d+$", "", arxiv_id)


def get_version_number(entry):
    arxiv_id = get_arxiv_id(entry)
    match = re.search(r"v(\d+)$", arxiv_id)

    if match:
        return int(match.group(1))

    return 1


def normalize_title_for_deduplication(title):
    title = normalize_text(title)
    return re.sub(r"[^a-z0-9]+", "", title)


# ============================================================
# arXiv API
# ============================================================

class ArxivAPIError(RuntimeError):
    """arXiv API 在完整重试后仍无法正常返回。"""


_LAST_REQUEST_MONOTONIC = None


def wait_for_request_slot():
    """
    保证任意两次 HTTP 请求的起始时间至少相隔
    REQUEST_INTERVAL_SECONDS 秒。
    """
    global _LAST_REQUEST_MONOTONIC

    now = time.monotonic()

    if _LAST_REQUEST_MONOTONIC is not None:
        elapsed = now - _LAST_REQUEST_MONOTONIC
        remaining = REQUEST_INTERVAL_SECONDS - elapsed

        if remaining > 0:
            print(
                f"   Rate-limit pause: "
                f"{remaining:.1f} seconds"
            )
            time.sleep(remaining)

    _LAST_REQUEST_MONOTONIC = time.monotonic()


def parse_retry_after(value):
    """
    解析 HTTP Retry-After。
    既支持秒数，也支持 HTTP-date。
    """
    if not value:
        return None

    value = value.strip()

    if value.isdigit():
        return max(0, int(value))

    try:
        retry_datetime = parsedate_to_datetime(value)

        if retry_datetime.tzinfo is None:
            retry_datetime = retry_datetime.replace(
                tzinfo=timezone.utc
            )

        seconds = int(
            (
                retry_datetime.astimezone(timezone.utc)
                - utc_now()
            ).total_seconds()
        )

        return max(0, seconds)

    except Exception:
        return None


def get_retry_wait_seconds(status, attempt, retry_after=None):
    """
    根据 HTTP 状态码给出退避时间。

    429 使用更长的等待时间。
    5xx 使用逐步增加的服务端故障退避。
    Retry-After 若存在则优先尊重。
    """
    if status == 429:
        calculated = min(180 * attempt, 600)

    elif status in (500, 502, 503, 504):
        calculated = min(60 * attempt, 300)

    else:
        calculated = min(30 * attempt, 180)

    if retry_after is not None:
        calculated = max(calculated, retry_after)

    return calculated


def fetch_arxiv_feed(url):
    """
    可靠地请求 arXiv API。

    主要改动：
    1. 显式设置 HTTP timeout。
    2. 显式处理 429/5xx。
    3. 尊重 Retry-After。
    4. 保证请求之间至少间隔 10 秒。
    5. 完整重试后抛出 ArxivAPIError，
       由上层决定是否继续，而不是直接让整个日报崩溃。
    """
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        print(
            f"   API request attempt "
            f"{attempt}/{MAX_RETRIES}"
        )

        wait_for_request_slot()

        status = None
        retry_after = None

        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/atom+xml",
                },
            )

            with urlopen(
                request,
                timeout=HTTP_TIMEOUT_SECONDS,
            ) as response:
                status = response.getcode()
                retry_after = parse_retry_after(
                    response.headers.get("Retry-After")
                )
                payload = response.read()

            feed = feedparser.parse(payload)
            bozo = bool(getattr(feed, "bozo", False))

            if status == 200 and not bozo:
                print(
                    f"   HTTP status: {status}, "
                    f"entries: {len(feed.entries)}"
                )
                return feed

            bozo_exception = getattr(
                feed,
                "bozo_exception",
                None,
            )

            last_error = (
                f"HTTP status={status}, "
                f"bozo={bozo}, "
                f"error={bozo_exception}"
            )

        except HTTPError as error:
            status = error.code
            retry_after = parse_retry_after(
                error.headers.get("Retry-After")
                if error.headers
                else None
            )

            last_error = (
                f"HTTP status={status}, "
                f"reason={error.reason}"
            )

        except URLError as error:
            last_error = (
                "network error="
                f"{getattr(error, 'reason', error)}"
            )

        except TimeoutError:
            last_error = (
                f"timeout after "
                f"{HTTP_TIMEOUT_SECONDS} seconds"
            )

        except Exception as error:
            last_error = (
                f"{type(error).__name__}: {error}"
            )

        print(
            f"   ⚠️ arXiv API error: "
            f"{last_error}"
        )

        if attempt >= MAX_RETRIES:
            break

        wait_seconds = get_retry_wait_seconds(
            status,
            attempt,
            retry_after=retry_after,
        )

        print(
            f"   Waiting {wait_seconds} seconds "
            "before retry..."
        )
        time.sleep(wait_seconds)

    raise ArxivAPIError(
        "arXiv API 请求连续失败。"
        "这通常是临时限流或服务端故障。\n"
        f"Last error: {last_error}"
    )


def build_search_query(keywords):
    """
    构造一批关键词的 arXiv 查询。

    多词关键词会加双引号，例如：
    ti:"thin-film lithium niobate"
    """
    keyword_expressions = []

    for keyword in keywords:
        escaped_keyword = keyword.replace('"', '\\"')

        keyword_expressions.append(
            f'ti:"{escaped_keyword}"'
        )
        keyword_expressions.append(
            f'abs:"{escaped_keyword}"'
        )

    keyword_query = " OR ".join(keyword_expressions)

    category_query = " OR ".join(
        f"cat:{category}"
        for category in CATEGORIES
    )

    return (
        f"({keyword_query}) "
        f"AND ({category_query})"
    )


def build_arxiv_url(search_query, start):
    parameters = {
        "search_query": search_query,
        "sortBy": "lastUpdatedDate",
        "sortOrder": "descending",
        "start": start,
        "max_results": PAGE_SIZE,
    }

    return BASE_URL + urlencode(parameters)


# ============================================================
# 搜索与论文解析
# ============================================================

def parse_entry(entry, group_name, keywords):
    if not is_category_allowed(entry):
        return None

    title = re.sub(
        r"\s+",
        " ",
        getattr(entry, "title", ""),
    ).strip()

    abstract = re.sub(
        r"\s+",
        " ",
        getattr(entry, "summary", ""),
    ).strip()

    searchable_text = f"{title} {abstract}"

    matched_keywords = [
        keyword
        for keyword in keywords
        if keyword_matched(searchable_text, keyword)
    ]

    if not matched_keywords:
        return None

    published_datetime = parse_arxiv_datetime(
        getattr(entry, "published", None)
    )

    updated_datetime = parse_arxiv_datetime(
        getattr(entry, "updated", None)
    )

    if published_datetime is None:
        return None

    if updated_datetime is None:
        updated_datetime = published_datetime

    cutoff = get_cutoff_datetime()

    # 关键：依据 updated 判断最近是否有活动
    if updated_datetime < cutoff:
        return None

    version = get_version_number(entry)

    # 如果是 v2/v3 且首次发布时间早于检索窗口，
    # 明确标记为重要版本更新
    if version > 1 and published_datetime < cutoff:
        status = "重要版本更新"
    elif version > 1:
        status = f"新预印本（当前 v{version}）"
    else:
        status = "新 arXiv 预印本"

    authors = ", ".join(
        author.name
        for author in getattr(entry, "authors", [])
    )

    arxiv_id = get_arxiv_id(entry)
    base_arxiv_id = get_base_arxiv_id(entry)

    return {
        "title": title,
        "authors": authors,
        "summary": abstract,
        "link": f"https://arxiv.org/abs/{base_arxiv_id}",
        "arxiv_id": arxiv_id,
        "base_arxiv_id": base_arxiv_id,
        "version": version,
        "published_datetime": published_datetime,
        "updated_datetime": updated_datetime,
        "published": published_datetime.strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        "updated": updated_datetime.strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        "status": status,
        "keywords": matched_keywords,
        "groups": [group_name],
        "categories": get_entry_categories(entry),
    }


def merge_paper(existing, incoming):
    """
    合并同一篇论文在不同关键词组中的匹配信息。
    """
    existing["groups"] = sorted(
        set(existing["groups"] + incoming["groups"])
    )

    existing["keywords"] = sorted(
        set(existing["keywords"] + incoming["keywords"]),
        key=str.lower,
    )

    existing["categories"] = sorted(
        set(existing["categories"] + incoming["categories"])
    )

    # 保留版本号更高或更新时间更新的记录
    if (
        incoming["updated_datetime"]
        > existing["updated_datetime"]
    ):
        preserved_groups = existing["groups"]
        preserved_keywords = existing["keywords"]
        preserved_categories = existing["categories"]

        existing.update(incoming)

        existing["groups"] = preserved_groups
        existing["keywords"] = preserved_keywords
        existing["categories"] = preserved_categories

    return existing


def search_group(group_name, keywords, search_state):
    print(f"\n========== {group_name} ==========")

    group_results = {}
    cutoff = get_cutoff_datetime()

    keyword_batches = list(
        chunk_list(keywords, KEYWORDS_PER_QUERY)
    )

    for batch_number, keyword_batch in enumerate(
        keyword_batches,
        start=1,
    ):
        if search_state["aborted"]:
            break

        print(
            f"\n🔍 Batch {batch_number}/"
            f"{len(keyword_batches)}"
        )
        print("   Keywords:", ", ".join(keyword_batch))

        search_query = build_search_query(keyword_batch)
        batch_failed = False

        for page_number in range(MAX_PAGES_PER_QUERY):
            start = page_number * PAGE_SIZE
            url = build_arxiv_url(search_query, start)

            print(
                f"   Fetching page {page_number + 1}, "
                f"start={start}"
            )

            try:
                feed = fetch_arxiv_feed(url)

            except ArxivAPIError as error:
                batch_failed = True

                failure = {
                    "group": group_name,
                    "batch_number": batch_number,
                    "keywords": list(keyword_batch),
                    "page_number": page_number + 1,
                    "error": str(error),
                }

                search_state["failed_batches"].append(
                    failure
                )

                print(
                    "   ❌ This keyword batch failed "
                    "after all retries."
                )

                print(
                    "   The script will keep any results "
                    "already collected and continue when safe."
                )

                if (
                    len(search_state["failed_batches"])
                    >= MAX_FAILED_BATCHES_BEFORE_ABORT
                ):
                    search_state["aborted"] = True

                    print(
                        "   🛑 Too many failed batches. "
                        "Stopping further arXiv requests "
                        "to avoid repeatedly hitting an "
                        "unhealthy or rate-limited API."
                    )

                break

            if not feed.entries:
                print("   No more results.")
                break

            page_updated_times = []

            for entry in feed.entries:
                entry_updated = parse_arxiv_datetime(
                    getattr(entry, "updated", None)
                )

                if entry_updated:
                    page_updated_times.append(entry_updated)

                paper = parse_entry(
                    entry,
                    group_name,
                    keyword_batch,
                )

                if paper is None:
                    continue

                key = paper["base_arxiv_id"]

                if not key:
                    key = normalize_title_for_deduplication(
                        paper["title"]
                    )

                if key in group_results:
                    group_results[key] = merge_paper(
                        group_results[key],
                        paper,
                    )
                else:
                    group_results[key] = paper

            # API 已按 lastUpdatedDate 降序排列。
            # 若本页最旧结果已早于 cutoff，
            # 后面的页面无需继续。
            if page_updated_times:
                oldest_on_page = min(page_updated_times)

                if oldest_on_page < cutoff:
                    print(
                        "   Reached results older than cutoff."
                    )
                    break

            if len(feed.entries) < PAGE_SIZE:
                print("   Last page reached.")
                break

        if batch_failed and search_state["aborted"]:
            break

    results = list(group_results.values())

    results.sort(
        key=lambda paper: paper["updated_datetime"],
        reverse=True,
    )

    print(
        f"✅ {group_name}: "
        f"{len(results)} unique papers"
    )

    return results


# ============================================================
# 跨主题统一去重
# ============================================================

def deduplicate_all_groups(grouped_entries):
    """
    按 arXiv ID 跨所有关键词组去重。

    一篇论文如果匹配多个主题，只在最先匹配的主题下显示，
    但保留所有匹配主题和关键词信息。
    """
    global_papers = {}
    group_order = list(grouped_entries.keys())

    for group_name in group_order:
        for paper in grouped_entries[group_name]:
            key = paper["base_arxiv_id"]

            if not key:
                key = normalize_title_for_deduplication(
                    paper["title"]
                )

            if key in global_papers:
                global_papers[key] = merge_paper(
                    global_papers[key],
                    paper,
                )
            else:
                global_papers[key] = paper

    deduplicated = {
        group_name: []
        for group_name in group_order
    }

    for paper in global_papers.values():
        # 放到它匹配到的第一个主题下
        primary_group = next(
            (
                group_name
                for group_name in group_order
                if group_name in paper["groups"]
            ),
            paper["groups"][0],
        )

        deduplicated[primary_group].append(paper)

    for group_name in deduplicated:
        deduplicated[group_name].sort(
            key=lambda paper: paper["updated_datetime"],
            reverse=True,
        )

    return deduplicated


# ============================================================
# 邮件正文
# ============================================================

def format_digest(grouped_entries, search_state):
    total_papers = sum(
        len(papers)
        for papers in grouped_entries.values()
    )

    generated_time = local_now().strftime(
        "%Y-%m-%d %H:%M %Z"
    )

    failed_batches = search_state["failed_batches"]
    incomplete = bool(failed_batches)

    lines = [
        "📚 arXiv 光子学与量子信息论文更新",
        "",
        f"生成时间：{generated_time}",
        f"检索范围：最近 {DAYS_BACK} 天内首次发布或更新",
        "排序依据：arXiv lastUpdatedDate",
    ]

    if incomplete:
        lines.extend(
            [
                "",
                "⚠️ 注意：本次 arXiv 检索未完全完成。",
                (
                    f"共有 {len(failed_batches)} 个关键词批次 "
                    "在完整重试后仍因 API 限流或服务端故障失败。"
                ),
                (
                    "以下论文列表保留了成功检索到的结果，"
                    "因此本次数量可能低于实际数量。"
                ),
            ]
        )

        if search_state["aborted"]:
            lines.append(
                "为避免持续触发 429/503，程序已提前停止后续 API 请求。"
            )

        lines.append("")

        for failure in failed_batches:
            lines.append(
                "   - "
                f"{failure['group']} | "
                f"Batch {failure['batch_number']} | "
                f"Page {failure['page_number']} | "
                "Keywords: "
                + ", ".join(failure["keywords"])
            )

    lines.extend(
        [
            "",
            f"统一去重后共 {total_papers} 篇",
            "",
        ]
    )

    if total_papers == 0:
        if incomplete:
            lines.append(
                "🛑 本次没有得到可用论文结果，"
                "但由于 arXiv API 检索不完整，"
                "不能据此判断最近两天确实没有匹配论文。"
            )
        else:
            lines.append(
                f"🛑 最近 {DAYS_BACK} 天内，"
                "arXiv 上没有找到首次发布或更新且匹配关键词的论文。"
            )

        return "\n".join(lines)

    for group_name, papers in grouped_entries.items():
        if not papers:
            continue

        lines.append(
            f"===== 【{group_name}】"
            f"（{len(papers)} 篇）====="
        )
        lines.append("")

        for index, paper in enumerate(papers, start=1):
            lines.append(f"{index}. {paper['title']}")
            lines.append(f"   作者：{paper['authors']}")
            lines.append(f"   状态：{paper['status']}")
            lines.append(
                f"   arXiv：{paper['base_arxiv_id']} "
                f"（当前 v{paper['version']}）"
            )
            lines.append(
                f"   首次提交：{paper['published']}"
            )
            lines.append(
                f"   最近更新：{paper['updated']}"
            )
            lines.append(
                "   匹配主题："
                + "；".join(paper["groups"])
            )
            lines.append(
                "   匹配关键词："
                + "；".join(paper["keywords"])
            )
            lines.append(
                "   arXiv 分类："
                + ", ".join(paper["categories"])
            )
            lines.append(f"   链接：{paper['link']}")
            lines.append(f"   摘要：{paper['summary']}")
            lines.append("")

    lines.append(
        f"📊 共找到 {total_papers} 篇去重后的论文。"
    )

    if incomplete:
        lines.append(
            "⚠️ 本次为不完整检索，请以明日自动运行或手动重跑结果为准。"
        )

    return "\n".join(lines)


# ============================================================
# 邮件发送
# ============================================================

def validate_email_config():
    if not SENDER_EMAIL:
        raise RuntimeError(
            "缺少环境变量 SENDER_EMAIL"
        )

    if not SENDER_PASSWORD:
        raise RuntimeError(
            "缺少环境变量 SENDER_PASSWORD"
        )

    if not RECEIVER_EMAILS:
        raise RuntimeError(
            "RECEIVER_EMAILS 不能为空"
        )


def send_email(subject, body):
    validate_email_config()

    message = MIMEText(
        body,
        "plain",
        "utf-8",
    )

    message["Subject"] = subject
    message["From"] = SENDER_EMAIL
    message["To"] = ", ".join(RECEIVER_EMAILS)

    server = None

    try:
        server = smtplib.SMTP(
            "smtp.gmail.com",
            587,
            timeout=30,
        )

        server.ehlo()
        server.starttls()
        server.ehlo()

        server.login(
            SENDER_EMAIL,
            SENDER_PASSWORD,
        )

        server.sendmail(
            SENDER_EMAIL,
            RECEIVER_EMAILS,
            message.as_string(),
        )

        print(
            "✅ Email sent to:",
            ", ".join(RECEIVER_EMAILS),
        )

    except Exception as error:
        print("❌ Email send failed:", str(error))
        raise

    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


# ============================================================
# 主流程
# ============================================================

def main():
    print("🔍 正在抓取 arXiv 论文...")
    print(
        "UTC cutoff:",
        get_cutoff_datetime().strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
    )

    search_state = {
        "failed_batches": [],
        "aborted": False,
    }

    all_grouped = {}

    for group_name, keywords in KEYWORD_GROUPS.items():
        if search_state["aborted"]:
            all_grouped[group_name] = []
            continue

        results = search_group(
            group_name,
            keywords,
            search_state,
        )

        all_grouped[group_name] = results

    deduplicated_groups = deduplicate_all_groups(
        all_grouped
    )

    total_papers = sum(
        len(papers)
        for papers in deduplicated_groups.values()
    )

    groups_with_hits = sum(
        1
        for papers in deduplicated_groups.values()
        if papers
    )

    email_body = format_digest(
        deduplicated_groups,
        search_state,
    )

    today_string = local_now().strftime("%Y-%m-%d")

    if search_state["failed_batches"]:
        subject_prefix = "⚠️ arXiv Digest"
        completeness_text = "INCOMPLETE | "
    else:
        subject_prefix = "📬 arXiv Digest"
        completeness_text = ""

    subject = (
        f"{subject_prefix} – {today_string} | "
        f"{completeness_text}"
        f"{total_papers} Papers | "
        f"{groups_with_hits} Groups"
    )

    print("\n========== DIGEST PREVIEW ==========")
    print(email_body)
    print("====================================\n")

    send_email(
        subject,
        email_body,
    )

    # 只要邮件成功发出，就让 GitHub Action 正常结束。
    # 如果 arXiv API 不完整，邮件主题和正文都会明确标记 INCOMPLETE，
    # 避免出现“红色失败但你不知道发生了什么”的情况。
    if search_state["failed_batches"]:
        print(
            "⚠️ Digest sent with incomplete-search warning."
        )
    else:
        print(
            "✅ Digest completed successfully."
        )


if __name__ == "__main__":
    main()
