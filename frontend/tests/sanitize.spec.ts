import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

async function render(page: Page, html: string) {
  await page.evaluate((next) => window.setFixture({ mode: 'html', html: next }), html);
  return page.locator('.sec-body');
}

test.beforeEach(async ({ page }) => {
  await page.route('https://fonts.**/*', (route) => route.abort());
  await page.goto('/tests/renderer.html');
  await page.waitForFunction(() => typeof window.setFixture === 'function');
});

test('article markup survives sanitizing', async ({ page }) => {
  const body = await render(page, [
    '<p>See <span class="link" data-target="note-abc123-slug">a wiki link</span> and <em>emphasis</em>.</p>',
    '<ul><li><span class="link" data-target="scope:slug">scoped</span> <span class="fact-date">2026-08-05</span> <code>x = 1</code></li></ul>',
    '<pre><code class="language-py">print(1)</code></pre>',
    '<table><thead><tr><th>A</th></tr></thead><tbody><tr><td colspan="2">B</td></tr></tbody></table>',
    '<p><a href="https://example.com/a?b=1">site</a> <a href="mailto:a@example.com">mail</a> <a href="#frag">frag</a></p>',
  ].join(''));

  await expect(body.locator('span.link[data-target="note-abc123-slug"]')).toHaveText('a wiki link');
  await expect(body.locator('span.link[data-target="scope:slug"]')).toHaveText('scoped');
  await expect(body.locator('span.fact-date')).toHaveText('2026-08-05');
  await expect(body.locator('pre code.language-py')).toHaveText('print(1)');
  await expect(body.locator('td[colspan="2"]')).toHaveText('B');
  await expect(body.locator('a[href="https://example.com/a?b=1"]')).toHaveCount(1);
  await expect(body.locator('a[href="mailto:a@example.com"]')).toHaveCount(1);
  await expect(body.locator('a[href="#frag"]')).toHaveCount(1);
});

test('active content is stripped', async ({ page }) => {
  await page.evaluate(() => { (window as unknown as { pwned: boolean }).pwned = false; });
  const body = await render(page, [
    '<p onclick="window.pwned = true" style="position:fixed" data-other="x">kept text</p>',
    '<img src="x" onerror="window.pwned = true">',
    '<script>window.pwned = true</script>',
    '<iframe srcdoc="<script>parent.pwned = true</script>"></iframe>',
    '<a href="javascript:window.pwned = true">bad link</a>',
    '<a href="data:text/html,x">data link</a>',
    '<form action="/api/x"><input name="q"><button>go</button></form>',
  ].join(''));

  await expect(body.locator('p')).toHaveText('kept text');
  await expect(body.locator('img, script, iframe, form, input, button')).toHaveCount(0);
  await expect(body.locator('[onclick], [style], [data-other]')).toHaveCount(0);
  await expect(body.locator('a[href]')).toHaveCount(0);
  await expect(body.locator('a')).toHaveText(['bad link', 'data link']);
  expect(await page.evaluate(() => (window as unknown as { pwned: boolean }).pwned)).toBe(false);
});
