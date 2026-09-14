"""arXiv digest with durable recovery and global rate-limit handling.
Run normally to send the configured digest; --dry-run never sends mail.
"""
import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import feedparser

SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECEIVER_EMAILS = ["foggymature@gmail.com", "mobius3516@gmail.com"]
LOCAL_TIMEZONE = ZoneInfo("Asia/Singapore")
DAYS_BACK = 2
STRICT_CATEGORY_MODE = True
PAGE_SIZE = 100
KEYWORDS_PER_QUERY = 6
MAX_PAGES_PER_QUERY = 50
REQUEST_INTERVAL_SECONDS = 10
HTTP_TIMEOUT_SECONDS = 45
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RUN_BUDGET_SECONDS = 600
STATE_PATH = Path(os.getenv("DIGEST_STATE_PATH", "digest-state.json"))
OUTPUT_DIR = Path(os.getenv("DIGEST_OUTPUT_DIR", "run-output"))
BASE_URL = "https://export.arxiv.org/api/query?"
SOURCE = 'oai'
OAI_URL = 'https://oaipmh.arxiv.org/oai?'
OAI_NS = '{http://www.openarchives.org/OAI/2.0/}'
RAW_NS = '{http://arxiv.org/OAI/arXivRaw/}'
OAI_SETS = {
    'physics.optics': 'physics:physics:optics',
    'quant-ph': 'physics:quant-ph',
    'physics.app-ph': 'physics:physics:app-ph',
    'eess.SP': 'eess:eess:SP',
    'cs.CV': 'cs:cs:CV',
    'cs.LG': 'cs:cs:LG',
}
USER_AGENT = "PhotonicsArxivDigest/3.0 (mailto:foggymature@gmail.com)"
_CUTOFF = None
_CLIENT = None


def utc_now():
    return datetime.now(timezone.utc)


def local_now():
    return utc_now().astimezone(LOCAL_TIMEZONE)


def get_cutoff_datetime():
    return _CUTOFF if _CUTOFF is not None else utc_now() - timedelta(days=DAYS_BACK)


CATEGORIES = [
    "physics.optics",
    "quant-ph",
    "physics.app-ph",
    "eess.SP",
    "cs.CV",
    "cs.LG",
]


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
    text, keyword = normalize_text(text), normalize_text(keyword)
    # Chemical abbreviations must not match "using", "since", "signal", etc.
    # Preserve the existing phrase/plural matching for descriptive keywords.
    if keyword in {'sin', 'aln', 'tfln', 'lnoi', 'linbo3', 'ppln', 'bto', 'tflt', 'spdc'}:
        return re.search(r'(?<!\w)' + re.escape(keyword) + r'(?!\w)', text) is not None
    return keyword in text


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
    return entry_id.rstrip("/").split('/abs/')[-1]


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

# Durable recovery. A completed batch is saved before the next API request.
def log(message):
    print(f"{utc_now().isoformat(timespec='seconds')} {message}", flush=True)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def encode_papers(papers):
    return [{k: v.isoformat() if isinstance(v, datetime) else v
             for k, v in p.items()} for p in papers]


def decode_papers(papers):
    result = []
    for paper in papers:
        p = dict(paper)
        for key in ('published_datetime', 'updated_datetime'):
            p[key] = parse_arxiv_datetime(p[key])
        result.append(p)
    return result


def config_hash():
    value = [2, SOURCE, KEYWORD_GROUPS, CATEGORIES, STRICT_CATEGORY_MODE, KEYWORDS_PER_QUERY]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def load_state():
    if not STATE_PATH.exists():
        return {'schema': 1, 'pending': None, 'last_complete_at': None,
                'last_complete_day': None, 'not_before': None,
                'last_notice_day': None}
    state = json.loads(STATE_PATH.read_text(encoding='utf-8'))
    if state.get('schema') != 1:
        raise RuntimeError('Unsupported state schema; refusing to reset recovery history.')
    if not all(k in state for k in ('pending', 'last_complete_at', 'not_before')):
        raise RuntimeError('Invalid state; refusing to silently discard recovery history.')
    return state


class Deferred(RuntimeError):
    pass


