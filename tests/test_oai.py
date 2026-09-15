import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import arxiv_daily_digest_simple as d

NOW = datetime(2026, 9, 14, 3, tzinfo=timezone.utc)


def record(id='2609.00001', versions=None, deleted=False):
    if deleted:
        return '<record><header status="deleted"><identifier>oai:arXiv.org:x</identifier></header></record>'
    versions = versions or [('v1', 'Fri, 11 Sep 2026 10:00:00 GMT')]
    history = ''.join(f'<version version="{v}"><date>{date}</date></version>' for v, date in versions)
    return f'''<record><header><identifier>oai:arXiv.org:{id}</identifier>
      <datestamp>2026-09-13</datestamp></header><metadata>
      <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/"><id>{id}</id>{history}
      <title>Lithium niobate frequency conversion</title><abstract>Photonics</abstract>
      <authors>A Author and B Author</authors><categories>physics.optics quant-ph</categories>
      </arXivRaw></metadata></record>'''


def response(rows=None, token=None, error=None):
    if error:
        body = f'<error code="{error}">test</error>'
    else:
        tail = f'<resumptionToken>{token}</resumptionToken>' if token else ''
        body = f'<ListRecords>{record() if rows is None else rows}{tail}</ListRecords>'
    return f'''<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
      <responseDate>2026-09-14T03:00:00Z</responseDate>{body}</OAI-PMH>'''.encode()


class OAITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for name, value in [('STATE_PATH', Path(temp.name)/'state.json'),
                            ('OUTPUT_DIR', Path(temp.name)/'output'),
                            ('SOURCE', 'oai'), ('utc_now', lambda: NOW),
                            ('_CUTOFF', NOW-timedelta(days=8))]:
            ctx = patch.object(d, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        ctx = contextlib.redirect_stdout(io.StringIO())
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def args(self, **kwargs):
        return argparse.Namespace(**dict(dict(dry_run=True, diagnose=False,
                                             scheduled=False, initial_days=8), **kwargs))

    def test_oai_metadata_edit_is_not_a_new_paper(self):
        feed = d.validate_oai(response(record(versions=[('v1', 'Wed, 01 Jan 2025 10:00:00 GMT')])))
        self.assertIsNone(d.parse_entry(feed.entries[0], 'G', ['lithium niobate']))

    def test_material_abbreviations_do_not_match_ordinary_words(self):
        for text in ('using signals since yesterday', 'personalnet', 'SPDCnet'):
            for keyword in ('SiN', 'AlN', 'SPDC'):
                self.assertFalse(d.keyword_matched(text, keyword))
        for text, keyword in [('SiN-based waveguide', 'SiN'), ('AlN resonator', 'AlN'),
                              ('MgO:PPLN', 'PPLN'), ('SPDC source', 'SPDC')]:
            self.assertTrue(d.keyword_matched(text, keyword))

    def test_legacy_id_recent_replacement_uses_actual_version_date(self):
        feed = d.validate_oai(response(record(id='quant-ph/9901001', versions=[
            ('v1', 'Fri, 01 Jan 1999 10:00:00 GMT'), ('v3', 'Fri, 11 Sep 2026 10:00:00 GMT')])))
        paper = d.parse_entry(feed.entries[0], 'G', ['lithium niobate'])
        self.assertEqual(paper['base_arxiv_id'], 'quant-ph/9901001')
        self.assertEqual(paper['version'], 3)
        self.assertEqual(paper['status'], '重要版本更新')

    def test_only_explicit_no_records_is_empty_success(self):
        self.assertEqual(d.validate_oai(response(error='noRecordsMatch')).entries, [])
        self.assertEqual(d.validate_oai(response(record(deleted=True))).entries, [])
        for payload in [response(rows=''), response(error='badArgument'), b'<html/>']:
            with self.assertRaises(d.InvalidFeed):
                d.validate_oai(payload)

    def test_missing_versions_never_becomes_empty_success(self):
        payload = response().replace(b'version="v1"', b'version="v2"')
        with self.assertRaises(d.InvalidFeed):
            d.validate_oai(payload)

    def test_token_request_does_not_repeat_selection_parameters(self):
        url = d.build_oai_url('physics.optics', '2026-09-06', '2026-09-14', 'a+b/c==')
        self.assertEqual(parse_qs(urlparse(url).query),
                         {'verb': ['ListRecords'], 'resumptionToken': ['a+b/c==']})

    def test_pagination_resume_cross_category_dedup_and_dry_run(self):
        seen = []
        def fetch(client, url, validator):
            seen.append(url)
            return validator(response())
        with patch.object(d.Client, 'fetch', fetch), patch.object(d, 'send_email') as send:
            self.assertEqual(d.run(self.args()), 0)
            self.assertEqual(len(seen), 6)
            self.assertEqual(d.run(self.args()), 0)
            self.assertEqual(len(seen), 6)
        send.assert_not_called()
        state = d.load_state()
        self.assertIsNone(state['last_complete_at'])
        grouped = d.grouped_from_cycle(state['pending'])
        self.assertEqual(sum(map(len, grouped.values())), 1)
        self.assertGreater(len(next(p for ps in grouped.values() for p in ps)['groups']), 1)

    def test_page_budget_saves_and_resumes_token(self):
        state = d.load_state()
        d.begin_cycle(state, NOW, 8)
        client = d.Client(state, now=lambda: NOW)
        progress = state['pending']['batches'].setdefault('physics.optics', {})
        with patch.object(client, 'fetch', return_value=d.validate_oai(response(token='page2'))), \
                patch.object(d, 'MAX_PAGES_PER_QUERY', 1):
            with self.assertRaises(d.Deferred):
                d.collect_oai(client, 'physics.optics', progress)
        self.assertEqual(d.load_state()['pending']['batches']['physics.optics']['token'], 'page2')
        with patch.object(client, 'fetch', return_value=d.validate_oai(response())) as fetch:
            d.collect_oai(client, 'physics.optics', progress)
        self.assertIn('resumptionToken=page2', fetch.call_args.args[0])
        self.assertTrue(progress['done'])

    def test_expired_token_restarts_fixed_range(self):
        state = d.load_state()
        d.begin_cycle(state, NOW-timedelta(days=1), 8)
        state['pending']['oai_until'] = '2026-09-13'
        client = d.Client(state, now=lambda: NOW)
        progress = {'token': 'old', 'token_day': '2026-09-13', 'papers': []}
        with patch.object(client, 'fetch', return_value=d.validate_oai(response())) as fetch:
            d.collect_oai(client, 'physics.optics', progress)
        query = parse_qs(urlparse(fetch.call_args.args[0]).query)
        self.assertEqual(query['until'], ['2026-09-13'])
        self.assertEqual(query['from'], ['2026-09-05'])
        self.assertNotIn('resumptionToken', query)

    def test_invalid_token_is_cleared_for_next_run(self):
        state = d.load_state()
        d.begin_cycle(state, NOW, 8)
        client = d.Client(state, now=lambda: NOW)
        progress = state['pending']['batches'].setdefault('physics.optics',
            {'token': 'bad', 'token_day': '2026-09-14', 'papers': []})
        with patch.object(client, 'fetch', side_effect=d.BadResumptionToken()):
            with self.assertRaises(d.Deferred):
                d.collect_oai(client, 'physics.optics', progress)
        self.assertFalse(progress['done'])
        self.assertNotIn('token', progress)

    def test_migration_preserves_uncovered_cutoff_and_cooldown(self):
        state = d.load_state()
        d.begin_cycle(state, NOW-timedelta(days=1), 8)
        cutoff = state['pending']['cutoff']
        state['pending']['config_hash'] = 'old-api-configuration'
        state['pending']['batches'] = {'1-1': {'done': True, 'papers': []}}
        state['not_before'] = (NOW+timedelta(hours=1)).isoformat()
        d.atomic_json(d.STATE_PATH, state)
        with patch.object(d.Client, 'fetch', autospec=True) as fetch:
            fetch.side_effect = lambda client, *a: d.validate_oai(response())
            self.assertEqual(d.run(self.args()), 0)
        after = d.load_state()
        self.assertEqual(after['pending']['cutoff'], cutoff)
        self.assertEqual(after['not_before'], state['not_before'])
        self.assertNotIn('1-1', after['pending']['batches'])
        self.assertIsNone(after['last_complete_at'])

    def test_oai_smtp_failure_keeps_all_completed_categories(self):
        with patch.object(d.Client, 'fetch', return_value=d.validate_oai(response())), \
                patch.object(d, 'validate_email_config'), \
                patch.object(d, 'send_email', side_effect=RuntimeError('SMTP')):
            with self.assertRaises(RuntimeError):
                d.run(self.args(dry_run=False))
        state = d.load_state()
        self.assertIsNone(state['last_complete_at'])
        self.assertTrue(all(v['done'] for v in state['pending']['batches'].values()))

    def test_diagnostic_calls_oai_without_email_or_delivery(self):
        with patch.object(d.Client, 'fetch', return_value=d.validate_oai(response())) as fetch, \
                patch.object(d, 'send_email') as send:
            self.assertEqual(d.run(self.args(diagnose=True)), 0)
        self.assertIn('oaipmh.arxiv.org', fetch.call_args.args[0])
        send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
