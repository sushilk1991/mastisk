import { expect, test } from '@playwright/test';

const PROVIDER_ORDER = ['ollama', 'codex', 'claude', 'muse'];
const MODEL = 'deepseek-v4.1-flash:1017-cloud';

// Exercise the real component and its HTTP payloads without changing a live config.
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
        claude_model: 'haiku', muse_model: '', muse_reasoning_effort: 'high',
      },
      budget: { scout: 500, listener: 20, compiler: 100, linter: 50, synthesizer: 10 },
    },
    model_roles: {
      claude_cmd: 'Claude executable.', codex_cmd: 'Read-only Codex executable.',
      muse_cmd: 'Tool-free Muse executable.', embed_model: 'Embedding model.',
      summarize_model_cheap: 'Local cheap model.', summarize_model_heavy: 'Main Ollama model.',
    },
    config_path: '/test/config.toml', cloud_active: false,
  };
}

test('provider settings save and reload the actual controls', async ({ page }) => {
  let bundle = initialBundle();
  const saved: Record<string, unknown>[] = [];
  await page.route('**/api/settings/github', (route) => route.fulfill({ json: { pat_set: false, pat_last4: null } }));
  await page.route('**/api/settings', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ json: bundle });
      return;
    }
    const patch = route.request().postDataJSON();
    saved.push(patch);
    bundle = {
      ...bundle,
      values: {
        ...bundle.values, ...patch,
        intelligence: { ...bundle.values.intelligence, ...patch.intelligence },
      },
    };
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/tests/renderer.html');
  await page.waitForFunction(() => typeof window.setFixture === 'function');
  await page.evaluate(() => window.setFixture({ mode: 'settings' }));

  await expect(page.getByRole('heading', { name: 'Codex', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Muse', exact: true })).toBeVisible();
  await expect(page.getByLabel('Default provider')).toHaveValue('ollama');
  await expect(page.getByLabel('ollama_think')).toHaveValue('high');
  await page.getByLabel('codex_cmd').fill('/custom/codex');
  await page.getByLabel('muse_cmd').fill('/custom/muse');
  await page.getByLabel('intelligence.muse_model').fill('muse-model');
  await page.getByLabel('Default provider').selectOption('muse');
  await page.getByLabel('ollama_think').selectOption('on');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();

  await expect(page.getByRole('button', { name: 'Saved', exact: true })).toBeDisabled();
  expect(saved).toHaveLength(1);
  expect(saved[0]).toMatchObject({
    codex_cmd: '/custom/codex', muse_cmd: '/custom/muse', ollama_think: true,
    intelligence: {
      provider_order: ['muse', 'ollama', 'codex', 'claude'],
      anthropic_auto: false, muse_model: 'muse-model',
    },
  });
  expect(saved[0]).not.toHaveProperty('ollama_cloud_key');
  await expect(page.getByLabel('Default provider')).toHaveValue('muse');
  await expect(page.getByLabel('ollama_think')).toHaveValue('on');
  await expect(page.getByLabel('muse_cmd')).toHaveValue('/custom/muse');
});

test('failed provider save retains the editable draft and shows the error', async ({ page }) => {
  await page.route('**/api/settings/github', (route) => route.fulfill({ json: { pat_set: false, pat_last4: null } }));
  await page.route('**/api/settings', (route) => route.fulfill(
    route.request().method() === 'GET'
      ? { json: initialBundle() }
      : { status: 422, json: { detail: 'Invalid provider configuration' } },
  ));
  await page.goto('/tests/renderer.html');
  await page.waitForFunction(() => typeof window.setFixture === 'function');
  await page.evaluate(() => window.setFixture({ mode: 'settings' }));
  await page.getByLabel('codex_cmd').fill('/custom/codex');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();

  await expect(page.locator('.settings-error')).toBeVisible();
  await expect(page.getByLabel('codex_cmd')).toHaveValue('/custom/codex');
  await expect(page.getByRole('button', { name: 'Save changes', exact: true })).toBeEnabled();
});
