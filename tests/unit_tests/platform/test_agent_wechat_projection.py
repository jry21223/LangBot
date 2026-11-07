"""Native projections retain identity without copying raw content into the queue."""

import unittest

from langbot.pkg.platform.adapters.agent_wechat.projection import project_message


def message(**updates):
    value = {
        'id': 'msg-1',
        'accountId': 'account-1',
        'threadId': 'group-1',
        'serverId': '9007199254740993123',
        'senderId': 'member-1',
        'type': 1,
        'direction': 'incoming',
        'body': 'private fixture text',
        'version': 'native-version-1',
        'createdAt': '2026-11-08T00:00:00Z',
        'media': [],
    }
    return dict(value, **updates)


class ProjectionTests(unittest.TestCase):
    def test_live_text_keeps_native_identity_without_raw_body(self):
        envelope = project_message(
            message(),
            cursor='10',
            origin='live',
            account='account-1',
            allowed_threads={'group-1'},
            activation_at='2026-11-08T00:00:00Z',
            source='fixture-wechat',
            scope='fixture-group',
        )
        self.assertEqual(envelope.payload['native']['message_id'], 'msg-1')
        self.assertEqual(envelope.payload['native']['server_id'], '9007199254740993123')
        self.assertTrue(envelope.payload['dispatch_eligible'])
        self.assertNotIn('private fixture text', str(envelope.payload))
        replay = project_message(
            message(),
            cursor='10',
            origin='live',
            account='account-1',
            allowed_threads={'group-1'},
            activation_at='2026-11-08T00:00:00Z',
            source='fixture-wechat',
            scope='fixture-group',
        )
        self.assertEqual(envelope, replay)

    def test_unknown_origins_are_visible_but_not_dispatched(self):
        for origin in ['profile_import', 'unverified']:
            envelope = project_message(
                message(),
                cursor='10',
                origin=origin,
                account='account-1',
                allowed_threads={'group-1'},
                activation_at='2026-11-08T00:00:00Z',
                source='fixture-wechat',
                scope='fixture-group',
            )
            self.assertFalse(envelope.payload['dispatch_eligible'])
            self.assertEqual(envelope.kind, 'baseline')

    def test_catchup_before_activation_is_baseline_but_later_gap_is_eligible(self):
        old = project_message(
            message(createdAt='2026-11-07T23:59:59Z'),
            cursor='10',
            origin='catchup',
            account='account-1',
            allowed_threads={'group-1'},
            activation_at='2026-11-08T00:00:00Z',
            source='fixture-wechat',
            scope='fixture-group',
        )
        self.assertFalse(old.payload['dispatch_eligible'])
        later = project_message(
            message(createdAt='2026-11-08T00:01:00Z'),
            cursor='11',
            origin='catchup',
            account='account-1',
            allowed_threads={'group-1'},
            activation_at='2026-11-08T00:00:00Z',
            source='fixture-wechat',
            scope='fixture-group',
        )
        self.assertTrue(later.payload['dispatch_eligible'])

    def test_unapproved_group_and_own_send_are_not_projected(self):
        for original in [message(threadId='other-group'), message(direction='outgoing')]:
            self.assertIsNone(
                project_message(
                    original,
                    cursor='10',
                    origin='live',
                    account='account-1',
                    allowed_threads={'group-1'},
                    activation_at='2026-11-08T00:00:00Z',
                    source='fixture-wechat',
                    scope='fixture-group',
                )
            )

    def test_media_pending_to_available_gets_new_version_and_keeps_record(self):
        asset = {
            'id': 'asset-1',
            'accountId': 'account-1',
            'messageId': 'msg-1',
            'kind': 'image',
            'state': 'pending',
            'contentUrl': 'http://not-retained.invalid',
        }
        original = message(type=3, body=None)
        pending = project_message(
            original,
            cursor='11',
            origin='live',
            account='account-1',
            allowed_threads={'group-1'},
            activation_at='2026-11-08T00:00:00Z',
            source='fixture-wechat',
            scope='fixture-group',
            assets=[asset],
        )
        self.assertFalse(pending.payload['dispatch_eligible'])
        available = project_message(
            original,
            cursor='12',
            origin='live',
            account='account-1',
            allowed_threads={'group-1'},
            activation_at='2026-11-08T00:00:00Z',
            source='fixture-wechat',
            scope='fixture-group',
            assets=[dict(asset, state='available', sha256='a' * 64, byteLength=4, contentType='image/png')],
        )
        self.assertEqual(pending.record, available.record)
        self.assertNotEqual(pending.version, available.version)
        self.assertTrue(available.payload['dispatch_eligible'])
        self.assertNotIn('contentUrl', str(available.payload))

    def test_recall_is_invalidation_even_if_original_body_still_present(self):
        envelope = project_message(
            message(recall={'observedAt': 'now'}),
            cursor='13',
            origin='live',
            account='account-1',
            allowed_threads={'group-1'},
            activation_at='2026-11-08T00:00:00Z',
            source='fixture-wechat',
            scope='fixture-group',
        )
        self.assertEqual(envelope.kind, 'recalled')
        self.assertFalse(envelope.payload['dispatch_eligible'])
        self.assertTrue(envelope.payload['recalled'])

    def test_wrong_account_and_asset_binding_are_rejected(self):
        for original, assets in [
            (message(accountId='other'), []),
            (message(type=3), [{'id': 'asset-1', 'accountId': 'other', 'messageId': 'msg-1', 'kind': 'image'}]),
        ]:
            with self.assertRaises(ValueError):
                project_message(
                    original,
                    cursor='10',
                    origin='live',
                    account='account-1',
                    allowed_threads={'group-1'},
                    activation_at='2026-11-08T00:00:00Z',
                    source='fixture-wechat',
                    scope='fixture-group',
                    assets=assets,
                )


