import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ArtifactCard } from '../src/components/ArtifactCard';
import { MermaidBlock } from '../src/components/MermaidBlock';
import { SafeHtml } from '../src/components/SafeHtml';
import { SettingsView } from '../src/components/SettingsView';
import '../src/styles/mastisk.css';

interface Fixture {
  mode: 'diagram' | 'artifact' | 'settings' | 'html' | 'empty';
  source?: string;
  html?: string;
  spec?: Record<string, unknown>;
  theme?: 'light' | 'dark';
}

declare global {
  interface Window {
    setFixture: (fixture: Fixture) => void;
  }
}

// Only the test entry point exposes fixtures; no live API or vault is used.
const root = createRoot(document.getElementById('root')!);
const style = document.createElement('style');
style.textContent = `
  body { overflow: auto; font-size: 16px; }
  #root { height: auto; padding: 16px; max-width: 960px; margin: auto; }
  #settings { padding: 8px 16px; font: 12px monospace; color: var(--fg); }
`;
document.head.append(style);

window.setFixture = (fixture) => {
  document.documentElement.dataset.theme = fixture.theme ?? 'light';
  document.getElementById('settings')!.textContent =
    `Chromium · ${window.innerWidth}×${window.innerHeight} · ${fixture.theme ?? 'light'} · text 16px`;
  root.render(
    <StrictMode>
      {fixture.mode === 'settings' && <SettingsView />}
      {fixture.mode === 'html' && <SafeHtml className="sec-body" html={fixture.html}/>}
      {fixture.mode === 'diagram' && <MermaidBlock source={fixture.source ?? ''} />}
      {fixture.mode === 'artifact' && <ArtifactCard
        artifact={{
          id: 1,
          article_id: 'test-fixture',
          kind: 'chart',
          title: 'Measured throughput',
          description: 'Fixed test data, not a live wiki article.',
          spec: fixture.spec ?? {},
          created_by: 'test',
          created_at: '2026-10-18T00:00:00Z',
          updated_at: '2026-10-18T00:00:00Z',
        }}
        onUpdated={() => {}}
        onDeleted={() => {}}
      />}
    </StrictMode>,
  );
};

window.setFixture({ mode: 'empty' });
