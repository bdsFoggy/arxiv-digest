"""Persist digest checkpoints in a dedicated GitHub branch using optimistic writes.

Only digest-state.json is stored. No credentials, mail configuration, or diagnostics.
GitHub credentials stay in environment variables, never in command arguments/logs.
"""
import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

BRANCH = 'arxiv-digest-state'
STATE = Path(os.getenv('DIGEST_STATE_PATH', 'digest-state.json'))
REMOTE = Path('.digest-remote.json')


def api(method, path, data=None):
    repo = os.environ['GITHUB_REPOSITORY']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise RuntimeError('Invalid repository name')
    payload = json.dumps(data).encode() if data is not None else None
    request = Request('https://api.github.com/repos/' + repo + path,
                      data=payload, method=method, headers={
                          'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                          'Accept': 'application/vnd.github+json',
                          'Content-Type': 'application/json',
                          'User-Agent': 'arxiv-digest-state-store'})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def restore():
    try:
        api('GET', '/git/ref/heads/' + BRANCH)
    except HTTPError as error:
        if error.code != 404:
            raise
        REMOTE.write_text(json.dumps({'branch_missing': True, 'sha': None}))
        print('No recovery branch yet; initial installation will backfill seven days.')
        return
    # If a branch exists but its state is missing/unreadable, fail explicitly.
    # A network failure or malformed file MUST NOT silently reset the coverage.
    item = api('GET', '/contents/digest-state.json?ref=' + quote(BRANCH))
    raw = base64.b64decode(item['content'])
    value = json.loads(raw)
    if value.get('schema') != 1:
        raise RuntimeError('Invalid remote state schema')
    STATE.write_bytes(raw)
    REMOTE.write_text(json.dumps({'branch_missing': False, 'sha': item['sha'],
                                 'content_hash': hashlib.sha256(raw).hexdigest()}))
    print('Restored durable digest recovery checkpoint.')


def save():
    if not STATE.exists():
        print('No state change to save.')
        return
    remote = json.loads(REMOTE.read_text())
    value = json.loads(STATE.read_text())
    if value.get('schema') != 1:
        raise RuntimeError('Refusing to save invalid state')
    if remote.get('content_hash') == hashlib.sha256(STATE.read_bytes()).hexdigest():
        print('Recovery checkpoint unchanged; no repository write needed.')
        return
    if remote['branch_missing']:
        api('POST', '/git/refs', {'ref': 'refs/heads/' + BRANCH,
                                 'sha': os.environ['GITHUB_SHA']})
    payload = {'message': 'Update arXiv digest recovery checkpoint',
               'content': base64.b64encode(STATE.read_bytes()).decode(),
               'branch': BRANCH}
    if remote['sha']:
        payload['sha'] = remote['sha']
    api('PUT', '/contents/digest-state.json', payload)
    print('Saved durable checkpoint; concurrent changes are never overwritten.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('restore', 'save'))
    args = parser.parse_args()
    try:
        restore() if args.command == 'restore' else save()
    except Exception as error:
        # Do not dump request objects or tokens on failure.
        print(f'State persistence failed: {type(error).__name__}: {error}', flush=True)
        raise SystemExit(1)
