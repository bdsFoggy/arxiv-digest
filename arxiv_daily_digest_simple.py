
import smtplib
import feedparser
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import re
import os


# ---------- 配置 ----------
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECEIVER_EMAILS = ["foggymature@gmail.com", "mobius3516@gmail.com"]
STRICT_CATEGORY_MODE = True  # 只处理光学/量子相关类别的论文
DAYS_BACK = 5

# ---------- 关键词组定义 ----------
KEYWORD_GROUPS = {
    "Integrated Photonic Materials": [
        "SiN", "silicon nitride", "AlN", "aluminum nitride",
        "TFLN", "thin-film lithium niobate",
        "BTO", "barium titanate",
        "thin-film lithium tantalate", "lithium tantalate",
        "heterogeneous integration"
    ],
    "Nonlinear & EO Devices": [
        "nonlinear frequency conversion", "electro-optic modulator",
        "on-chip comb",
        "acousto-optic", "acousto-optic modulator",
        "optical nonreciprocity", "nonreciprocal optics"
    ],
    "Multimodal Imaging & ML": [
        "microsphere imaging", "endomicroscopy",
        "photonic neural network", "on-chip machine learning",
        "optical computing"
    ],
    "Quantum Optics & Photonics": [
        "single photon", "quantum source",
        "entangled photons", "quantum interference", "quantum memory"
    ],
    "Microwave-to-Optical & Synthetic Dimensions": [
        "microwave-to-optical transducer", "microwave to optical conversion",
        "optomechanical interface", "electromechanical interface",
        "optical synthetic dimension", "synthetic frequency dimension",
        "synthetic photonics"
    ],
    "Core Topics (LNOI, Quantum etc)": [
        "LNOI", "lithium niobate", "LiNbO3", "SPDC", "electro-optic",
        "super-resolution imaging", "optical fiber endoscope"
    ]
}

CATEGORIES = [
    "physics.optics", "quant-ph", "eess.SP",
    "cs.CV", "cs.LG", "physics.app-ph"
]

MAX_RESULTS = 50
BASE_URL = "http://export.arxiv.org/api/query?"

# ---------- 抓取论文 ----------
def get_start_date_str():
    return (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")

def keyword_matched(text, keyword):
    return keyword.lower() in text.lower()

def is_category_allowed(entry):
    if not STRICT_CATEGORY_MODE:
        return True
    try:
        categories = []
        for tag in entry.tags:
            if isinstance(tag, dict) and 'term' in tag:
                categories.append(tag['term'])
            elif isinstance(tag, str):
                categories.append(tag)
        return any(cat in categories for cat in CATEGORIES)
    except:
        return False

def search_group(group_name, keywords):
    start_date = get_start_date_str()
    matched = []

    for kw in keywords:
        encoded_kw = quote(kw)
        cat_query = "+OR+".join([f"cat:{quote(cat)}" for cat in CATEGORIES])
        url = f"{BASE_URL}search_query=(ti:{encoded_kw}+OR+abs:{encoded_kw})+AND+({cat_query})&sortBy=submittedDate&sortOrder=descending&max_results={MAX_RESULTS}"
        print(f"🔍 [{group_name}] Searching: {kw}")
        feed = feedparser.parse(url)
        for entry in feed.entries:
            published = entry.published.split("T")[0]
            if published >= start_date and is_category_allowed(entry):
                title = entry.title.strip().replace('\n', ' ')
                abstract = entry.summary.strip().replace('\n', ' ')
                if keyword_matched(title, kw) or keyword_matched(abstract, kw):
                    matched.append({
                        "title": title,
                        "authors": ', '.join(author.name for author in entry.authors),
                        "summary": abstract,
                        "link": entry.link,
                        "keyword": kw,
                        "group": group_name
                    })
    return matched

# ---------- 构造邮件正文 ----------
def format_digest(grouped_entries):
    if all(len(papers) == 0 for papers in grouped_entries.values()):
        return "🛑 最近 %d 天内 arXiv 上没有找到匹配关键词的新论文。" % DAYS_BACK

    lines = ["📚 arXiv 关键词论文更新如下：\n"]
    total_papers = 0

    for group_name, papers in grouped_entries.items():
        if not papers:
            continue
        lines.append(f"#### 【{group_name}】 ({len(papers)} 篇)\n")
        total_papers += len(papers)
        for i, paper in enumerate(papers, 1):
            lines.append(f"{i}. {paper['title']}")
            lines.append(f"   作者: {paper['authors']}")
            lines.append(f"   匹配关键词（精确）: {paper['keyword']}")
            lines.append(f"   分类限定: {', '.join(CATEGORIES)}")
            lines.append(f"   链接: {paper['link']}")
            lines.append(f"   摘要: {paper['summary']}\n")

    lines.append(f"\n📊 共匹配到 {total_papers} 篇论文。")
    return "\n".join(lines)

# ---------- 发送邮件 ----------
def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(RECEIVER_EMAILS)  # 仅用于邮件头展示

    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)

        # 关键：这里必须传列表 RECEIVER_EMAILS
        server.sendmail(SENDER_EMAIL, RECEIVER_EMAILS, msg.as_string())
        server.quit()
        print("Email sent to:", RECEIVER_EMAILS)

    except Exception as e:
        print("Email send failed:", str(e))
        raise  # 关键：让 GitHub Actions 失败，便于排查

# ---------- 去重逻辑 ----------
def deduplicate_grouped_entries(grouped_entries):
    seen_titles = set()
    deduped = {}
    for group_name, papers in grouped_entries.items():
        deduped[group_name] = []
        for paper in papers:
            title_key = paper["title"].strip().lower()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                deduped[group_name].append(paper)
    return deduped

# ---------- 主流程 ----------
if __name__ == "__main__":
    print("🔍 正在抓取 arXiv 论文...")
    all_grouped = {}
    total_groups_with_hits = 0

    for group_name, kw_list in KEYWORD_GROUPS.items():
        results = search_group(group_name, kw_list)
        if results:
            total_groups_with_hits += 1
        all_grouped[group_name] = results

    all_grouped_deduped = deduplicate_grouped_entries(all_grouped)

    email_body = format_digest(all_grouped_deduped)
    today_str = datetime.now().strftime("%Y-%m-%d")
    subject = f"📬 arXiv Digest – {today_str} | {total_groups_with_hits} Groups Matched"
    send_email(subject, email_body)

# dummy comment to trigger push
