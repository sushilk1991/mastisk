import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const FLOW = 'flowchart LR\n A[Source] --> B[Research] --> C[Compile] --> D[Wiki]';
const XY = 'xychart-beta\n title "Measured throughput"\n x-axis [A, B, C]\n y-axis "Requests" 0 --> 30\n bar [10, 20, 30]';
const PIE = 'pie title Proportions\n "A" : 10\n "B" : 20';

async function fixture(page: Page, value: Record<string, unknown>) {
  await page.evaluate((next) => window.setFixture(next as Parameters<typeof window.setFixture>[0]), value);
}

test.beforeEach(async ({ page }) => {
  // Fonts are optional; the rendering tests must not depend on an external CDN.
  await page.route('https://fonts.**/*', (route) => route.abort());
  await page.goto('/tests/renderer.html');
  await page.waitForFunction(() => typeof window.setFixture === 'function');
});

test('stored Mermaid flowcharts use Pele', async ({ page }) => {
  await fixture(page, { mode: 'diagram', source: FLOW });
  await expect(page.locator('svg.pele-flowchart')).toBeVisible();
  await expect(page.locator('svg.pele')).toContainText('Research');
  await expect(page.locator('.mermaid-error')).toHaveCount(0);
});

for (const source of [XY, PIE]) {
  test(`new ${source.split('\n')[0]} chart uses SVG`, async ({ page }) => {
    await fixture(page, { mode: 'artifact', spec: { renderer: 'pele', source } });
    await expect(page.locator('svg.pele').first()).toBeVisible();
    await expect(page.locator('.artifact-chart-wrap canvas')).toHaveCount(0);
    await expect(page.locator('.artifact-malformed')).toHaveCount(0);
  });
}

test('stored Chart.js configs still draw a canvas', async ({ page }) => {
  await fixture(page, {
    mode: 'artifact', spec: {
      type: 'bar', data: { labels: ['A', 'B'], datasets: [{ label: 'Requests', data: [10, 20] }] },
    },
  });
  const canvas = page.locator('.artifact-chart-wrap canvas');
  await expect(canvas).toBeVisible();
  await expect(page.locator('.artifact-malformed')).toHaveCount(0);
  expect(await canvas.evaluate((el) => el.toDataURL().length)).toBeGreaterThan(100);
});

test('invalid marked charts do not fall back to Chart.js', async ({ page }) => {
  await fixture(page, { mode: 'artifact', spec: { renderer: 'pele', source: '' } });
  await expect(page.locator('.artifact-malformed')).toContainText('source');
  await expect(page.locator('canvas')).toHaveCount(0);
  await fixture(page, { mode: 'artifact', spec: { renderer: 'unknown', source: PIE } });
  await expect(page.locator('.artifact-malformed')).toContainText('renderer');
});

test('parse errors show inert source and recover after correction', async ({ page }) => {
  const invalid = '<img src=x onerror="window.__diagramXss=true">';
  await fixture(page, { mode: 'diagram', source: invalid });
  await expect(page.locator('.mermaid-error')).toHaveText(invalid);
  expect(await page.evaluate(() => '__diagramXss' in window)).toBe(false);
  await expect(page.locator('.mermaid-error img')).toHaveCount(0);
  await fixture(page, { mode: 'diagram', source: FLOW });
  await expect(page.locator('svg.pele-flowchart')).toBeVisible();
  await expect(page.locator('.mermaid-error')).toHaveCount(0);
});

test('diagram links and images cannot trigger external requests', async ({ page }) => {
  const requests: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('diagram-tracker.invalid')) requests.push(request.url());
  });
  await fixture(page, { mode: 'diagram', source:
    'flowchart LR\n A@{ img: "https://diagram-tracker.invalid/image.png", label: "Tracking image", h: 60 } --> B[Output]\n click B href "https://diagram-tracker.invalid/click"',
  });
  await expect(page.locator('svg.pele-flowchart')).toBeVisible();
  await expect(page.locator('.mermaid-block a, .mermaid-block image, .mermaid-block foreignObject, .mermaid-block script')).toHaveCount(0);
  expect(requests).toEqual([]);
});

