// Synthetic fixtures: no real account, message, credential, or server is used.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  hasNativeIngressCapability,
  parseNativeIngress,
} from './native-ingress.ts';
const synthetic = {
  state: 'connected',
  counts: {
    source_events: 2,
    router_accepted: 1,
    pending_host: 1,
    quarantines: 0,
    owner_gaps: 1,
  },
  records: [
    {
      event_id: 'fixture-event',
      record: 'fixture-record',
      version: 'v2',
      kind: 'created',
      stage: 'router_accepted',
      receipt_id: 'fixture-receipt',
    },
  ],
};
test('capability comes only from trusted adapter spec, never Bot config', () => {
  assert.equal(
    hasNativeIngressCapability({ spec: { native_ingress: true } }),
    true,
  );
  assert.equal(
    hasNativeIngressCapability({ adapter_config: { native_ingress: true } }),
    false,
  );
});
test('missing or future status stays unknown and exposes no stale records', () => {
  assert.deepEqual(parseNativeIngress(undefined), {
    state: 'unknown',
    records: [],
    recordsAvailable: false,
    has_more: false,
  });
  assert.equal(
    parseNativeIngress({ ...synthetic, state: 'running' }).records.length,
    0,
  );
});
test('revoked audience drops even stale counts and identities', () => {
  const result = parseNativeIngress({
    ...synthetic,
    state: 'needs_authorization',
  });
  assert.equal(result.counts, undefined);
  assert.deepEqual(result.records, []);
});
test('Router acceptance is preserved without inventing business success or copying bodies', () => {
  const result = parseNativeIngress({
    ...synthetic,
    secret: 'do-not-copy',
    records: [{ ...synthetic.records[0], content: 'do-not-copy' }],
  });
  assert.equal(result.records[0].stage, 'router_accepted');
  assert.equal(JSON.stringify(result).includes('do-not-copy'), false);
});
test('malformed counts, stages and unbounded records are not rendered', () => {
  assert.equal(
    parseNativeIngress({
      ...synthetic,
      counts: { ...synthetic.counts, source_events: -1 },
    }).counts,
    undefined,
  );
  assert.equal(
    parseNativeIngress({
      ...synthetic,
      records: [{ ...synthetic.records[0], stage: 'succeeded' }],
    }).records.length,
    0,
  );
  assert.equal(
    parseNativeIngress({
      ...synthetic,
      records: Array(40).fill(synthetic.records[0]),
    }).records.length,
    25,
  );
});

test('withheld metadata is distinct from an authorized empty records array', () => {
  assert.equal(
    parseNativeIngress({ state: 'needs_authorization', records: [] })
      .recordsAvailable,
    false,
  );
  assert.equal(parseNativeIngress({ state: 'error' }).recordsAvailable, false);
  assert.equal(
    parseNativeIngress({ state: 'source_available', records: [] })
      .recordsAvailable,
    true,
  );
});
