"""Frozen public source implementation, unpacked only inside isolated tests.

The archive contains committed Python and synthetic tests, never live profiles.
Its per-file manifest is checked before importing the real HTTP fixture.
"""

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
import tarfile

import pytest


@pytest.fixture
def real_wechat_owner(tmp_path):
    base = Path(__file__).resolve().parents[2] / 'fixtures' / 'native_owner'
    assert hashlib.sha256((base / 'source-481338b44-v3.tar.gz').read_bytes()).hexdigest() == (
        '15f1cffce69fe5df7a043d50255555519a123a30aaa27be85f26a5a13bb451a6'
    )
    manifest = json.loads((base / 'source-481338b44-v3.manifest.json').read_text())
    assert manifest['revision'] == '481338b44dd60d8de03cd6bf64aaf0ff3b6035bd'
    assert not manifest['contains_private_profiles'] and not manifest['contains_production_credentials']
    root = tmp_path / 'public-source'
    with tarfile.open(base / 'source-481338b44-v3.tar.gz') as archive:
        members = archive.getmembers()
        assert {member.name for member in members} == set(manifest['files']) | {'PUBLIC_TEST_MANIFEST.json'}
        for member in members:
            path = Path(member.name)
            assert member.isfile() and not path.is_absolute() and '..' not in path.parts
            data = archive.extractfile(member).read()
            if member.name in manifest['files']:
                assert hashlib.sha256(data).hexdigest() == manifest['files'][member.name]
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    # Keep one enterprise_agent namespace while resolving exported ops.hermes
    # dependencies from the same verified package, never another checkout.
    for import_root in (root, root / 'ops', root / 'ops/tests'):
        sys.path.insert(0, str(import_root))
    fixture_path = root / 'ops/tests/test_wechat_native_http.py'
    spec = importlib.util.spec_from_file_location('frozen_wechat_owner_http', fixture_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.scoped.__wrapped__(tmp_path)
    try:
        yield next(fixture)
    finally:
        fixture.close()