test('HTML-looking labels remain inert SVG text', async ({ page }) => {
  await fixture(page, { mode: 'diagram', source:
    'flowchart TD\n A["Text <script>window.__diagramXss=true</script>"] --> B[Safe]',
  });
  await expect(page.locator('svg.pele-flowchart')).toBeVisible();
  await expect(page.locator('.mermaid-block script, .mermaid-block foreignObject')).toHaveCount(0);
  expect(await page.evaluate(() => '__diagramXss' in window)).toBe(false);
});

test('zoom supports keyboard and touch-sized controls', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addStyleTag({ content: '.mermaid-block { max-width: 100px; }' });
  await fixture(page, { mode: 'diagram', source:
    'sequenceDiagram\n participant Alice\n participant Bob\n Alice->>Bob: Request\n Bob-->>Alice: Response',
  });
  const diagram = page.locator('.mermaid-block > svg.pele');
  await expect(diagram).toBeVisible();
  const fit = await diagram.getAttribute('viewBox');
  const zoomIn = page.getByRole('button', { name: 'Zoom in', exact: true });
  await expect(zoomIn).toBeVisible();
  const size = await zoomIn.boundingBox();
  expect(size!.width).toBeGreaterThanOrEqual(44);
  expect(size!.height).toBeGreaterThanOrEqual(44);
  await zoomIn.click();
  await expect(diagram).not.toHaveAttribute('viewBox', fit!);
  await diagram.focus();
  await page.keyboard.press('Escape');
  await expect(diagram).toHaveAttribute('viewBox', fit!);
});

test('oversized source produces a readable fallback', async ({ page }) => {
  const source = `flowchart TD\n${'A --> B\n'.repeat(7000)}`;
  await fixture(page, { mode: 'diagram', source });
  await expect(page.locator('.mermaid-error')).toBeVisible();
});

test('resize reflows diagrams without page overflow', async ({ page }, info) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await fixture(page, { mode: 'diagram', source: FLOW });
  const diagram = page.locator('svg.pele-flowchart');
  await expect(diagram).toBeVisible();
  const wide = await diagram.getAttribute('viewBox');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(diagram).not.toHaveAttribute('viewBox', wide!);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await fixture(page, { mode: 'diagram', source: FLOW, theme: 'light' });
  await expect(diagram).toBeVisible();
  await page.screenshot({ path: info.outputPath('mobile-light.png'), fullPage: true });
});

test('theme changes recolor existing SVG without replacing it', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page, { mode: 'diagram', source: FLOW, theme: 'light' });
  const text = page.locator('svg.pele-flowchart text').first();
  await expect(text).toBeVisible();
  const before = await text.evaluate((el) => getComputedStyle(el).fill);
  const original = await page.locator('svg.pele-flowchart').elementHandle();
  await page.evaluate(() => { document.documentElement.dataset.theme = 'dark'; });
  await expect.poll(() => text.evaluate((el) => getComputedStyle(el).fill)).not.toBe(before);
  expect(await original!.evaluate((el) => el.isConnected)).toBe(true);
  await fixture(page, { mode: 'diagram', source: FLOW, theme: 'dark' });
  await expect(text).toBeVisible();
  await page.screenshot({ path: info.outputPath('mobile-dark.png'), fullPage: true });
});

test('unmount disconnects resize observers', async ({ page }) => {
  await page.evaluate(() => {
    const Original = window.ResizeObserver;
    let observers = 0;
    Object.defineProperty(window, '__diagramObservers', { get: () => observers });
    window.ResizeObserver = class extends Original {
      private watching = false;
      observe(target: Element, options?: ResizeObserverOptions) {
        if (!this.watching) { observers += 1; this.watching = true; }
        super.observe(target, options);
      }
      disconnect() {
        if (this.watching) { observers -= 1; this.watching = false; }
        super.disconnect();
      }
    };
  });
  await fixture(page, { mode: 'diagram', source: FLOW });
  await expect(page.locator('svg.pele-flowchart')).toBeVisible();
  await expect.poll(() => page.evaluate(() => Reflect.get(window, '__diagramObservers'))).toBeGreaterThan(0);
  await fixture(page, { mode: 'empty' });
  await expect.poll(() => page.evaluate(() => Reflect.get(window, '__diagramObservers'))).toBe(0);
});
