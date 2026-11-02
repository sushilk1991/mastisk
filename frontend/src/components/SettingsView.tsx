import { useEffect, useState } from 'react';
import { api } from '../api';
import type { IntelligenceProvider, IntelligenceValues, SettingsBundle, SettingsPatch } from '../types';

// Provider names and thinking levels are explicit so saved values match the runtime API.
const PROVIDERS: IntelligenceProvider[] = ['ollama', 'codex', 'claude', 'muse', 'anthropic'];
const PROVIDER_NAMES: Record<IntelligenceProvider, string> = {
  ollama: 'Ollama', codex: 'Codex', claude: 'Claude', muse: 'Muse', anthropic: 'Anthropic API',
};
const THINKING_LEVELS = ['low', 'medium', 'high', 'max'] as const;

type SaveState = 'idle' | 'saving' | 'saved' | 'error';

type GithubState = { pat_set: boolean; pat_last4: string | null } | null;
type GhMsg = { kind: 'ok' | 'err'; text: string } | null;

export function SettingsView() {
  const [bundle, setBundle] = useState<SettingsBundle | null>(null);
  const [draft, setDraft] = useState<SettingsPatch>({});
  const [keyInput, setKeyInput] = useState('');
  const [save, setSave] = useState<SaveState>('idle');
  const [err, setErr] = useState<string | null>(null);

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

  if (!bundle) {
    return (
      <div className="view">
        {err ? <p style={{color:'#c53030'}}>{err}</p> : <p style={{color:'var(--fg-faint)'}}>loading…</p>}
      </div>
    );
  }

  const v = bundle.values;
  const cur = <K extends keyof typeof v>(k: K) => (draft as Record<string, unknown>)[k as string] ?? v[k];
  const set = (patch: SettingsPatch) => { setDraft((d) => ({ ...d, ...patch })); setSave('idle'); setErr(null); };
  // Merge nested drafts so changing one model pin cannot reset another provider's fields.
  const intelligence = { ...v.intelligence, ...draft.intelligence };
  const setIntelligence = (patch: Partial<IntelligenceValues>) => {
    set({ intelligence: { ...intelligence, ...patch } });
  };
  const setDefault = (value: string) => {
    const provider = PROVIDERS.find((item) => item === value);
    if (!provider) {
      return;
    }
    setIntelligence({
      provider_order: [provider, ...intelligence.provider_order.filter((item) => item !== provider)],
      anthropic_auto: false,
    });
  };
  const changeThinking = (value: string) => {
    if (value === 'on' || value === 'off') {
      set({ ollama_think: value === 'on' });
      return;
    }
    const level = THINKING_LEVELS.find((item) => item === value);
    if (level) {
      set({ ollama_think: level });
    }
  };
  const thinking = draft.ollama_think ?? v.ollama_think;
  const thinkingValue = typeof thinking === 'boolean' ? (thinking ? 'on' : 'off') : thinking;

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
      <p className="view-sub">
        Write straight into <code style={{fontFamily:'var(--mono)',fontSize:12,background:'var(--bg-sunk)',padding:'1px 6px',borderRadius:4}}>{bundle.config_path}</code>.
        Model, provider, thinking, and budget changes apply to new calls immediately. CLI paths apply to the next subprocess call.
      </p>

      <Section title="Default provider">
        <SelectField
          label="Default provider"
          hint={`Shared intelligence and Ask fallback order: ${intelligence.provider_order.map((item) => PROVIDER_NAMES[item]).join(' → ')}. Explicit task-specific choices stay unchanged.`}
          value={intelligence.provider_order[0]}
          options={PROVIDERS.map((value) => ({ value, label: PROVIDER_NAMES[value] }))}
          onChange={setDefault}
        />
        <Toggle
          label="intelligence.anthropic_auto"
          hint="Automatically put the Anthropic API first when a key exists. Choosing a default provider turns this override off."
          value={intelligence.anthropic_auto}
          onChange={(value) => setIntelligence({ anthropic_auto: value })}
        />
      </Section>

      <Section title="Claude">
        <Field
          label="claude_cmd"
          hint={bundle.model_roles.claude_cmd}
          value={String(cur('claude_cmd'))}
          onChange={(val) => set({ claude_cmd: val })}
          placeholder="claude"
        />
        <Field
          label="intelligence.claude_model"
          hint="Model pin for the shared Claude tier. Leave blank for the CLI default."
          value={intelligence.claude_model ?? ''}
          onChange={(value) => setIntelligence({ claude_model: value })}
        />
      </Section>

      <Section title="Codex">
        <Field label="codex_cmd" hint={bundle.model_roles.codex_cmd}
          value={String(cur('codex_cmd'))} onChange={(value) => set({ codex_cmd: value })} placeholder="codex" />
        <Field label="intelligence.codex_model" hint="Model pin for the shared Codex tier. Leave blank for the CLI default."
          value={intelligence.codex_model ?? ''} onChange={(value) => setIntelligence({ codex_model: value })} />
        <Field label="intelligence.codex_reasoning_effort" hint="Reasoning effort supported by the selected Codex model."
          value={intelligence.codex_reasoning_effort ?? ''} onChange={(value) => setIntelligence({ codex_reasoning_effort: value })} />
      </Section>

      <Section title="Muse">
        <Field label="muse_cmd" hint={bundle.model_roles.muse_cmd}
          value={String(cur('muse_cmd'))} onChange={(value) => set({ muse_cmd: value })} placeholder="muse" />
        <Field label="intelligence.muse_model" hint="Model pin for the shared Muse tier. Leave blank for the CLI default."
          value={intelligence.muse_model ?? ''} onChange={(value) => setIntelligence({ muse_model: value })} />
        <Field label="intelligence.muse_reasoning_effort" hint="Muse reasoning effort, for example high or xhigh."
          value={intelligence.muse_reasoning_effort ?? ''} onChange={(value) => setIntelligence({ muse_reasoning_effort: value })} />
      </Section>

      <Section title="Ollama connection">
        <Field
          label="ollama_local_url"
          hint="Where your local Ollama daemon is listening. The default port is 11434."
          value={String(cur('ollama_local_url'))}
          onChange={(val) => set({ ollama_local_url: val })}
          placeholder="http://localhost:11434"
        />
        <Toggle
          label="ollama_local_only"
          hint="Use only the local Ollama endpoint instead of the direct cloud API. A signed-in local daemon can still proxy :cloud models."
          value={Boolean(cur('ollama_local_only'))}
          onChange={(val) => set({ ollama_local_only: val })}
        />
        <div className="settings-row">
          <label className="settings-label">
            <div className="k">ollama_cloud_key</div>
            <div className="h">
              Sign in to <a href="https://ollama.com" target="_blank" rel="noreferrer">ollama.com</a> and paste the API key here.
              Stored only in your local config.toml. A signed-in local Ollama daemon can proxy cloud models without this key.
            </div>
          </label>
          <div className="settings-ctl">
            <input
              type="password"
              autoComplete="off"
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
          label="ollama_cloud_url"
          hint="Only edit this if you're proxying Ollama Cloud through your own host."
          value={String(cur('ollama_cloud_url'))}
          onChange={(val) => set({ ollama_cloud_url: val })}
          placeholder="https://ollama.com"
        />
      </Section>

      <Section title="Models">
        <Field
          label="embed_model"
          hint={bundle.model_roles.embed_model}
          value={String(cur('embed_model'))}
          onChange={(val) => set({ embed_model: val })}
          placeholder="nomic-embed-text"
        />
        <Field
          label="summarize_model_cheap"
          hint={bundle.model_roles.summarize_model_cheap}
          value={String(cur('summarize_model_cheap'))}
          onChange={(val) => set({ summarize_model_cheap: val })}
          placeholder="qwen3.5:4b"
        />
        <Field
          label="summarize_model_heavy"
          hint={bundle.model_roles.summarize_model_heavy}
          value={String(cur('summarize_model_heavy'))}
          onChange={(val) => set({ summarize_model_heavy: val })}
          placeholder="deepseek-v4.1-flash:1017-cloud"
        />
        <SelectField
          label="ollama_think"
          hint="Thinking for the main Ollama model. Use a level supported by that model; DeepSeek 1017 supports high. Cheap-model calls stay non-thinking."
          value={thinkingValue}
          options={[
            { value: 'off', label: 'Off' }, { value: 'on', label: 'On (model default)' },
            ...THINKING_LEVELS.map((value) => ({ value, label: value })),
          ]}
          onChange={changeThinking}
        />
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
              <label className="settings-label">
                <div className="k">github.pat</div>
                <div className="h">Paste a new token here and hit save. Empty string clears.</div>
              </label>
              <div className="settings-ctl">
                <input
                  type="password"
                  autoComplete="off"
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
      <label className="settings-label" htmlFor={label}>
        <div className="k">{label}</div>
        <div className="h">{hint}</div>
      </label>
      <div className="settings-ctl">
        <input id={label} type="text" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      </div>
    </div>
  );
}

// Native selects keep provider/thinking controls keyboard-accessible and match existing fields.
function SelectField({ label, hint, value, options, onChange }: {
  label: string;
  hint: string;
  value: string;
  options: ReadonlyArray<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <div className="settings-row">
      <label className="settings-label" htmlFor={label}>
        <div className="k">{label}</div>
        <div className="h">{hint}</div>
      </label>
      <div className="settings-ctl">
        <select id={label} aria-label={label} value={value} onChange={(event) => onChange(event.target.value)}>
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
