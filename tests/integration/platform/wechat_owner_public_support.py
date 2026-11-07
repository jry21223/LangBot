"""Frozen public source implementation, unpacked only inside isolated tests.

The archive contains committed Python and synthetic tests, never live profiles.
Its per-file manifest is checked before importing the real HTTP fixture.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile

import pytest


@pytest.fixture
def real_wechat_owner(tmp_path):
    base = Path(__file__).resolve().parents[2] / 'fixtures' / 'native_owner'
    assert hashlib.sha256((base / 'source-061479ead.tar.gz').read_bytes()).hexdigest() == (
        '1f6657021dea2e86cb64a2ec0c1ce2bc53857026999e4e0b1effbd0c0d458594'
    )
    manifest = json.loads((base / 'source-061479ead.manifest.json').read_text())
    assert manifest['revision'] == '061479ead0d4a91be16e8075ddde84ba5e69e5d5'
    assert not manifest['contains_private_profiles'] and not manifest['contains_production_credentials']
    root = tmp_path / 'public-source'
    with tarfile.open(base / 'source-061479ead.tar.gz') as archive:
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
    fixture_path = root / 'ops/tests/test_wechat_native_http.py'
    spec = importlib.util.spec_from_file_location('frozen_wechat_owner_http', fixture_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.scoped.__wrapped__(tmp_path)
    try:
        yield next(fixture)
    finally:
        fixture.close()
