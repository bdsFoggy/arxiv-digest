# arXiv 光子学与量子信息日报

按 6 个学科获取 arXiv 官方 OAI-PMH 增量元数据，在程序内匹配原有 115 个关键词、7 个主题，去重后发送日报。

## 2026-09-14 修复

旧的关键词搜索接口持续出现超时和 HTTP 429。此前修复了长时间等待，但仍无法获取论文。这次默认数据源改为 `https://oaipmh.arxiv.org/oai`，使用 `ListRecords`、`arXivRaw` 和学科集合；不再发出 22 批关键词搜索。

- 根据 OAI 元数据修改日期增量获取，再根据 `arXivRaw` 的实际版本提交时间判断论文更新。修改 DOI、期刊信息不会被误报为新版论文。
- 保留旧论文近期 v2/v3 更新和包含斜线的旧式 arXiv ID。
- 保留已有待补查起点；切换来源时重新检索该范围，不把旧接口的完成批次当作 OAI 完成。
- 每页保存论文和续传令牌。令牌过期或服务器拒绝时，按原日期范围重新获取；超过预算不会冒充完整结果。
- 单连接，请求起始至少相隔 10 秒；429/403 立即停止整个任务，持久保存冷却时间并遵守 Retry-After。来源切换不会清空已有冷却时间，没有自动轮换接口/IP 重试。
- `SiN`、`AlN` 等材料缩写按完整词匹配，避免把 `using`、`since` 等普通单词误判为材料关键词。

首次安装默认补查 7 天。正常连续成功时，论文筛选窗口严格为当前时间往前 48 小时。上次成功周期起点早于 48 小时前时，才从该起点再回溯 2 天补查；未完成任务继续使用原有补查起点，直到完成。OAI 查询日期按 UTC 天取整，但论文最终按实际版本时间进行 48 小时过滤。OAI 在论文公告后更新，超过窗口的发布延迟仍可能需要扩大补查范围。每日 RSS 不用作历史补查。

## GitHub 网页操作

Actions → Arxiv Daily Digest → Run workflow，分支选 `main`。

| mode | 行为 |
|---|---|
| `resume` | 完整获取并向既有收件人发送日报 |
| `preview` | 完整获取、保存补查进度和预览，不发邮件、不推进交付时间 |
| `diagnose` | 请求一个 OAI 页面，不发邮件；成功不代表全部学科已获取 |

相关代码推送到 `main` 后自动运行测试和预览；当天日报已完成时自动预览跳过获取，避免建立多余的待发送周期。手动 `preview` 仍执行完整获取。定时运行在新加坡时间 08:07、10:17、14:27、20:37 执行；当天已完成则跳过。GitHub 可能延迟触发。

原有 `SENDER_EMAIL`、`SENDER_PASSWORD` Secrets 和收件人保持原设置。专用 `arxiv-digest-state` 分支保存进度，不要删除。工作流需要 `contents: write`，所有运行使用同一个 concurrency group。保存失败会报错，Artifacts 保留状态备份。

`COMPLETE` 表示所有学科和分页获取完成；`INCOMPLETE` 表示覆盖尚不完整，不能把 0 篇理解为没有匹配论文。SMTP 失败不推进交付时间。邮件发送和 GitHub 状态保存不构成原子事务，保存失败可能导致重复邮件。

## 验证

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python arxiv_daily_digest_simple.py --dry-run
```

2026-09-14 本地 31 项测试通过。另在调查环境实际获取 2026-09-06 至 2026-09-14 六个学科的全部返回页，7 次 HTTP 请求均成功，约 2 分 18 秒完成。修正缩写匹配后，使用这些真实记录离线重新筛选得到 58 篇、7 个主题。没有发送测试邮件。这些数字属于该时间窗口，不保证未来日报篇数相同，也不能替代 GitHub runner 的验证。

需要专门诊断旧搜索服务时可显式指定 `--source api --diagnose`，它也遵守保存的冷却时间。旧服务是否恢复与 OAI 能否工作是两件独立的事。

## 官方说明

- [OAI-PMH 接口、版本历史、日期和学科集合](https://info.arxiv.org/help/oa/index.html)
- [arXiv API 使用条款与速率限制](https://info.arxiv.org/help/api/tou.html)
- [每日 RSS 的范围与限制](https://info.arxiv.org/help/rss.html)
