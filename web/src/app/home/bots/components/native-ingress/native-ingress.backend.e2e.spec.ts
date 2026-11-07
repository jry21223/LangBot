// Actual Quart/BotService/SQLite native APIs. Login, placement and upstream
// lifecycle remain explicit synthetic fixture boundaries; no production data.
import { expect, test, type Page } from '@playwright/test';
import {
  installLangBotApiMocks,
  makeWorkspaceEntry,
} from '../../../../../../tests/e2e/fixtures/langbot-api';
const backend = 'http://127.0.0.1:5300';
const escaped = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
async function scaffold(page: Page, token: string) {
  const workspace = makeWorkspaceEntry(
    'workspace-a',
    'Isolated actual backend',
    'local',
  );
  workspace.workspace.instance_uuid = 'test-workspace-a';
  workspace.membership.account_uuid = 'operator';
  await installLangBotApiMocks(page, {
    authenticated: true,
    workspaces: [workspace],
    storage: { token, userEmail: 'operator@fixture.invalid' },
  });
  // Native product API responses must come from the real candidate backend.
  await page.route('**/api/v1/platform/**', (route) => route.continue());
}
for (const [adapter, width] of [
  ['wecom-archive', 1440],
  ['agent-wechat', 390],
] as const) {
  test(`actual backend ${adapter} official setup, guarded history and revoke at ${width}px`, async ({
    page,
    request,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    const healthResponse = await request.get(`${backend}/__fixture/health`);
    expect(healthResponse.status()).toBe(200);
    const health = await healthResponse.json();
    expect(health.boundary).toBe('synthetic-login-source-lifecycle');
    await scaffold(page, health.token);
    const headers = { Authorization: `Bearer ${health.token}` };
    const descriptorsResponse = await request.get(
      `${backend}/api/v1/platform/adapters`,
      { headers },
    );
    expect(descriptorsResponse.status()).toBe(200);
    const descriptors = (await descriptorsResponse.json()).data.adapters;
    const descriptor = descriptors.find(
      (item: { name: string }) => item.name === adapter,
    );
    expect(descriptor.spec.native_ingress).toBe(true);
    await page.goto('/home/bots?id=new');
    const option = page.getByTestId('adapter-gallery').getByRole('button', {
      name: new RegExp(escaped(descriptor.label.en_US)),
    });
    await expect(option).toBeVisible();
    await expect
      .poll(() =>
        option
          .locator('img')
          .evaluate((image: HTMLImageElement) => image.naturalWidth),
      )
      .toBeGreaterThan(0);
    await option.click();
    await page.locator('input[name="name"]').fill(`Actual backend ${adapter}`);
    const creation = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === '/api/v1/platform/bots',
    );
    await page.getByRole('button', { name: /^Submit$/ }).click();
    const created = await creation;
    expect(created.status()).toBe(200);
    expect(created.request().postDataJSON().adapter_config).toEqual({});
    expect(created.request().postDataJSON().enable).toBe(false);
    const id = (await created.json()).data.uuid;
    await expect(page).toHaveURL(new RegExp(`id=${id}`));
    const status = page.getByTestId('native-ingress-status');
    await expect(
      status.getByText('Configure source settings', { exact: true }),
    ).toBeVisible();
    const configuration: Record<string, unknown> = health.configs[adapter];
    for (const field of descriptor.spec.config) {
      if (!Object.hasOwn(configuration, field.name)) continue;
      const item = page
        .getByText(new RegExp(`^${escaped(field.label.en_US)}\\s*\\*?$`))
        .locator('xpath=ancestor::*[@data-slot="form-item"][1]');
      const value = configuration[field.name];
      if (Array.isArray(value)) {
        for (let index = 0; index < value.length; index++) {
          if ((await item.locator('input').count()) <= index)
            await item.getByRole('button', { name: /^Add$/ }).click();
          await item.locator('input').nth(index).fill(String(value[index]));
        }
      } else await item.locator('input').first().fill(String(value));
    }
    const saving = page.waitForResponse(
      (response) =>
        response.request().method() === 'PUT' &&
        new URL(response.url()).pathname === `/api/v1/platform/bots/${id}`,
    );
    await page.getByRole('button', { name: /^Save$/ }).click();
    const saved = await saving;
    expect(saved.status()).toBe(200);
    expect(saved.request().postDataJSON().enable).toBe(false);
    expect(saved.request().postDataJSON().adapter_config).toEqual(
      configuration,
    );
    const registered = await request.post(
      `${backend}/__fixture/register-seed/${id}`,
    );
    expect(registered.status(), await registered.text()).toBe(200);
    const enabled = page.waitForResponse(
      (response) =>
        response.request().method() === 'PUT' &&
        new URL(response.url()).pathname === `/api/v1/platform/bots/${id}`,
    );
    await page.getByRole('switch', { name: 'Enable', exact: true }).check();
    expect((await enabled).status()).toBe(200);
    await page.getByRole('tab', { name: 'Logs', exact: true }).click();
    await expect(
      status.getByText('Source reachable; upstream not verified'),
    ).toBeVisible({ timeout: 10000 });
    await expect(page.getByTestId('native-ingress-record')).toHaveCount(1);
    await expect(status.getByText('fixture-v1', { exact: true })).toBeVisible();
    const detail = await request.get(`${backend}/api/v1/platform/bots/${id}`, {
      headers,
    });
    const snapshot = (await detail.json()).data.bot.adapter_runtime_values
      .native_ingress;
    expect(snapshot.counts.source_events).toBe(1);
    expect(snapshot.counts.router_accepted).toBe(0);
    expect(snapshot.records[0].stage).toBe('source_accepted');
    expect(
      (
        await request.post(`${backend}/__fixture/upstream-unknown/${id}`)
      ).status(),
    ).toBe(200);
    await expect(status.getByText('Not verified', { exact: true })).toBeVisible(
      { timeout: 10000 },
    );
    await expect(page.getByTestId('native-ingress-record')).toHaveCount(1);
    await expect(status.getByRole('status')).not.toHaveClass(/emerald/);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: test.info().outputPath(`actual-backend-${adapter}-${width}.png`),
      fullPage: true,
    });
    expect(
      (
        await request.post(`${backend}/__fixture/revoke-observer/${id}`)
      ).status(),
    ).toBe(200);
    await expect(
      status.getByText('Authorization required', { exact: true }),
    ).toBeVisible({ timeout: 10000 });
    await expect(page.getByTestId('native-ingress-record')).toHaveCount(0);
    await expect(status).not.toContainText('fixture-v1');
    await expect(status).not.toContainText('No accepted records to display.');
    const denied = await request.post(
      `${backend}/api/v1/platform/bots/${id}/logs`,
      { headers, data: { from_index: -1, max_count: 10 } },
    );
    expect(denied.status()).toBe(403);
    await expect(
      page.getByText('Enable Auto Refresh', { exact: true }),
    ).toHaveCount(0);
    await page.screenshot({
      path: test
        .info()
        .outputPath(`actual-backend-${adapter}-${width}-revoked.png`),
      fullPage: true,
    });
  });
}