class InvalidFeed(RuntimeError):
    pass


def validate_feed(payload):
    root = ET.fromstring(payload)
    if root.tag != '{http://www.w3.org/2005/Atom}feed':
        raise InvalidFeed('Response is not an Atom feed')
    feed = feedparser.parse(payload)
    if feed.bozo:
        raise InvalidFeed(f'Invalid Atom XML: {feed.bozo_exception}')
    if 'opensearch_totalresults' not in feed.feed:
        raise InvalidFeed('Missing totalResults; cannot treat response as no papers')
    total = int(feed.feed.opensearch_totalresults)
    for entry in feed.entries:
        if '/api/errors' in entry.get('id', '') or not entry.get('updated'):
            raise InvalidFeed('API error entry or missing updated timestamp')
    if total < 0 or (total == 0 and feed.entries):
        raise InvalidFeed('Inconsistent totalResults')
    return feed


class Client:
    def __init__(self, state, opener=urlopen, sleeper=time.sleep,
                 monotonic=time.monotonic, now=utc_now):
        self.state = state
        self.opener = opener
        self.sleep = sleeper
        self.clock = monotonic
        self.now = now
        self.deadline = self.clock() + RUN_BUDGET_SECONDS
        self.last_start = None
        self.requests = 0

    def defer(self, reason, seconds=3600):
        until = self.now() + timedelta(seconds=seconds)
        current = parse_arxiv_datetime(self.state.get('not_before'))
        if current and current > until:
            until = current
        self.state['not_before'] = until.isoformat()
        atomic_json(STATE_PATH, self.state)
        raise Deferred(f'{reason}; next API request no earlier than {until.isoformat()}')

    def slot(self):
        until = parse_arxiv_datetime(self.state.get('not_before'))
        if until and until > self.now():
            raise Deferred(f'Server cooldown remains active until {until.isoformat()}')
        pause = 0 if self.last_start is None else max(
            0, REQUEST_INTERVAL_SECONDS - (self.clock() - self.last_start))
        if self.clock() + pause + HTTP_TIMEOUT_SECONDS >= self.deadline:
            raise Deferred('Run time budget reached; saved batches will resume next run')
        if pause:
            self.sleep(pause)
        self.last_start = self.clock()
        self.requests += 1

    def diagnostic(self, url, status, headers, body, elapsed):
        # Do not record cookies, authorization headers, SMTP credentials or tokens.
        keep = {'date', 'server', 'content-type', 'retry-after', 'via',
                'x-cache', 'x-cache-hits', 'x-served-by', 'x-cloud-trace-context',
                'x-request-id', 'x-timer', 'age'}
        record = {'time_utc': self.now().isoformat(), 'url': url,
                  'status': status, 'elapsed_seconds': round(elapsed, 3),
                  'headers': {k.lower(): v for k, v in headers.items()
                              if k.lower() in keep},
                  'body_excerpt': body[:4000].decode('utf-8', 'replace'),
                  'github_run_id': os.getenv('GITHUB_RUN_ID'),
                  'github_sha': os.getenv('GITHUB_SHA'),
                  'runner_os': os.getenv('RUNNER_OS')}
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with (OUTPUT_DIR / 'http-diagnostics.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        log(f'HTTP {status}, {elapsed:.1f}s; diagnostic saved')
        log(f'Retry-After={record["headers"].get("retry-after", "absent")}; '
            f'X-Cache={record["headers"].get("x-cache", "absent")}')

    def fetch(self, url, validator=validate_feed):
        # Only 5xx / transport / invalid XML get one short retry.
        # A 429/403 opens one GLOBAL circuit immediately. No endpoint/IP switching.
        for attempt in (1, 2):
            self.slot()
            start = self.clock()
            log(f'API request {self.requests}, attempt {attempt}/2')
            try:
                request = Request(url, headers={'User-Agent': USER_AGENT,
                                               'Accept': 'application/atom+xml, application/xml'})
                with self.opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    payload = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise InvalidFeed('Response too large')
                    headers = dict(response.headers)
                feed = validator(payload)
                log(f'HTTP 200, entries={len(feed.entries)}, '
                    f'elapsed={self.clock() - start:.1f}s')
                self.state['not_before'] = None
                return feed
            except HTTPError as error:
                try:
                    body = error.read(4000)
                finally:
                    error.close()
                headers = dict(error.headers or {})
                self.diagnostic(url, error.code, headers, body, self.clock() - start)
                retry_after = parse_retry_after(error.headers.get('Retry-After')) if error.headers else None
                if error.code in (429, 403):
                    self.defer(f'HTTP {error.code}: globally stop further requests',
                               max(3600, retry_after or 0))
                if error.code not in (500, 502, 503, 504):
                    raise RuntimeError(f'HTTP {error.code}; requires query/access inspection') from error
                if retry_after is not None:
                    self.defer(f'HTTP {error.code}: honor Retry-After', max(60, retry_after))
                if attempt == 2:
                    self.defer(f'HTTP {error.code} persisted after one retry')
            except (URLError, TimeoutError, OSError, InvalidFeed, ET.ParseError,
                    ValueError) as error:
                log(f'{type(error).__name__}: {error}')
                if attempt == 2:
                    self.defer(f'Repeated network/response failure: {error}')
            if self.clock() + 30 + HTTP_TIMEOUT_SECONDS >= self.deadline:
                raise Deferred('Insufficient time for another request')
            log('Transient failure; wait 30 seconds for one retry')
            self.sleep(30)
        raise AssertionError('unreachable')


class BadResumptionToken(RuntimeError):
    pass


def build_oai_url(category, lower, upper, token=None):
    # A token replaces all selection arguments, as required by OAI-PMH.
    params = {'verb': 'ListRecords'}
    if token:
        params['resumptionToken'] = token
    else:
        params.update(metadataPrefix='arXivRaw', set=OAI_SETS[category],
                      **{'from': lower, 'until': upper})
    return OAI_URL + urlencode(params)


def validate_oai(payload):
    root = ET.fromstring(payload)
    if root.tag != OAI_NS + 'OAI-PMH':
        raise InvalidFeed('Response is not OAI-PMH XML')
    response_date = parse_arxiv_datetime(root.findtext(OAI_NS + 'responseDate'))
    if response_date is None:
        raise InvalidFeed('Missing OAI responseDate')
    errors = root.findall(OAI_NS + 'error')
    records = root.find(OAI_NS + 'ListRecords')
    if errors:
        if len(errors) == 1 and errors[0].get('code') == 'noRecordsMatch' and records is None:
            return SimpleNamespace(entries=[], token=None, expiration=None)
        if any(e.get('code') == 'badResumptionToken' for e in errors):
            raise BadResumptionToken('OAI continuation token expired')
        raise InvalidFeed('OAI error: ' + '; '.join(
            f'{e.get("code")}: {e.text}' for e in errors))
    if records is None:
        raise InvalidFeed('Missing OAI ListRecords; cannot certify coverage')
    rows = records.findall(OAI_NS + 'record')
    if not rows:
        raise InvalidFeed('Empty ListRecords without noRecordsMatch')
    entries = []
    for row in rows:
        header = row.find(OAI_NS + 'header')
        if header is None or not header.findtext(OAI_NS + 'identifier'):
            raise InvalidFeed('OAI record has no identifier')
        if header.get('status') == 'deleted':
            continue
        raw = row.find(OAI_NS + 'metadata/' + RAW_NS + 'arXivRaw')
        if raw is None:
            raise InvalidFeed('Missing arXivRaw metadata')
        values = {key: (raw.findtext(RAW_NS + key) or '').strip()
                  for key in ('id', 'title', 'abstract', 'authors', 'categories')}
        if not all(values.values()):
            raise InvalidFeed('Incomplete arXivRaw metadata')
        versions = {}
        for version in raw.findall(RAW_NS + 'version'):
            name = version.get('version', '')
            if not re.fullmatch(r'v[1-9]\d*', name):
                raise InvalidFeed('Invalid arXiv version number')
            number = int(name[1:])
            date = parsedate_to_datetime(version.findtext(RAW_NS + 'date') or '')
            if date.tzinfo is None or number in versions:
                raise InvalidFeed('Invalid or duplicate arXiv version timestamp')
            versions[number] = date.astimezone(timezone.utc)
        if 1 not in versions:
            raise InvalidFeed('Missing first submission date')
        latest = max(versions)
        # OAI header.datestamp tracks metadata edits, not paper versions.
        # Using it would incorrectly report bibliography edits as new papers.
        entries.append(SimpleNamespace(
            id=f'https://arxiv.org/abs/{values["id"]}v{latest}',
            title=values['title'], summary=values['abstract'],
            authors=[SimpleNamespace(name=values['authors'])],
            tags=[{'term': c} for c in values['categories'].split()],
            published=versions[1].isoformat(), updated=versions[latest].isoformat()))
    token = records.find(OAI_NS + 'resumptionToken')
    return SimpleNamespace(entries=entries,
                           token=(token.text or '').strip() or None if token is not None else None,
                           expiration=token.get('expirationDate') if token is not None else None)


def collect_oai(client, category, progress):
    cycle = client.state['pending']
    lower = parse_arxiv_datetime(cycle['cutoff']).date().isoformat()
    upper = cycle.setdefault('oai_until', client.now().date().isoformat())
    token = progress.get('token')
    expiration = parse_arxiv_datetime(progress.get('expiration'))
    if token and (progress.get('token_day') != client.now().date().isoformat()
                  or (expiration and expiration <= client.now())):
        log(f'OAI token expired for {category}; restart the same date range')
        progress.clear()
        progress.update(done=False, papers=[])
        token = None
    papers = {p['base_arxiv_id']: p for p in decode_papers(progress.get('papers', []))}
    seen_tokens = {token} if token else set()
    for page in range(MAX_PAGES_PER_QUERY):
        log(f'OAI {category}, {lower} through {upper}, page {page + 1}')
        try:
            feed = client.fetch(build_oai_url(category, lower, upper, token), validate_oai)
        except BadResumptionToken as error:
            progress.clear()
            progress.update(done=False, papers=[])
            atomic_json(STATE_PATH, client.state)
            raise Deferred('OAI token rejected; saved range will restart next run') from error
        if feed.token and feed.token in seen_tokens:
            raise InvalidFeed('Repeated OAI token; cannot certify coverage')
        for entry in feed.entries:
            for group, keywords in KEYWORD_GROUPS.items():
                paper = parse_entry(entry, group, keywords)
                if paper:
                    key = paper['base_arxiv_id']
                    papers[key] = merge_paper(papers[key], paper) if key in papers else paper
        token = feed.token
        if token:
            seen_tokens.add(token)
        progress.update(papers=encode_papers(list(papers.values())), done=not token,
                        token=token, expiration=feed.expiration,
                        token_day=client.now().date().isoformat())
        atomic_json(STATE_PATH, client.state)
        if not token:
            return
    raise Deferred('OAI page budget reached; saved continuation will resume next run')


def batch_specs():
    if SOURCE == 'oai':
        return [(category, category, i, [])
                for i, category in enumerate(CATEGORIES, 1)]
    return [(f'{gi}-{bi}', group, bi, list(batch))
            for gi, (group, keywords) in enumerate(KEYWORD_GROUPS.items(), 1)
            for bi, batch in enumerate(chunk_list(keywords, KEYWORDS_PER_QUERY), 1)]


def begin_cycle(state, now, initial_days):
    previous = parse_arxiv_datetime(state.get('last_complete_at'))
    lower = now - timedelta(days=DAYS_BACK if previous else initial_days)
    if previous:
        lower = min(lower, previous - timedelta(days=DAYS_BACK))
    state['pending'] = {'started_at': now.isoformat(), 'cutoff': lower.isoformat(),
                        'config_hash': config_hash(), 'batches': {}}
    atomic_json(STATE_PATH, state)


def collect_batch(client, group, keywords, progress):
    if SOURCE == 'oai':
        return collect_oai(client, group, progress)
    papers = {p['base_arxiv_id']: p for p in decode_papers(progress.get('papers', []))}
    # Restart only the incomplete batch at page 1. Offsets from an older live
    # lastUpdatedDate result set are not stable across runs.
    seen_pages = set()
    previous_oldest = None
    for page in range(MAX_PAGES_PER_QUERY):
        log(f'Fetching page {page + 1} for {group}')
        feed = client.fetch(build_arxiv_url(build_search_query(keywords), page * PAGE_SIZE))
        total = int(feed.feed.opensearch_totalresults)
        if not feed.entries:
            if page * PAGE_SIZE < total:
                raise InvalidFeed('Unexpected empty page before totalResults')
            progress.update(done=True, papers=encode_papers(list(papers.values())))
            atomic_json(STATE_PATH, client.state)
            return
        signature = tuple(e.get('id') for e in feed.entries)
        if signature in seen_pages:
            raise InvalidFeed('Repeated API page; cannot certify coverage')
        seen_pages.add(signature)
        times = [parse_arxiv_datetime(e.updated) for e in feed.entries]
        if any(t is None for t in times) or times != sorted(times, reverse=True):
            raise InvalidFeed('API results are not sorted by lastUpdatedDate')
        if previous_oldest and max(times) > previous_oldest:
            raise InvalidFeed('Live pagination moved; restart this batch next run')
        previous_oldest = min(times)
        for entry in feed.entries:
            paper = parse_entry(entry, group, keywords)
            if paper:
                key = paper['base_arxiv_id']
                papers[key] = merge_paper(papers[key], paper) if key in papers else paper
        progress['papers'] = encode_papers(list(papers.values()))
        progress['done'] = (min(times) < get_cutoff_datetime()
                            or page * PAGE_SIZE + len(feed.entries) >= total)
        atomic_json(STATE_PATH, client.state)
        if progress['done']:
            return
    raise Deferred('Page safety limit reached; incomplete, coverage checkpoint NOT advanced')


def grouped_from_cycle(cycle):
    grouped = {group: [] for group in KEYWORD_GROUPS}
    for key, group, _, _ in batch_specs():
        progress = cycle['batches'].get(key, {})
        for paper in decode_papers(progress.get('papers', [])):
            grouped[paper['groups'][0] if SOURCE == 'oai' else group].append(paper)
    return deduplicate_all_groups(grouped)


def write_summary(status, body):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / 'digest.txt').write_text(body, encoding='utf-8')
    (OUTPUT_DIR / 'status.txt').write_text(status + '\n', encoding='utf-8')
    summary = os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write(f'## arXiv digest — {status}\n\n')
            f.write('完整诊断和日报预览见本次运行的 Artifacts。\n\n')
            f.write(body[:1800] + '\n')


