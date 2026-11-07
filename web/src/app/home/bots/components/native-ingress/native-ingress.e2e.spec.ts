// Synthetic API fixture only. This is UI verification, not a live source test.
import { expect, test, type Page, type Route } from '@playwright/test';
import { installLangBotApiMocks } from '../../../../../../tests/e2e/fixtures/langbot-api';
const fixtureId = 'native-fixture';
const counts = {
  source_events: 2,
  router_accepted: 1,
  pending_host: 1,
  quarantines: 0,
  owner_gaps: 1,
};
const records = [
  {
    event_id: 'fixture-event-one',
    record: 'fixture-native-record-' + 'x'.repeat(100),
    version: 'fixture-v2',
    kind: 'attachments_changed',
    stage: 'router_accepted',
    receipt_id: 'fixture-receipt',
  },
  {
    event_id: 'fixture-event-two',
    record: 'fixture-pending-record',
    version: 'fixture-v1',
    kind: 'created',
    stage: 'source_accepted',
  },
];
const fields = [
  ['source', 'Source ID'],
  ['account', 'Owner Corporation ID'],
  ['scope', 'Authorized Conversation Scope'],
  ['grant_revision', 'Registered Grant Revision'],
  ['credential_ref', 'Service Credential Reference'],
];
async function json(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ code: 0, message: 'ok', data }),
  });
}
function fixtureBot(runtime: unknown) {
  return {
    uuid: fixtureId,
    name: 'Synthetic Native Source',
    description: 'Synthetic only',
    enable: true,
    adapter: 'synthetic-native',
    adapter_config: {
      source: 'fixture-source',
      account: 'fixture-corp',
      scope: 'fixture-chat',
      grant_revision: 'fixture-grant',
      credential_ref: 'fixture-reader',
    },
    event_bindings: [],
    plugin_processors: [],
    adapter_runtime_values: { native_ingress: runtime },
  };
}
async function setup(page: Page, language = 'en-US') {
  await installLangBotApiMocks(page, { authenticated: true, language });
  const runtime = {
    state: 'source_available',
    counts,
    records,
    has_more: false,
  };
  await page.route('**/api/v1/platform/adapters', (route) =>
    json(route, {
      adapters: [
        {
          name: 'synthetic-native',
          label: { en_US: 'Synthetic Native Source', zh_Hans: '合成原生来源' },
          description: { en_US: 'Synthetic native channel fixture' },
          spec: {
            native_ingress: true,
            categories: ['testing'],
            supported_events: ['message.received'],
            supported_apis: [],
            config: fields.map(([name, label]) => ({
              name,
              label: { en_US: label },
              type: 'string',
              required: true,
              default: '',
            })),
          },
        },
        {
          name: 'synthetic-ordinary',
          label: { en_US: 'Synthetic Ordinary Adapter' },
          description: { en_US: 'Synthetic ordinary fixture' },
          spec: {
            native_ingress: false,
            categories: ['testing'],
            config: [],
            supported_events: ['message.received'],
          },
        },
      ],
    }),
  );
  await page.route(`**/api/v1/platform/bots/${fixtureId}`, (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    return json(route, {
      bot: fixtureBot(runtime),
    });
  });
  return runtime;
}
for (const width of [1440, 390]) {
  test(`native source uses existing details and logs at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    const runtime = await setup(page);
    await page.goto(`/home/bots?id=${fixtureId}`);
    const status = page.getByTestId('native-ingress-status');
    await expect(
      status.getByText('Source reachable; upstream not verified'),
    ).toBeVisible();
    await expect(status.getByRole('status')).not.toHaveClass(/emerald/);
    await expect(page.locator('[data-guide="bot-routing"]')).toHaveCount(0);
    await expect(
      page.getByRole('button', { name: 'Test listener' }),
    ).toHaveCount(0);
    await page.getByRole('tab', { name: 'Logs', exact: true }).click();
    await expect(page.getByTestId('native-ingress-record')).toHaveCount(2);
    await expect(status.getByText('fixture-v2', { exact: true })).toBeVisible();
    await expect(
      status.getByText('Router accepted', { exact: true }),
    ).toHaveCount(2);
    await expect(status).not.toContainText('succeeded');
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: test.info().outputPath(`native-${width}.png`),
      fullPage: true,
    });
    runtime.state = 'needs_authorization';
    await expect(status.getByText('Authorization required')).toBeVisible({
      timeout: 10000,
    });
    await expect(page.getByTestId('native-ingress-record')).toHaveCount(0);
    await expect(status).not.toContainText('fixture-v2');
    await expect(status).not.toContainText('Awaiting Router');
    await page.screenshot({
      path: test.info().outputPath(`native-${width}-revoked.png`),
      fullPage: true,
    });
  });
}
test('native channel can be created using only the existing form', async ({
  page,
}) => {
  await setup(page);
  await page.goto('/home/bots?id=new');
  await page
    .getByTestId('adapter-gallery')
    .getByRole('button', { name: /Synthetic Native Source/ })
    .click();
  await page.locator('input[name="name"]').fill('Synthetic New Source');
  const createRequest = page.waitForRequest(
    (request) =>
      request.method() === 'POST' &&
      new URL(request.url()).pathname === '/api/v1/platform/bots',
  );
  await page.getByRole('button', { name: /^Submit$/ }).click();
  const draft = (await createRequest).postDataJSON();
  expect(draft.enable).toBe(false);
  expect(draft.adapter_config).toEqual({});
  await expect(page).toHaveURL(/\/home\/bots\?id=bot-1$/);
  for (const [name, label] of fields)
    await page
      .getByText(new RegExp(`^${label}\\s*\\*$`))
      .locator('..')
      .getByRole('textbox')
      .fill(`fixture-${name}`);
  const savedRequest = page.waitForRequest(
    (request) =>
      request.method() === 'PUT' &&
      new URL(request.url()).pathname === '/api/v1/platform/bots/bot-1',
  );
  await page.getByRole('button', { name: /^Save$/ }).click();
  const saved = (await savedRequest).postDataJSON();
  expect(saved.enable).toBe(false);
  expect(saved.adapter_config.credential_ref).toBe('fixture-credential_ref');
  await expect(page.locator('[data-guide="bot-routing"]')).toHaveCount(0);
  await expect(page.getByTestId('native-ingress-status')).toBeVisible();
});

test('an earlier success cannot restore records after account audience changes', async ({
  page,
}) => {
  const runtime = await setup(page);
  await page.goto(`/home/bots?id=${fixtureId}&tab=logs`);
  await expect(page.getByTestId('native-ingress-record')).toHaveCount(2);
  let delayed: Route | undefined;
  let holdNext = true;
  await page.route(`**/api/v1/platform/bots/${fixtureId}`, (route) => {
    if (route.request().method() === 'GET' && holdNext) {
      holdNext = false;
      delayed = route;
      return;
    }
    return route.fallback();
  });
  await expect.poll(() => Boolean(delayed), { timeout: 10000 }).toBe(true);
  const oldRuntime = structuredClone(runtime);
  runtime.state = 'needs_authorization';
  // Synthetic account transition through the application's real subscription store.
  await page.evaluate(async () => {
    const modulePath = '/src/app/infra/http/currentWorkspaceStore.ts';
    const store = await import(/* @vite-ignore */ modulePath);
    const current = store.getCurrentWorkspaceSnapshot();
    store.setCurrentWorkspaceSnapshot({
      ...current,
      membership: {
        ...current.membership,
        account_uuid: 'synthetic-next-account',
      },
    });
  });
  await expect(page.getByTestId('native-ingress-record')).toHaveCount(0);
  await expect(
    page.getByText('Authorization required', { exact: true }),
  ).toBeVisible();
  await json(delayed!, { bot: fixtureBot(oldRuntime) });
  await expect(page.getByTestId('native-ingress-status')).not.toContainText(
    'fixture-v2',
  );
  await expect(page.getByTestId('native-ingress-record')).toHaveCount(0);
});

test('SPA Bot switch does not retain native capability or records on an ordinary Bot', async ({
  page,
}) => {
  const runtime = await setup(page);
  const ordinary = {
    ...fixtureBot(undefined),
    uuid: 'ordinary-fixture',
    name: 'Other Ordinary Source',
    adapter: 'synthetic-ordinary',
    adapter_config: {},
    adapter_runtime_values: {},
  };
  await page.route('**/api/v1/platform/bots', (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    return json(route, { bots: [fixtureBot(runtime), ordinary] });
  });
  await page.route('**/api/v1/platform/bots/ordinary-fixture', (route) =>
    json(route, { bot: ordinary }),
  );
  await page.goto(`/home/bots?id=${fixtureId}&tab=logs`);
  await expect(page.getByTestId('native-ingress-record')).toHaveCount(2);
  await page
    .getByText('Other Ordinary Source', { exact: true })
    .first()
    .click();
  await expect(page).toHaveURL(/id=ordinary-fixture/);
  await expect(page.getByTestId('native-ingress-record')).toHaveCount(0);
  await expect(page.getByTestId('native-ingress-status')).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: 'Test listener' }),
  ).toBeVisible();
  await expect(page.locator('[data-guide="bot-routing"]')).toHaveCount(1);
});
