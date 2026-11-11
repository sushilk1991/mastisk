import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { IntelligenceProvider, IntelligenceValues, ModelCatalog, SettingsBundle, SettingsPatch } from '../types';

// Provider names and thinking levels are explicit so saved values match the runtime API.
const PROVIDERS: IntelligenceProvider[] = ['ollama', 'codex', 'claude', 'muse', 'anthropic'];
const PROVIDER_NAMES: Record<IntelligenceProvider, string> = {
  ollama: 'Ollama', codex: 'Codex', claude: 'Claude', muse: 'Muse', anthropic: 'Anthropic API',
};
const THINKING_LEVELS = ['low', 'medium', 'high', 'max'] as const;
// Provider-specific storage stays behind one form; switching services retains each model pin.
const MODEL_KEYS = { ollama: null, codex: 'codex_model', claude: 'claude_model', muse: 'muse_model', anthropic: 'anthropic_model' } as const;
const EFFORT_KEYS = { ollama: null, codex: 'codex_reasoning_effort', claude: 'claude_reasoning_effort', muse: 'muse_reasoning_effort', anthropic: null } as const;
const CLI_PROVIDERS = ['claude', 'codex', 'muse'] as const;

type SaveState = 'idle' | 'saving' | 'saved' | 'error';

type GithubState = { pat_set: boolean; pat_last4: string | null } | null;
type GhMsg = { kind: 'ok' | 'err'; text: string } | null;