def run(args):
    global _CUTOFF, _CLIENT
    state = load_state()
    today = local_now().date().isoformat()
    if args.scheduled and not state['pending'] and state.get('last_complete_day') == today:
        log('Today already completed; no arXiv requests and no duplicate email')
        write_summary('ALREADY_COMPLETE', '今天已完成日报，本次恢复检查无需请求 arXiv。')
        return 0
    if not args.dry_run and not args.diagnose:
        validate_email_config()
    _CLIENT = Client(state)
    if args.diagnose:
        # One normal first-batch request; respects stored cooldown, never sends email.
        _, group, _, keywords = batch_specs()[0]
        try:
            if SOURCE == 'oai':
                now = utc_now()
                _CLIENT.fetch(build_oai_url(group, (now - timedelta(days=DAYS_BACK)).date().isoformat(),
                                            now.date().isoformat()), validate_oai)
            else:
                _CLIENT.fetch(build_arxiv_url(build_search_query(keywords), 0))
            write_summary('DIAGNOSTIC_OK', '诊断请求成功；此结果不代表完整检索已完成。')
            return 0
        except (Deferred, RuntimeError, InvalidFeed) as error:
            write_summary('DIAGNOSTIC_FAILED', str(error))
            return 2
    if not state['pending']:
        begin_cycle(state, utc_now(), args.initial_days)
    cycle = state['pending']
    if cycle['config_hash'] != config_hash():
        # Preserve the uncovered time range, but recompute results under new settings.
        cycle['batches'] = {}
        cycle['config_hash'] = config_hash()
        atomic_json(STATE_PATH, state)
    _CUTOFF = parse_arxiv_datetime(cycle['cutoff'])
    log(f'Fixed recovery cutoff {_CUTOFF.isoformat()}; '
        f'{len(cycle["batches"])} batch checkpoints available')
    reason = None
    for key, group, number, keywords in batch_specs():
        progress = cycle['batches'].setdefault(key, {'done': False, 'papers': []})
        if progress['done']:
            log(f'Reuse completed batch {key}; no HTTP request')
            continue
        try:
            collect_batch(_CLIENT, group, keywords, progress)
        except (Deferred, RuntimeError, InvalidFeed, ValueError, ET.ParseError) as error:
            reason = str(error)
            log(f'INCOMPLETE: {reason}')
            break
    incomplete = reason is not None
    search_state = {'aborted': incomplete, 'failed_batches': []}
    if incomplete:
        for key, group, number, keywords in batch_specs():
            if not cycle['batches'].get(key, {}).get('done'):
                search_state['failed_batches'].append({'group': group,
                    'batch_number': number, 'keywords': keywords, 'page_number': 1,
                    'error': reason})
    grouped = grouped_from_cycle(cycle)
    body = format_digest(grouped, search_state)
    if SOURCE == 'oai':
        body = body.replace('排序依据：arXiv lastUpdatedDate',
                            '数据来源：arXiv OAI-PMH；按论文版本提交时间排序，关键词在本地匹配')
        body = body.replace('个关键词批次', '个学科批次')
    body = body.replace(f'检索范围：最近 {DAYS_BACK} 天内首次发布或更新',
                        f'检索范围：自 {_CUTOFF.strftime("%Y-%m-%d %H:%M UTC")} 起发布或更新（含故障补查）')
    body = body.replace('在完整重试后仍因 API 限流或服务端故障失败。',
                        '尚未完成，包含失败批次和因此暂未请求的批次。')
    body = body.replace('为避免持续触发 429/503，程序已提前停止后续 API 请求。',
                        '程序已保存进度，暂停后续请求，等待后续运行恢复。')
    if incomplete:
        body = f'本次未完成检索。原因为 {reason}\n\n' + body
    total = sum(map(len, grouped.values()))
    hits = sum(bool(papers) for papers in grouped.values())
    status = 'INCOMPLETE' if incomplete else 'COMPLETE'
    subject = f'arXiv Digest – {today} | {status} | {total} Papers | {hits} Groups'
    if incomplete:
        atomic_json(STATE_PATH, state)
    write_summary(status, body)
    if args.dry_run:
        log('DRY RUN: no email, delivery checkpoint not advanced')
        return 2 if incomplete else 0
    if not incomplete or state.get('last_notice_day') != today:
        send_email(subject, body)
        if incomplete:
            state['last_notice_day'] = today
    if not incomplete:
        # Never advance coverage on partial retrieval or before SMTP success.
        state['last_complete_at'] = cycle['started_at']
        state['last_complete_day'] = parse_arxiv_datetime(cycle['started_at']).astimezone(
            LOCAL_TIMEZONE).date().isoformat()
        state['pending'] = None
        state['not_before'] = None
    atomic_json(STATE_PATH, state)
    log(f'{status}; {total} papers; { _CLIENT.requests } API requests')
    return 2 if incomplete else 0


def main():
    global SOURCE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Fetch/preview only; do not send email')
    parser.add_argument('--diagnose', action='store_true', help='One normal query; never sends email')
    parser.add_argument('--scheduled', action='store_true', help='Skip when today already completed')
    parser.add_argument('--initial-days', type=int, default=7,
                        help='First installation backfill; later runs use durable coverage')
    parser.add_argument('--source', choices=('oai', 'api'), default='oai',
                        help='Official OAI metadata by default; api is the legacy search service')
    args = parser.parse_args()
    SOURCE = args.source
    if args.initial_days < DAYS_BACK:
        parser.error('--initial-days must be at least 2')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        return run(args)
    except Exception as error:
        log(f'ERROR {type(error).__name__}: {error}')
        write_summary('ERROR', f'{type(error).__name__}: {error}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
