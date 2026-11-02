import { expect, test, type Page } from '@playwright/test';

const PROVIDER_ORDER = ['ollama', 'codex', 'claude', 'muse'];
const MODEL = 'deepseek-v4.1-flash:1017-cloud';
const MIN_TOUCH_TARGET_PX = 44;

// Test only the actual component/HTTP contract; never mutate the daemon's configuration.
function initialBundle() {
  return {
    values: {
      claude_cmd: 'claude', codex_cmd: 'codex', muse_cmd: 'muse',
      ollama_local_url: 'http://localhost:11434', ollama_local_only: false,
      ollama_cloud_url: 'https://ollama.com', ollama_cloud_key_set: false,
      embed_model: 'nomic-embed-text', summarize_model_cheap: 'qwen3.5:4b',
      summarize_model_heavy: MODEL, ollama_think: 'high',
      intelligence: {
        provider_order: [...PROVIDER_ORDER], anthropic_auto: false,
        codex_model: 'codex-model', codex_reasoning_effort: 'low',
        claude_model: 'haiku', claude_reasoning_effort: '',
        muse_model: '', muse_reasoning_effort: 'high', anthropic_model: 'api-model',
      },
      budget: { scout: 500, listener: 20, compiler: 100, linter: 50, synthesizer: 10 },
    },
    model_roles: {}, config_path: '/test/config.toml', cloud_active: false,
    cli_detection: { codex: { path: '/detected/codex' }, claude: { path: '/detected/claude' }, muse: { path: '/detected/muse' } },
    effective_order: [...PROVIDER_ORDER],
  };
}

function catalog(provider: string) {
  const row = (id: string, thinking: string[] | null, defaultThinking: string | null = null, kind = 'chat') =>
    ({ id, name: id, thinking, default_thinking: defaultThinking, kind });
  const models: Record<string, ReturnType<typeof row>[]> = {
    ollama: [row(MODEL, ['off', 'low', 'high', 'max'], 'high'), row('plain-model', ['off'], 'off'), row('nomic-embed-text', ['off'], 'off', 'embedding')],
    codex: [row('codex-model', ['low', 'high'], 'low'), row('codex-strong', ['high', 'xhigh'], 'high')],
    claude: [row('haiku', []), row('sonnet', ['low', 'high'], 'high')],
    muse: [row('native-muse', ['low', 'high'], 'high')],
    anthropic: [row('api-model', null)],
  };
  return { provider, models: models[provider], status: 'ready', source: 'native-cli', stale: false, message: null, checked_at: '2026-11-02T05:30:00Z' };
}

async function fixture(page: Page, options: { failSave?: boolean; missingModel?: boolean; dark?: boolean; autoPriority?: boolean } = {}) {
  let bundle = initialBundle();
  if (options.missingModel) {
    bundle.values.summarize_model_heavy = 'saved-custom-cloud';
  }
  if (options.autoPriority) {
    bundle.values.intelligence.anthropic_auto = true;
    bundle.effective_order = ['anthropic', ...PROVIDER_ORDER];
  }
  const saved: Record<string, unknown>[] = [];
  await page.route('**/api/settings/github', (route) => route.fulfill({ json: { pat_set: false, pat_last4: null } }));
  await page.route('**/api/settings/models/*', (route) => {
    const provider = new URL(route.request().url()).pathname.split('/').pop()!;
    return route.fulfill({ json: catalog(provider) });
  });
  await page.route('**/api/settings', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ json: bundle });
      return;
    }
    if (options.failSave) {
      await route.fulfill({ status: 422, json: { detail: 'Invalid provider configuration' } });
      return;
    }
    const patch = route.request().postDataJSON();
    saved.push(patch);
    bundle = { ...bundle, values: { ...bundle.values, ...patch,
      intelligence: { ...bundle.values.intelligence, ...patch.intelligence } } };
    bundle.effective_order = [...bundle.values.intelligence.provider_order];
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/tests/renderer.html');
  await page.waitForFunction(() => typeof window.setFixture === 'function');
  await page.evaluate((dark) => window.setFixture({ mode: 'settings', theme: dark ? 'dark' : 'light' }), Boolean(options.dark));
  return saved;
}

test('one card offers native models and saves only edited provider controls', async ({ page }) => {
  const saved = await fixture(page);
  await expect(page.getByRole('heading', { name: 'AI model', exact: true })).toBeVisible();
  await expect(page.getByLabel('Provider', { exact: true })).toHaveValue('ollama');
  await expect(page.getByLabel('Model', { exact: true })).toHaveValue(MODEL);
  await expect(page.getByLabel('Thinking', { exact: true })).toHaveValue('high');
  await expect(page.getByLabel('Thinking', { exact: true }).locator('option[value="medium"]')).toHaveCount(0);
  await expect(page.getByLabel('Model', { exact: true }).locator('option[value="nomic-embed-text"]')).toHaveCount(0);

  await page.getByLabel('Provider', { exact: true }).selectOption('codex');
  await expect(page.getByLabel('Model', { exact: true }).locator('option[value="codex-strong"]')).toHaveCount(1);
  await page.getByLabel('Model', { exact: true }).selectOption('codex-strong');
  await expect(page.getByLabel('Thinking', { exact: true })).toHaveValue('high');
  await page.getByLabel('Provider', { exact: true }).selectOption('muse');
  await expect(page.getByLabel('Model', { exact: true }).locator('option[value="native-muse"]')).toHaveCount(1);
  await page.getByLabel('Model', { exact: true }).selectOption('native-muse');
  await page.getByLabel('Thinking', { exact: true }).selectOption('low');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();

  await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeDisabled();
  expect(saved).toHaveLength(1);
  expect(saved[0]).toMatchObject({ intelligence: {
    provider_order: ['muse', 'ollama', 'codex', 'claude'], anthropic_auto: false,
    codex_model: 'codex-strong', codex_reasoning_effort: 'high',
    muse_model: 'native-muse', muse_reasoning_effort: 'low',
  } });
  for (const key of ['codex_cmd', 'muse_cmd', 'claude_cmd', 'ollama_cloud_key', 'summarize_model_heavy', 'ollama_think']) {
    expect(saved[0]).not.toHaveProperty(key);
  }
});

