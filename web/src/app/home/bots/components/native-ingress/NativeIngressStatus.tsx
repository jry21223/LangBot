import { useTranslation } from 'react-i18next';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { parseNativeIngress } from './native-ingress';

export default function NativeIngressStatus({
  value,
  showRecords = false,
}: {
  value: unknown;
  showRecords?: boolean;
}) {
  const { t } = useTranslation();
  const status = parseNativeIngress(value);
  return (
    <Card className="min-w-0" data-testid="native-ingress-status">
      <CardHeader className="gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">
            {t('nativeIngress.title')}
          </CardTitle>
          <Badge status={status.state} role="status" aria-live="polite">
            {t(`nativeIngress.state.${status.state}`)}
          </Badge>
        </div>
        <p className="text-sm leading-relaxed text-muted-foreground">
          {t('nativeIngress.routedBy')}
        </p>
      </CardHeader>
      <CardContent className="min-w-0 space-y-3">
        {status.error_code && (
          <p className="break-all font-mono text-xs text-muted-foreground">
            {status.error_code}
          </p>
        )}
        {status.state === 'needs_configuration' && (
          <p className="text-sm text-muted-foreground">
            {t('nativeIngress.configurationHelp')}
          </p>
        )}
        {status.state === 'needs_authorization' && (
          <p className="text-sm text-muted-foreground">
            {t('nativeIngress.authority')}
          </p>
        )}
        {status.state === 'awaiting_upstream' && (
          <p className="text-sm text-muted-foreground">
            {t('nativeIngress.upstreamHelp')}
          </p>
        )}
        {status.counts && (
          <dl className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
            {(
              Object.keys(status.counts) as Array<keyof typeof status.counts>
            ).map((key) => (
              <div key={key}>
                <dt className="text-xs text-muted-foreground">
                  {t(`nativeIngress.counts.${key}`)}
                </dt>
                <dd className="tabular-nums">{status.counts?.[key]}</dd>
              </div>
            ))}
          </dl>
        )}
        {showRecords && status.recordsAvailable && (
          <div className="space-y-2">
            {status.records.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {t('nativeIngress.empty')}
              </p>
            ) : (
              status.records.map((row) => (
                <div
                  key={row.event_id}
                  className="min-w-0 space-y-1 rounded-md border p-3 text-xs"
                  data-testid="native-ingress-record"
                >
                  <Badge variant="secondary">
                    {t(`nativeIngress.stage.${row.stage}`)}
                  </Badge>
                  <dl className="space-y-1">
                    {row.record && (
                      <div>
                        <dt className="text-muted-foreground">
                          {t('nativeIngress.record')}
                        </dt>
                        <dd className="break-all font-mono select-text">
                          {row.record}
                        </dd>
                      </div>
                    )}
                    {row.version && (
                      <div>
                        <dt className="text-muted-foreground">
                          {t('nativeIngress.version')}
                        </dt>
                        <dd className="break-all font-mono select-text">
                          {row.version}
                        </dd>
                      </div>
                    )}
                    <div>
                      <dt className="text-muted-foreground">
                        {t('nativeIngress.kind')}
                      </dt>
                      <dd className="break-all font-mono">{row.kind}</dd>
                    </div>
                    {row.receipt_id && (
                      <div>
                        <dt className="text-muted-foreground">
                          {t('nativeIngress.receipt')}
                        </dt>
                        <dd className="break-all font-mono select-text">
                          {row.receipt_id}
                        </dd>
                      </div>
                    )}
                  </dl>
                </div>
              ))
            )}
            {status.has_more && (
              <p className="text-xs text-muted-foreground">
                {t('nativeIngress.more')}
              </p>
            )}
          </div>
        )}
        <p className="text-xs leading-relaxed text-muted-foreground">
          {t('nativeIngress.resultBoundary')}
        </p>
      </CardContent>
    </Card>
  );
}
