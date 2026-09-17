import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import arxiv_daily_digest_simple as d
import state_store as store

NOW = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)


def atom(entries='', total=0):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"
        xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>{total}</opensearch:totalResults>
      {entries}</feed>'''.encode()


def entry(id='2609.00001v1', updated='2026-09-12T10:00:00Z',
          published='2026-09-12T10:00:00Z'):
    return f'''<entry><id>http://arxiv.org/abs/{id}</id>
      <title>Integrated lithium niobate photonics</title>
      <summary>lithium niobate frequency conversion</summary>
      <published>{published}</published><updated>{updated}</updated>
      <author><name>Test Author</name></author><category term="physics.optics"/>
      </entry>'''


class Response(io.BytesIO):
    headers = {}


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers['Retry-After'] = str(retry_after)
    headers['X-Request-Id'] = 'test-id'
    headers['Set-Cookie'] = 'must-not-be-logged'
    return HTTPError('https://export.arxiv.org/api/query', code, 'test', headers,
                     io.BytesIO(b'Rate exceeded.'))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = Path(self.temp.name)
        for name, value in [('STATE_PATH', p / 'state.json'),
                            ('SOURCE', 'api'),
                            ('OUTPUT_DIR', p / 'output'), ('_CUTOFF', None)]:
            ctx = patch.object(d, name, value)
            ctx.start()
            self.addCleanup(ctx.stop)
        ctx = contextlib.redirect_stdout(io.StringIO())
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def client(self, responses):
        queue = list(responses)
        calls, sleeps = [], []
        clock = [0.0]
        def opener(request, **kwargs):
            calls.append(request.full_url)
            response = queue.pop(0)
            if isinstance(response, Exception):
                raise response
            return Response(response)
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        state = d.load_state()
        client = d.Client(state, opener=opener, sleeper=sleep,
                          monotonic=lambda: clock[0], now=lambda: NOW)
        return client, calls, sleeps

    def args(self, **overrides):
        return argparse.Namespace(**dict({'dry_run': False, 'diagnose': False,
                                          'scheduled': False, 'initial_days': 7}, **overrides))

    def test_429_only_one_request_and_persisted_global_cooldown(self):
        client, calls, sleeps = self.client([http_error(429)])
        with self.assertRaises(d.Deferred):
            client.fetch('https://export.arxiv.org/api/query')
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        state = d.load_state()
        self.assertEqual(d.parse_arxiv_datetime(state['not_before']), NOW + timedelta(hours=1))
        with self.assertRaises(d.Deferred):
            client.fetch('https://export.arxiv.org/api/query?other=batch')
        self.assertEqual(len(calls), 1)
        report = (d.OUTPUT_DIR / 'http-diagnostics.jsonl').read_text()
        self.assertIn('Rate exceeded.', report)
        self.assertNotIn('must-not-be-logged', report)

    def test_retry_after_is_not_capped_shorter(self):
        client, calls, sleeps = self.client([http_error(429, 7200)])
        with self.assertRaises(d.Deferred):
            client.fetch('https://export.arxiv.org/api/query')
        self.assertEqual(d.parse_arxiv_datetime(client.state['not_before']), NOW + timedelta(hours=2))
        self.assertFalse(sleeps)

    def test_http_date_retry_after(self):
        with patch.object(d, 'utc_now', return_value=NOW):
            self.assertEqual(d.parse_retry_after('Sun, 13 Sep 2026 02:00:00 GMT'), 7200)

    def test_503_one_retry_can_recover(self):
        client, calls, sleeps = self.client([http_error(503), atom()])
        feed = client.fetch('https://export.arxiv.org/api/query')
        self.assertEqual(len(feed.entries), 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [30])

    def test_503_retry_after_defers_without_sleep(self):
        client, calls, sleeps = self.client([http_error(503, 120)])
        with self.assertRaises(d.Deferred):
            client.fetch('https://export.arxiv.org/api/query')
        self.assertEqual(sleeps, [])
        self.assertEqual(d.parse_arxiv_datetime(client.state['not_before']), NOW + timedelta(seconds=120))

    def test_html_is_not_a_successful_empty_feed(self):
        client, calls, sleeps = self.client([b'<html>Rate exceeded</html>'] * 2)
        with self.assertRaises(d.Deferred):
            client.fetch('https://export.arxiv.org/api/query')
        self.assertEqual(len(calls), 2)

    def test_missing_total_results_is_not_zero_papers(self):
        with self.assertRaises(d.InvalidFeed):
            d.validate_feed(b'<feed xmlns="http://www.w3.org/2005/Atom"/>')

    def test_timeout_is_bounded_retry(self):
        client, calls, sleeps = self.client([TimeoutError(), TimeoutError()])
        with self.assertRaises(d.Deferred):
            client.fetch('https://export.arxiv.org/api/query')
        self.assertEqual(len(calls), 2)

    def test_run_budget_stops_before_request(self):
        client, calls, sleeps = self.client([])
        client.deadline = 1
        with self.assertRaises(d.Deferred):
            client.fetch('https://export.arxiv.org/api/query')
        self.assertEqual(calls, [])

    def test_last_success_expands_backfill_after_outage(self):
        state = d.load_state()
        state['last_complete_at'] = '2026-09-05T00:00:00+00:00'
        d.begin_cycle(state, NOW, 7)
        self.assertEqual(state['pending']['cutoff'], '2026-09-03T00:00:00+00:00')

    def test_recent_success_uses_exactly_two_days(self):
        for hours in (1, 24, 30, 48):
            with self.subTest(hours=hours):
                state = d.load_state()
                state['last_complete_at'] = (NOW - timedelta(hours=hours)).isoformat()
                d.begin_cycle(state, NOW, 7)
                self.assertEqual(d.parse_arxiv_datetime(state['pending']['cutoff']),
                                 NOW - timedelta(days=2))

    def test_unfinished_cycle_keeps_original_backfill_window(self):
        state = d.load_state()
        d.begin_cycle(state, NOW - timedelta(days=4), 7)
        cutoff = state['pending']['cutoff']
        with patch.object(d.Client, 'fetch', side_effect=d.Deferred('Still unavailable')):
            self.assertEqual(d.run(self.args(dry_run=True)), 2)
        self.assertEqual(d.load_state()['pending']['cutoff'], cutoff)

    def test_old_paper_new_version_is_preserved(self):
        d._CUTOFF = NOW - timedelta(days=2)
        feed = d.validate_feed(atom(entry(id='2501.00001v3', published='2025-01-01T00:00:00Z'), 1))
        paper = d.parse_entry(feed.entries[0], 'Materials', ['lithium niobate'])
        self.assertEqual(paper['version'], 3)
        self.assertEqual(paper['status'], '重要版本更新')

    def test_incomplete_cached_batch_then_recovery(self):
        groups = {'G1': ['lithium niobate'], 'G2': ['frequency conversion']}
        with patch.object(d, 'KEYWORD_GROUPS', groups), patch.object(d, 'utc_now', return_value=NOW), \
                patch.object(d, 'validate_email_config'), patch.object(d, 'send_email') as send:
            def first_fetch(self, url):
                if 'frequency' in url:
                    self.defer('HTTP 429')
                return d.validate_feed(atom(entry(), 1))
            with patch.object(d.Client, 'fetch', first_fetch):
                self.assertEqual(d.run(self.args()), 2)
            state = d.load_state()
            self.assertTrue(state['pending']['batches']['1-1']['done'])
            self.assertIsNone(state['last_complete_at'])
            state['not_before'] = None
            d.atomic_json(d.STATE_PATH, state)
            fetched = []
            def second_fetch(self, url):
                fetched.append(url)
                return d.validate_feed(atom(entry(), 1))
            with patch.object(d.Client, 'fetch', second_fetch):
                self.assertEqual(d.run(self.args()), 0)
            self.assertEqual(len(fetched), 1)
            self.assertIn('frequency', fetched[0])
            self.assertEqual(len(send.call_args_list), 1)
            self.assertIn('1 Papers', send.call_args.args[0])
            state = d.load_state()
            self.assertIsNone(state['pending'])
            self.assertEqual(state['last_complete_at'], NOW.isoformat())

    def test_smtp_failure_does_not_advance_checkpoint(self):
        with patch.object(d, 'KEYWORD_GROUPS', {'G': ['lithium niobate']}), \
                patch.object(d, 'utc_now', return_value=NOW), \
                patch.object(d, 'validate_email_config'), \
                patch.object(d.Client, 'fetch', return_value=d.validate_feed(atom())), \
                patch.object(d, 'send_email', side_effect=RuntimeError('SMTP unavailable')):
            with self.assertRaises(RuntimeError):
                d.run(self.args())
            state = d.load_state()
            self.assertIsNone(state['last_complete_at'])
            self.assertTrue(state['pending']['batches']['1-1']['done'])

    def test_scheduled_success_skips_network_and_mail(self):
        state = d.load_state()
        state['last_complete_day'] = '2026-09-13'
        d.atomic_json(d.STATE_PATH, state)
        with patch.object(d, 'utc_now', return_value=NOW), \
                patch.object(d.Client, 'fetch') as fetch, patch.object(d, 'send_email') as send:
            self.assertEqual(d.run(self.args(scheduled=True)), 0)
        fetch.assert_not_called()
        send.assert_not_called()

    def test_page_limit_marks_incomplete(self):
        client, _, _ = self.client([atom(entry(), 1000)])
        d._CUTOFF = NOW - timedelta(days=2)
        progress = {'done': False, 'papers': []}
        with patch.object(d, 'MAX_PAGES_PER_QUERY', 1):
            with self.assertRaises(d.Deferred):
                d.collect_batch(client, 'G', ['lithium niobate'], progress)
        self.assertFalse(progress['done'])

    def test_corrupt_state_is_not_silently_reset(self):
        d.STATE_PATH.write_text('broken json')
        with self.assertRaises(json.JSONDecodeError):
            d.load_state()

    def test_dry_run_never_sends_or_marks_delivered(self):
        with patch.object(d, 'KEYWORD_GROUPS', {'G': ['lithium niobate']}), \
                patch.object(d, 'utc_now', return_value=NOW), \
                patch.object(d.Client, 'fetch', return_value=d.validate_feed(atom())), \
                patch.object(d, 'send_email') as send:
            self.assertEqual(d.run(self.args(dry_run=True)), 0)
        send.assert_not_called()
        self.assertIsNone(d.load_state()['last_complete_at'])

    def test_state_save_failure_does_not_force_overwrite(self):
        state = d.load_state()
        d.atomic_json(d.STATE_PATH, state)
        remote = Path(self.temp.name) / 'remote.json'
        remote.write_text(json.dumps({'branch_missing': False, 'sha': 'old-sha'}))
        with patch.object(store, 'STATE', d.STATE_PATH), patch.object(store, 'REMOTE', remote), \
                patch.object(store, 'api', side_effect=http_error(409)) as api:
            with self.assertRaises(HTTPError):
                store.save()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[2]['sha'], 'old-sha')


if __name__ == '__main__':
    unittest.main()