test('technical overrides are hidden and labelled in plain language', async ({ page }) => {
  await fixture(page);
  await expect(page.getByLabel('Codex executable override')).not.toBeVisible();
  await expect(page.getByText('intelligence.anthropic_auto', { exact: true })).toHaveCount(0);
  await expect(page.getByText('summarize_model_heavy', { exact: true })).toHaveCount(0);
  await page.getByText('Advanced', { exact: true }).click();
  await expect(page.getByLabel('Codex executable override')).toBeVisible();
  await expect(page.getByLabel('Codex executable override')).toHaveValue('');
  await expect(page.getByText(/Detected: \/detected\/codex/)).toBeVisible();
});

test('failed save retains the model draft and shows the error', async ({ page }) => {
  await fixture(page, { failSave: true });
  await expect(page.getByLabel('Model', { exact: true }).locator('option[value="plain-model"]')).toHaveCount(1);
  await page.getByLabel('Model', { exact: true }).selectOption('plain-model');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.locator('.settings-error')).toBeVisible();
  await expect(page.getByLabel('Model', { exact: true })).toHaveValue('plain-model');
  await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toBeEnabled();
});

test('refresh failure keeps last known choices without changing settings', async ({ page }) => {
  const saved = await fixture(page);
  await expect(page.getByLabel('Thinking', { exact: true })).toHaveValue('high');
  await page.route('**/api/settings/models/*', (route) => route.fulfill({ status: 503, json: { detail: 'Temporary catalog failure' } }));
  await page.getByRole('button', { name: 'Refresh models' }).click();
  await expect(page.getByRole('status')).toContainText(/could not|couldn’t|unavailable/i);
  await expect(page.getByLabel('Model', { exact: true })).toHaveValue(MODEL);
  await expect(page.getByLabel('Thinking', { exact: true })).toHaveValue('high');
  await expect(page.getByLabel('Model', { exact: true }).locator('option[value="plain-model"]')).toHaveCount(1);
  expect(saved).toHaveLength(0);
});

test('a missing saved model stays selected and is not silently replaced', async ({ page }) => {
  await fixture(page, { missingModel: true });
  await expect(page.getByLabel('Model', { exact: true })).toHaveValue('saved-custom-cloud');
  await expect(page.getByRole('status')).toContainText(/saved model.*not.*list/i);
  await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toBeDisabled();
});

test('automatic API priority is visible and choosing a provider disables it', async ({ page }) => {
  const saved = await fixture(page, { autoPriority: true });
  await expect(page.getByText(/Anthropic API currently takes priority/)).toBeVisible();
  await page.getByRole('button', { name: 'Use my selected provider' }).click();
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeDisabled();
  expect(saved[0]).toMatchObject({ intelligence: { anthropic_auto: false } });
});

test('late provider responses cannot overwrite the selected provider catalog', async ({ page }) => {
  await fixture(page);
  await page.route('**/api/settings/models/*', async (route) => {
    const provider = new URL(route.request().url()).pathname.split('/').pop()!;
    if (provider === 'codex') {
      await new Promise((resolve) => setTimeout(resolve, 600));
    }
    await route.fulfill({ json: catalog(provider) });
  });
  await page.getByLabel('Provider', { exact: true }).selectOption('codex');
  await page.getByLabel('Provider', { exact: true }).selectOption('muse');
  await expect(page.getByLabel('Model', { exact: true }).locator('option[value="native-muse"]')).toHaveCount(1);
  await page.waitForTimeout(750);
  await expect(page.getByLabel('Model', { exact: true }).locator('option[value="native-muse"]')).toHaveCount(1);
  await expect(page.getByLabel('Model', { exact: true }).locator('option[value="codex-strong"]')).toHaveCount(0);
});

test('desktop light settings supports keyboard navigation through the primary controls', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await fixture(page);
  await expect(page.getByLabel('Thinking', { exact: true })).toHaveValue('high');
  await page.getByLabel('Provider', { exact: true }).focus();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Model', { exact: true })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Thinking', { exact: true })).toBeFocused();
  await page.screenshot({ path: 'test-results/settings-desktop-light.png', fullPage: true });
});

test('mobile dark settings keeps the primary controls within the viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page, { dark: true });
  await expect(page.getByLabel('Thinking', { exact: true })).toHaveValue('high');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.getByText('Advanced', { exact: true })).toBeVisible();
  const route = await page.locator('.settings-route').boundingBox();
  const footer = await page.locator('.settings-footer').boundingBox();
  expect(route).not.toBeNull();
  expect(footer).not.toBeNull();
  expect(footer!.y).toBeGreaterThanOrEqual(route!.y + route!.height);
  const save = await page.getByRole('button', { name: 'Save changes', exact: true }).boundingBox();
  expect(save!.height).toBeGreaterThanOrEqual(MIN_TOUCH_TARGET_PX);
  await page.screenshot({ path: 'test-results/settings-mobile-dark.png', fullPage: true });
});
