export type NativeState =
  | 'source_available'
  | 'awaiting_upstream'
  | 'disabled'
  | 'needs_configuration'
  | 'needs_authorization'
  | 'connecting'
  | 'connected'
  | 'disconnected'
  | 'error'
  | 'unknown';
export type NativeStage = 'source_accepted' | 'router_accepted' | 'quarantined';
export interface NativeRecord {
  event_id: string;
  record?: string;
  version?: string;
  kind: string;
  stage: NativeStage;
  receipt_id?: string;
}
export interface NativeCounts {
  source_events: number;
  router_accepted: number;
  pending_host: number;
  quarantines: number;
  owner_gaps: number;
}
export interface NativeSnapshot {
  state: NativeState;
  error_code?: string;
  counts?: NativeCounts;
  records: NativeRecord[];
  recordsAvailable: boolean;
  has_more: boolean;
}
const states = new Set<NativeState>([
  'source_available',
  'awaiting_upstream',
  'disabled',
  'needs_configuration',
  'needs_authorization',
  'connecting',
  'connected',
  'disconnected',
  'error',
  'unknown',
]);
const stages = new Set<NativeStage>([
  'source_accepted',
  'router_accepted',
  'quarantined',
]);
const object = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
const identifier = (value: unknown): value is string =>
  typeof value === 'string' &&
  value.length > 0 &&
  value.length <= 512 &&
  !/[\x00-\x1f\x7f]/.test(value);
export function hasNativeIngressCapability(adapter: unknown): boolean {
  return object(object(adapter)?.spec)?.native_ingress === true;
}
export function parseNativeIngress(value: unknown): NativeSnapshot {
  const input = object(value);
  const state: NativeState =
    input && states.has(input.state as NativeState)
      ? (input.state as NativeState)
      : 'unknown';
  const snapshot: NativeSnapshot = {
    state,
    records: [],
    recordsAvailable: false,
    has_more: false,
  };
  if (
    typeof input?.error_code === 'string' &&
    /^[A-Za-z0-9_.:-]{1,100}$/.test(input.error_code)
  )
    snapshot.error_code = input.error_code;
  // Never retain stale record identities when the current audience is missing.
  if (
    !input ||
    state === 'needs_configuration' ||
    state === 'needs_authorization' ||
    state === 'unknown'
  )
    return snapshot;
  const counts = object(input.counts);
  const names = [
    'source_events',
    'router_accepted',
    'pending_host',
    'quarantines',
    'owner_gaps',
  ] as const;
  if (
    counts &&
    names.every(
      (key) =>
        Number.isSafeInteger(counts[key]) && (counts[key] as number) >= 0,
    )
  ) {
    snapshot.counts = Object.fromEntries(
      names.map((key) => [key, counts[key]]),
    ) as unknown as NativeCounts;
  }
  if (Array.isArray(input.records)) {
    snapshot.recordsAvailable = true;
    for (const candidate of input.records.slice(0, 25)) {
      const row = object(candidate);
      if (
        !row ||
        !identifier(row.event_id) ||
        !identifier(row.kind) ||
        !stages.has(row.stage as NativeStage)
      )
        continue;
      const record: NativeRecord = {
        event_id: row.event_id,
        kind: row.kind,
        stage: row.stage as NativeStage,
      };
      if (identifier(row.record)) record.record = row.record;
      if (identifier(row.version)) record.version = row.version;
      if (identifier(row.receipt_id)) record.receipt_id = row.receipt_id;
      snapshot.records.push(record);
    }
  }
  snapshot.has_more = input.has_more === true;
  return snapshot;
}
