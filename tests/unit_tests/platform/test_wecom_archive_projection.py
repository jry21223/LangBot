"""Native archived revisions preserve identity without publishing original bodies."""

import json

from langbot.pkg.platform.native_contract import SourceBinding
from langbot.pkg.platform.adapters.wecom_archive.projection import project_page


def archived_page(kind='created', available=True):
    return {
        'source': 'fixture-wecom',
        'scope': 'fixture-chat',
        'complete_page': True,
        'previous_cursor': None,
        'next_cursor': 'cursor-one',
        'coverage': 'success',
        'gaps': [],
        'items': [
            {
                'source': 'fixture-wecom',
                'scope': 'fixture-chat',
                'record': 'message-one',
                'version': 'wr-1',
                'sequence': 1,
                'kind': kind,
                'business_at': '2026-01-01T00:00:00Z',
                'observed_at': '2026-01-01T00:00:01Z',
                'original': 'wecom:message/fixture/wr-1',
                'projection_hash': 'a' * 64,
                'original_available': available,
                'saved': {
                    'version': 'wr-1',
                    'sequence': 1,
                    'observed_at': '2026-01-01T00:00:01Z',
                    'original': 'wecom:message/fixture/wr-1',
                    'attachments': [],
                    'message': {
                        'message_id': 'message-one',
                        'chat_id': 'chat-one',
                        'chat_type': 'group',
                        'sender_user_id': 'sender-one',
                        'sender_identity_type': 'CUSTOMER',
                        'message_type': 'text',
                        'source_seq': 19,
                        'action': 'send',
                        'sent_at': '2026-01-01T00:00:00Z',
                        'content': 'PRIVATE ORIGINAL MUST STAY AT OWNER',
                        'raw_payload': {'token': 'DO NOT PERSIST'},
                    },
                },
            }
        ],
    }


def test_narrow_page_retains_native_revision_and_excludes_original_body():
    page = project_page(archived_page(), SourceBinding('fixture-wecom', 'fixture-corp', 'fixture-chat', 'grant-v1'))
    assert page.next_cursor == 'cursor-one'
    event = page.events[0]
    assert (event.record, event.version, event.reference) == ('message-one', 'wr-1', 'wecom:message/fixture/wr-1')
    assert event.payload['change']['sequence'] == 1
    assert event.payload['change']['saved']['message']['source_seq'] == 19
    assert event.payload['dispatch_eligible'] is True
    assert 'PRIVATE ORIGINAL' not in json.dumps(event.payload)
    assert 'DO NOT PERSIST' not in json.dumps(event.payload)


def test_baseline_and_recall_still_reach_handoff_without_new_business_dispatch():
    binding = SourceBinding('fixture-wecom', 'fixture-corp', 'fixture-chat', 'grant-v1')
    baseline = project_page(archived_page(kind='baseline_snapshot'), binding)
    assert len(baseline.events) == 1
    assert baseline.events[0].payload['dispatch_eligible'] is False
    recalled = archived_page(kind='recalled', available=False)
    recalled['items'][0]['saved']['message'].update(
        action='recall', message_type='revoke', recalled_message_id='message-one'
    )
    recall = project_page(recalled, binding)
    assert len(recall.events) == 1
    assert recall.events[0].kind == 'recalled'
    assert recall.events[0].payload['dispatch_eligible'] is False


def test_media_pending_is_retained_but_only_complete_version_is_dispatch_eligible():
    from copy import deepcopy

    binding = SourceBinding('fixture-wecom', 'fixture-corp', 'fixture-chat', 'grant-v1')
    pending = archived_page(kind='attachments_changed', available=False)
    saved = pending['items'][0]['saved']
    saved['message'].update(
        message_type='image', content_materialization={'status': 'pending', 'evidence_fingerprint': 'b' * 64}
    )
    saved['attachments'] = [
        {'reference': 'wecom:media/fixture-image', 'status': 'missing', 'type': 'image', 'sha256': None, 'size': None}
    ]
    event = project_page(pending, binding).events[0]
    assert event.payload['dispatch_eligible'] is False
    assert event.payload['change']['saved']['attachments'][0]['status'] == 'missing'
    complete = deepcopy(pending)
    complete['items'][0].update(
        version='wr-2', sequence=2, original='wecom:message/fixture/wr-2', original_available=True
    )
    saved = complete['items'][0]['saved']
    saved.update(version='wr-2', sequence=2, original='wecom:message/fixture/wr-2')
    saved['attachments'][0].update(status='saved', sha256='c' * 64, size=7)
    saved['message']['content_materialization'] = {
        'status': 'complete',
        'parser_version': 'fixture-v1',
        'input_hash': 'd' * 64,
        'result_sha256': 'e' * 64,
        'text_bytes': 7,
        'parts': 1,
    }
    event = project_page(complete, binding).events[0]
    assert event.version == 'wr-2'
    assert event.payload['dispatch_eligible'] is True


def test_invalid_second_revision_rejects_the_whole_page():
    from copy import deepcopy
    import pytest
    from langbot.pkg.platform.adapters.wecom_archive.reader import SourceUnavailable

    page = archived_page()
    other = deepcopy(page['items'][0])
    other['scope'] = 'wrong-chat'
    page['items'].append(other)
    with pytest.raises(SourceUnavailable):
        project_page(page, SourceBinding('fixture-wecom', 'fixture-corp', 'fixture-chat', 'grant-v1'))


def test_metadata_cannot_smuggle_nested_body_or_untyped_sequence():
    import pytest
    from langbot.pkg.platform.adapters.wecom_archive.reader import SourceUnavailable

    binding = SourceBinding('fixture-wecom', 'fixture-corp', 'fixture-chat', 'grant-v1')
    for field, value in [
        ('sender_access_type', {'content': 'private'}),
        ('media_evidence', [{'mediaId': {'content': 'private'}}]),
    ]:
        page = archived_page()
        page['items'][0]['saved']['message'][field] = value
        with pytest.raises(SourceUnavailable):
            project_page(page, binding)
    page = archived_page()
    page['items'][0]['saved']['sequence'] = True
    with pytest.raises(SourceUnavailable):
        project_page(page, binding)


def test_image_voice_and_file_versions_preserve_refs_without_body_or_host_path():
    from copy import deepcopy

    binding = SourceBinding('fixture-wecom', 'fixture-corp', 'fixture-chat', 'grant-v1')
    for media_type in ('image', 'voice', 'file'):
        page = deepcopy(archived_page(kind='attachments_changed'))
        saved = page['items'][0]['saved']
        saved['attachments'] = [
            {
                'reference': 'wecom:media/fixture-' + media_type,
                'status': 'saved',
                'type': media_type,
                'sha256': 'c' * 64,
                'size': 7,
            }
        ]
        saved['message'].update(
            message_type=media_type,
            content_materialization={
                'status': 'complete',
                'parser_version': 'fixture-v1',
                'input_hash': 'd' * 64,
                'result_sha256': 'e' * 64,
                'text_bytes': 7,
                'parts': 1,
            },
            media_evidence=[
                {
                    'mediaId': 'fixture-' + media_type,
                    'mediaType': media_type,
                    'contentSha256': 'c' * 64,
                    'contentSize': 7,
                    'sourcePath': '/private/owner/original',
                }
            ],
        )
        event = project_page(page, binding).events[0]
        assert event.payload['dispatch_eligible'] is True
        assert event.payload['change']['saved']['attachments'][0]['type'] == media_type
        assert '/private/owner/original' not in json.dumps(event.payload)
        assert 'PRIVATE ORIGINAL' not in json.dumps(event.payload)