export function SettingsView() {
  const [bundle, setBundle] = useState<SettingsBundle | null>(null);
  const [draft, setDraft] = useState<SettingsPatch>({});
  const [keyInput, setKeyInput] = useState('');
  const [save, setSave] = useState<SaveState>('idle');
  const [err, setErr] = useState<string | null>(null);
  const [catalogs, setCatalogs] = useState<Partial<Record<IntelligenceProvider, ModelCatalog>>>({});
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [modelNotice, setModelNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const lastRefresh = useRef(0);
  const intelligence = bundle ? { ...bundle.values.intelligence, ...draft.intelligence } : null;
  const provider = intelligence?.provider_order[0] ?? null;
  const modelKey = provider ? MODEL_KEYS[provider] : null;
  const model = provider === 'ollama'
    ? draft.summarize_model_heavy ?? bundle?.values.summarize_model_heavy ?? ''
    : (modelKey ? intelligence?.[modelKey] : '') ?? '';

  const [githubState, setGithubState] = useState<GithubState>(null);
  const [newPat, setNewPat] = useState('');
  const [ghSaveBusy, setGhSaveBusy] = useState(false);
  const [ghTestBusy, setGhTestBusy] = useState(false);
  const [ghMsg, setGhMsg] = useState<GhMsg>(null);

  useEffect(() => {
    void api.settings().then((b) => { setBundle(b); setDraft({}); setKeyInput(''); }).catch((e) => setErr(String(e)));
    void api.github.get()
      .then((g) => setGithubState({ pat_set: g.pat_set, pat_last4: g.pat_last4 }))
      .catch(() => setGithubState({ pat_set: false, pat_last4: null }));
  }, []);

  // Selection -> API metadata -> this provider's picker. Ignore obsolete responses on switches.
  useEffect(() => {
    if (!provider) {
      return;
    }
    let current = true;
    const force = refresh !== lastRefresh.current;
    lastRefresh.current = refresh;
    setCatalogBusy(true);
    setCatalogError(null);
    void api.modelCatalog(provider, model, force).then((result) => {
      if (!current) {
        return;
      }
      setCatalogs((previous) => {
        const old = previous[provider];
        const models = result.models.length || !old ? result.models : old.models;
        return { ...previous, [provider]: { ...result, models, stale: result.stale || (models !== result.models) } };
      });
    }).catch(() => {
      if (current) {
        setCatalogError('Could not refresh models. Your saved choice and last known list are unchanged.');
      }
    }).finally(() => {
      if (current) {
        setCatalogBusy(false);
      }
    });
    return () => { current = false; };
  }, [provider, model, refresh, bundle?.values.claude_cmd, bundle?.values.codex_cmd, bundle?.values.muse_cmd,
    bundle?.values.ollama_local_url, bundle?.values.ollama_cloud_url, bundle?.values.ollama_local_only, bundle?.values.ollama_cloud_key_set]);

  if (!bundle || !intelligence || !provider) {
    return (
      <div className="view">
        {err ? <p style={{color:'var(--danger)'}}>{err}</p> : <p style={{color:'var(--fg-faint)'}}>loading…</p>}
      </div>
    );
  }

  const v = bundle.values;
  const cur = <K extends keyof typeof v>(k: K) => (draft as Record<string, unknown>)[k as string] ?? v[k];
  const set = (patch: SettingsPatch) => { setDraft((d) => ({ ...d, ...patch })); setSave('idle'); setErr(null); };
  // Send only edited nested fields; unrelated pins and future config keys stay untouched.
  const setIntelligence = (patch: Partial<IntelligenceValues>) => {
    set({ intelligence: { ...draft.intelligence, ...patch } });
  };
  const setDefault = (value: string) => {
    const provider = PROVIDERS.find((item) => item === value);
    if (!provider) {
      return;
    }
    // Previewing several providers must not reorder the saved fallbacks as a side effect.
    setModelNotice(null);
    setIntelligence({
      provider_order: [provider, ...v.intelligence.provider_order.filter((item) => item !== provider)],
      anthropic_auto: false,
    });
  };
  const catalog = catalogs[provider];
  const choices = catalog?.models.filter((choice) => choice.kind !== 'embedding') ?? [];
  const selected = choices.find((choice) => choice.id === model);
  const effortKey = EFFORT_KEYS[provider];
  const thinking = provider === 'ollama' ? draft.ollama_think ?? v.ollama_think : (effortKey ? intelligence[effortKey] : '') ?? '';
  const thinkingValue = typeof thinking === 'boolean' ? (thinking ? 'on' : 'off') : thinking;
  const thinkingLevels = selected?.thinking?.filter((level) => provider !== 'ollama'
    || ['off', 'on', ...THINKING_LEVELS].includes(level)) ?? [];
  const changeThinking = (value: string) => {
    if (effortKey) {
      setIntelligence({ [effortKey]: value });
      return;
    }
    if (value === 'on' || value === 'off') {
      set({ ollama_think: value === 'on' });
      return;
    }
    const level = THINKING_LEVELS.find((item) => item === value);
    if (level) {
      set({ ollama_think: level });
    }
  };
  // A deliberate model change adjusts only this provider's incompatible effort, never another pin.
  const changeModel = (value: string) => {
    const next = choices.find((choice) => choice.id === value);
    const levels = next?.thinking;
    const compatible = levels == null || levels.includes(thinkingValue) || (!thinkingValue && provider !== 'ollama');
    const nextEffort = next?.default_thinking && levels?.includes(next.default_thinking)
      ? next.default_thinking : provider === 'ollama' ? 'off' : '';
    setModelNotice(!compatible ? `Thinking set to ${thinkingLabel(nextEffort || 'CLI default')} for this model.` : null);
    if (modelKey) {
      setIntelligence({ [modelKey]: value, ...(effortKey && (!compatible || !value) ? { [effortKey]: value ? nextEffort : '' } : {}) });
      return;
    }
    const patch: SettingsPatch = { summarize_model_heavy: value };
    if (!compatible) {
      const level = THINKING_LEVELS.find((item) => item === nextEffort);
      patch.ollama_think = level ?? (nextEffort === 'on');
    }
    set(patch);
  };
  const modelOptions = choices.map((choice) => ({ value: choice.id, label: choice.name }));
  if (modelKey && provider !== 'anthropic') {
    modelOptions.unshift({ value: '', label: 'Use CLI default' });
  }
  if (model && !selected) {
    modelOptions.unshift({ value: model, label: `${model} (saved)` });
  }
  const thinkingOptions = thinkingLevels.map((value) => ({ value, label: thinkingLabel(value) }));
  if (effortKey) {
    thinkingOptions.unshift({ value: '', label: 'Use CLI default' });
  }
  if (thinkingValue && !thinkingOptions.some((option) => option.value === thinkingValue)) {
    thinkingOptions.unshift({ value: thinkingValue, label: `${thinkingLabel(thinkingValue)} (saved)` });
  }

  const setBudget = (k: keyof typeof v.budget, n: number) => {
    const base = draft.budget ?? { ...v.budget };
    set({ budget: { ...base, [k]: n } });
  };

  const submit = async () => {
    setSave('saving');
    try {
      const patch: SettingsPatch = { ...draft };
      // Only send the cloud key if the user actually typed one (including "" to clear).
      if (keyInput !== '') patch.ollama_cloud_key = keyInput;
      await api.saveSettings(patch);
      const fresh = await api.settings();
      setBundle(fresh);
      setDraft({});
      setKeyInput('');
      setSave('saved');
      setRefresh((value) => value + 1);
    } catch (e) {
      setErr(String(e));
      setSave('error');
    }
  };

  const clearKey = async () => {
    setSave('saving');
    try {
      await api.saveSettings({ ollama_cloud_key: '' });
      const fresh = await api.settings();
      setBundle(fresh);
      setKeyInput('');
      setSave('saved');
      setRefresh((value) => value + 1);
    } catch (e) {
      setErr(String(e));
      setSave('error');
    }
  };

  const cloudActive = bundle.cloud_active;
  const hasDraft = Object.keys(draft).length > 0 || keyInput !== '';

  return (
    <div className="view settings-view">
      <div className="view-h">System · Settings</div>
      <h1 className="view-title">Settings.</h1>
      <p className="view-sub">Choose what answers your questions. Changes apply to new calls after saving.</p>

      <Section title="AI model">
        <SelectField label="Provider" hint="Uses the Mastisk server’s sign-in or API key."
          value={provider} options={PROVIDERS.map((value) => ({ value, label: PROVIDER_NAMES[value] }))} onChange={setDefault} />
        <SelectField label="Model" hint="Models available through this service."
          value={model} options={modelOptions} onChange={changeModel} disabled={catalogBusy && !choices.length} />
        {thinkingLevels.length > 1 && (
          <SelectField label="Thinking" hint="Higher effort can take longer."
            value={thinkingValue} options={thinkingOptions} onChange={changeThinking} />
        )}
        {selected && thinkingLevels.length <= 1 && (
          <p className="settings-section-hint">{provider === 'anthropic' ? 'Thinking is managed by the Anthropic API integration.'
            : selected.thinking === null ? 'Thinking settings could not be read for this model.' : 'This model has no adjustable thinking level.'}</p>
        )}
        <div className="settings-catalog-status" role="status" aria-live="polite">
          <span>{catalogBusy ? 'Reading models…' : catalogError ?? (
            catalog?.status !== 'ready' ? catalog?.message ?? 'Model list unavailable. Your saved choice is unchanged.'
              : model && !selected ? 'Your saved model is not in this list. It stays selected until you choose a replacement.'
                : catalog.source === 'cli-bundled' ? 'Models from the CLI’s bundled catalog; account access is not checked.'
                  : provider === 'ollama' || provider === 'anthropic' ? 'Models read from the provider API.' : 'Models read from the installed server CLI.'
          )}{catalog?.stale && !catalogBusy ? ' Showing the last known list.' : ''}{!catalogBusy && modelNotice ? ` ${modelNotice}` : ''}</span>
          <button className="chip" type="button" aria-label="Refresh models" disabled={catalogBusy}
            onClick={() => setRefresh((value) => value + 1)}>Refresh</button>
        </div>
        <p className="settings-route">{intelligence.provider_order.length > 1
          ? `If ${PROVIDER_NAMES[provider]} fails: ${intelligence.provider_order.slice(1).map((item) => PROVIDER_NAMES[item]).join(' → ')}.`
          : 'No fallback provider is configured.'} Some tasks use their own model.</p>
        {intelligence.anthropic_auto && (
          <div className="settings-priority-note">
            <p>{bundle.effective_order?.[0] === 'anthropic' && provider !== 'anthropic'
              ? 'Anthropic API currently takes priority because automatic API priority is enabled.'
              : 'Automatic API priority can put Anthropic first when a key is configured and it is not already in your list.'}</p>
            <button className="chip" type="button" onClick={() => setIntelligence({ anthropic_auto: false })}>Use my selected provider</button>
          </div>
        )}
      </Section>

      <details className="settings-advanced">
        <summary>Advanced</summary>
        <p className="settings-section-hint">Optional overrides, connections and limits. The detected defaults usually need no changes.</p>
        <Section title="Installed CLIs">
          {CLI_PROVIDERS.map((cli) => {
            const key = `${cli}_cmd` as const;
            const detection = bundle.cli_detection?.[cli];
            const command = String(cur(key));
            const automatic = command === cli || command === detection?.auto_path;
            return <Field key={cli} label={`${PROVIDER_NAMES[cli]} executable override`}
              hint={`Detected: ${detection?.auto_path ?? detection?.path ?? 'not found'}. Leave blank to auto-detect. Save overrides before refreshing models.`}
              value={automatic ? '' : command} placeholder="Auto-detect"
              onChange={(value) => set({ [key]: value || cli })} />;
          })}
        </Section>
        <Section title="Routing and custom models">
          <Field label="Custom model ID" hint={`Optional: enter an unlisted model for ${PROVIDER_NAMES[provider]}. This does not install or download it.`}
            value={model} onChange={changeModel} />
          <Toggle label="Automatic Anthropic priority"
            hint="If an API key exists and Anthropic is not already in the fallback list, add it before the chosen provider. Leave off to honor your choice."
            value={intelligence.anthropic_auto} onChange={(value) => setIntelligence({ anthropic_auto: value })} />
          <p className="settings-section-hint">Saved runtime order: {(bundle.effective_order ?? v.intelligence.provider_order).map((item) => PROVIDER_NAMES[item]).join(' → ')}. Temporarily failing services can be skipped.</p>
          <p className="settings-config-path">Configuration: <code>{bundle.config_path}</code></p>
        </Section>
      <Section title="Ollama connection">
        <Field
          label="Local Ollama address"
          hint="Where your local Ollama daemon is listening. The default port is 11434."
          value={String(cur('ollama_local_url'))}
          onChange={(val) => set({ ollama_local_url: val })}
          placeholder="http://localhost:11434"
        />
        <Toggle
          label="Use the local endpoint only"
          hint="Use only the local Ollama endpoint instead of the direct cloud API. A signed-in local daemon can still proxy :cloud models."
          value={Boolean(cur('ollama_local_only'))}
          onChange={(val) => set({ ollama_local_only: val })}
        />
        <div className="settings-row">
          <label className="settings-label" htmlFor="ollama-cloud-api-key">
            <div className="k">Ollama cloud API key</div>
            <div className="h">
              Sign in to <a href="https://ollama.com" target="_blank" rel="noreferrer">ollama.com</a> and paste the API key here.
              Stored only in your local config.toml. A signed-in local Ollama daemon can proxy cloud models without this key.
            </div>
          </label>
          <div className="settings-ctl">
            <input
              type="password"
              autoComplete="off"
              id="ollama-cloud-api-key"
              value={keyInput}
              placeholder={v.ollama_cloud_key_set ? '•••••••• (set — leave blank to keep)' : 'paste a key to enable cloud'}
              onChange={(e) => { setKeyInput(e.target.value); setSave('idle'); }}
            />
            <div className="settings-inline-note">
              {v.ollama_cloud_key_set ? (
                <>
                  <span>Key is set.</span>
                  <button className="chip" onClick={() => void clearKey()} disabled={save==='saving'}>Clear key</button>
                </>
              ) : (
                <span>No cloud key on file.</span>
              )}
              <span className={`pill ${cloudActive ? 'on' : 'off'}`}>
                direct API {cloudActive ? 'active' : 'inactive'}
              </span>
            </div>
            {!cloudActive && v.ollama_cloud_key_set && v.ollama_local_only && (
              <div className="settings-inline-note warn">
                Direct cloud API access is bypassed. The local endpoint may still proxy cloud-tagged models.
              </div>
            )}
          </div>
        </div>
        <Field
          label="Ollama cloud address"
          hint="Only edit this if you're proxying Ollama Cloud through your own host."
          value={String(cur('ollama_cloud_url'))}
          onChange={(val) => set({ ollama_cloud_url: val })}
          placeholder="https://ollama.com"
        />
      </Section>

      <Section title="Specialised models">
        <Field label="Embedding model" hint="Ollama model used for similarity search, not chat."
          value={String(cur('embed_model'))} onChange={(value) => set({ embed_model: value })} />
        <Field label="Fast local model" hint="Used only for explicit lightweight Ollama calls. Main Ask and shared agents use the AI model above."
          value={String(cur('summarize_model_cheap'))} onChange={(value) => set({ summarize_model_cheap: value })} />
      </Section>

      <Section title="GitHub">
        {githubState === null ? (
          <p className="settings-section-hint">loading…</p>
        ) : (
          <>
            <div className="settings-row">
              <label className="settings-label">
                <div className="k">Current PAT</div>
                <div className="h">
                  Without a PAT, mastisk polls public repos at 60 req/hour.
                  With a <a href="https://github.com/settings/tokens" target="_blank" rel="noreferrer">classic PAT</a>
                  {' '}(scope: <code>public_repo</code> for public only, <code>repo</code> for private)
                  you get 5000/hour and access to private repos.
                </div>
              </label>
              <div className="settings-ctl">
                {githubState.pat_set ? (
                  <code style={{ fontFamily: 'var(--mono)' }}>{githubState.pat_last4}</code>
                ) : (
                  <em style={{ color: 'var(--fg-faint)' }}>not set (unauthenticated polling, 60 req/hr)</em>
                )}
              </div>
            </div>

            <div className="settings-row">
              <label className="settings-label" htmlFor="github-access-token">
                <div className="k">GitHub access token</div>
                <div className="h">Paste a new token here and hit save. Empty string clears.</div>
              </label>
              <div className="settings-ctl">
                <input
                  type="password"
                  autoComplete="off"
                  id="github-access-token"
                  value={newPat}
                  placeholder="ghp_... (classic) or github_pat_... (fine-grained)"
                  onChange={(e) => { setNewPat(e.target.value); setGhMsg(null); }}
                />
                <div className="settings-inline-note">
                  <button
                    className="chip"
                    disabled={ghSaveBusy}
                    onClick={async () => {
                      setGhSaveBusy(true);
                      setGhMsg(null);
                      try {
                        const res = await api.github.setPat(newPat);
                        setGithubState({ pat_set: res.pat_set, pat_last4: res.pat_last4 });
                        setNewPat('');
                        setGhMsg({ kind: 'ok', text: res.pat_set ? 'PAT saved' : 'PAT cleared' });
                      } catch (e) {
                        setGhMsg({ kind: 'err', text: e instanceof Error ? e.message : 'save failed' });
                      } finally {
                        setGhSaveBusy(false);
                      }
                    }}
                  >
                    {ghSaveBusy ? 'saving…' : 'Save'}
                  </button>
                  {githubState.pat_set && (
                    <button
                      className="chip"
                      disabled={ghTestBusy}
                      onClick={async () => {
                        setGhTestBusy(true);
                        setGhMsg(null);
                        try {
                          const r = await api.github.testPat();
                          setGhMsg({
                            kind: 'ok',
                            text: `authenticated as @${r.username} (${r.public_repos} public repos)`,
                          });
                        } catch (e) {
                          setGhMsg({ kind: 'err', text: e instanceof Error ? e.message : 'test failed' });
                        } finally {
                          setGhTestBusy(false);
                        }
                      }}
                    >
                      {ghTestBusy ? 'testing…' : 'Test'}
                    </button>
                  )}
                </div>
                {ghMsg && (
                  <div className={`settings-inline-note ${ghMsg.kind === 'err' ? 'warn' : ''}`}>
                    {ghMsg.text}
                  </div>
                )}
              </div>
            </div>
          </>
        )}
      </Section>

      <Section title="Daily agent budgets">
        <p className="settings-section-hint">
          Caps on how many items each agent will act on per day. Stops a chatty feed from burning through cloud calls.
        </p>
        {(Object.keys(v.budget) as (keyof typeof v.budget)[]).map((k) => (
          <NumberField
            key={k}
            label={k}
            value={Number((draft.budget ?? v.budget)[k])}
            onChange={(n) => setBudget(k, n)}
          />
        ))}
      </Section>

      </details>

      <div className="settings-footer">
        <button
          className="settings-save"
          onClick={() => void submit()}
          disabled={!hasDraft || save === 'saving'}
        >
          {save === 'saving' ? 'Saving…' : save === 'saved' ? 'Saved' : 'Save changes'}
        </button>
        {hasDraft && (
          <button className="settings-cancel" onClick={() => { setDraft({}); setKeyInput(''); setSave('idle'); }}>
            Discard
          </button>
        )}
        {err && <span className="settings-error">{err}</span>}
      </div>
    </div>
  );
}

// Labels are presentation-only; wire values always remain the provider's exact identifiers.
function thinkingLabel(value: string): string {
  const labels: Record<string, string> = { off: 'Off', on: 'On', xhigh: 'Extra high' };
  return labels[value] ?? value.charAt(0).toUpperCase() + value.slice(1);
}

// Friendly labels may contain spaces; controls still need valid, stable label/description targets.
function controlId(label: string): string {
  return `settings-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="settings-section">
      <h2 className="settings-section-title">{title}</h2>
      {children}
    </section>
  );
}

function Field({
  label, hint, value, onChange, placeholder,
}: {
  label: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="settings-row">
      <label className="settings-label" htmlFor={controlId(label)}>
        <div className="k">{label}</div>
        <div className="h" id={`${controlId(label)}-hint`}>{hint}</div>
      </label>
      <div className="settings-ctl">
        <input id={controlId(label)} aria-describedby={`${controlId(label)}-hint`} type="text" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      </div>
    </div>
  );
}

// Native selects keep provider/thinking controls keyboard-accessible and match existing fields.
function SelectField({ label, hint, value, options, onChange, disabled = false }: {
  label: string;
  hint: string;
  value: string;
  options: ReadonlyArray<{ value: string; label: string }>;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="settings-row">
      <label className="settings-label" htmlFor={controlId(label)}>
        <div className="k">{label}</div>
        <div className="h" id={`${controlId(label)}-hint`}>{hint}</div>
      </label>
      <div className="settings-ctl">
        <select id={controlId(label)} aria-describedby={`${controlId(label)}-hint`} aria-label={label} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
          {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </div>
    </div>
  );
}

function NumberField({
  label, value, onChange,
}: { label: string; value: number; onChange: (n: number) => void }) {
  return (
    <div className="settings-row">
      <label className="settings-label">
        <div className="k">{label}</div>
      </label>
      <div className="settings-ctl settings-ctl-number">
        <input
          type="number" min={0} max={100000} value={Number.isFinite(value) ? value : 0}
          onChange={(e) => onChange(Math.max(0, Math.min(100000, Number(e.target.value) || 0)))}
        />
      </div>
    </div>
  );
}

function Toggle({
  label, hint, value, onChange,
}: {
  label: string;
  hint: string;
  value: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="settings-row">
      <label className="settings-label">
        <div className="k">{label}</div>
        <div className="h">{hint}</div>
      </label>
      <div className="settings-ctl">
        <button
          className={`settings-toggle ${value ? 'on' : 'off'}`}
          aria-label={label}
          onClick={() => onChange(!value)}
          type="button"
          aria-pressed={value}
        >
          <span className="dot"/>
          <span className="lbl">{value ? 'on' : 'off'}</span>
        </button>
      </div>
    </div>
  );
}