if __name__ == '__main__':
    unittest.main()


def test_v14_cursor_rejects_opaque_overflow_noncanonical_and_missing():
    import pytest

    for cursor in ['opaque:10', '', None, '01', '9223372036854775808', '١', '-1', '0']:
        with pytest.raises(ValueError):
            project_message(
                message(),
                cursor=cursor,
                origin='live',
                account='account-1',
                allowed_threads={'group-1'},
                activation_at='2026-11-08T00:00:00Z',
                source='fixture',
                scope='scope',
            )
    envelope = project_message(
        message(),
        cursor='9223372036854775807',
        origin='live',
        account='account-1',
        allowed_threads={'group-1'},
        activation_at='2026-11-08T00:00:00Z',
        source='fixture',
        scope='scope',
    )
    assert envelope.payload['event_cursor'] == '9223372036854775807'
    assert envelope.payload['change']['sequence'] == 9223372036854775807


def test_missing_native_identity_is_never_invented():
    envelope = project_message(
        message(serverId=None, senderId=None),
        cursor='10',
        origin='live',
        account='account-1',
        allowed_threads={'group-1'},
        activation_at='2026-11-08T00:00:00Z',
        source='fixture',
        scope='scope',
    )
    assert envelope.payload['change']['saved']['sender_id'] is None
    assert envelope.payload['change']['saved']['server_id'] is None
    assert not envelope.payload['dispatch_eligible']


def test_canonical_hash_rejects_nonfinite_and_preserves_unicode():
    import hashlib
    import pytest
    from langbot.pkg.platform.adapters.agent_wechat.projection import digest

    assert digest({'中': '文'}) == hashlib.sha256('{"中":"文"}'.encode()).hexdigest()
    for value in [float('nan'), float('inf'), -float('inf')]:
        with pytest.raises(ValueError):
            digest({'value': value})


def test_saved_contract_is_validated_before_source_ack():
    from copy import deepcopy
    import pytest
    from langbot.pkg.platform.adapters.agent_wechat.projection import validate_change, digest

    envelope = project_message(
        message(),
        cursor='10',
        origin='live',
        account='account-1',
        allowed_threads={'group-1'},
        activation_at='2026-11-08T00:00:00Z',
        source='fixture',
        scope='scope',
    )
    validate_change(envelope.payload['change'])
    for mutate in [
        lambda c: c['saved'].update(body='private'),
        lambda c: c.update(original='wechat:message/wrong'),
        lambda c: c.update(sequence=True),
        lambda c: c['saved'].update(direction='outgoing'),
    ]:
        bad = deepcopy(envelope.payload['change'])
        mutate(bad)
        bad['projection_hash'] = digest({key: value for key, value in bad.items() if key != 'projection_hash'})
        with pytest.raises(ValueError):
            validate_change(bad)
