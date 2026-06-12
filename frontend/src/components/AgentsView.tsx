import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../api';
import type { AgentDetail, AgentInfo, AgentPromptSlot, AgentSkill, FeedTick, View } from '../types';
import { CodeMirrorMarkdown } from './MarkdownEditor';

interface Props {
  agents: AgentInfo[];
  feed: FeedTick[];
  agentId?: string;
  onNavigate?: (view: View, id?: string) => void;
  onAgentsChanged?: () => void | Promise<void>;
}

export function AgentsView({ agents, feed, agentId, onNavigate, onAgentsChanged }: Props) {
  if (agentId) {
    return (
      <AgentDetailView
        agentId={agentId}
        agents={agents}
        onNavigate={onNavigate}
        onAgentsChanged={onAgentsChanged}
      />
    );
  }
  return <AgentListView agents={agents} feed={feed} onNavigate={onNavigate}/>;
}

function AgentListView({
  agents,
  feed,
  onNavigate,
}: {
  agents: AgentInfo[];
  feed: FeedTick[];
  onNavigate?: (view: View, id?: string) => void;
}) {
  return (
    <div className="view">
      <div className="view-h">System / Agents</div>
      <h1 className="view-title">Your agents, always on.</h1>
      <p className="view-sub">Each agent is a narrow specialist that hands off to the next. Click into a card to inspect the live prompt, customize it, and attach reusable skill snippets.</p>

      <div className="agent-grid">
        {agents.map((a) => (
          <button
            key={a.id}
            className={`agent-card clickable ${a.implemented ? '' : 'disabled'}`}
            type="button"
            onClick={() => onNavigate?.('agent_detail', a.id)}
          >
            <div className={`agent-status ${a.status}`}>
              <span className="s-dot"/>{a.status === 'disabled' ? 'disabled' : a.status}
            </div>
            <div className="a-name">{a.name}</div>
            <div className="a-role">{a.role}</div>
            {a.profile?.invalid && (
              <div className="agent-warning">override ignored: {a.profile.invalid_reason ?? 'invalid prompt override'}</div>
            )}
            {a.implemented ? (
              <div className="agent-load">
                <div className="label">
                  <span>queue</span>
                  <span>{a.queued} queued / {a.running} running</span>
                </div>
                <div className="bar"><div className="fill" style={{ width: `${Math.min(1, a.load) * 100}%` }}/></div>
              </div>
            ) : (
              <div className="agent-load">
                <div className="label" style={{ color: 'var(--fg-faint)' }}>
                  <span>status</span><span>no implementation</span>
                </div>
              </div>
            )}
          </button>
        ))}
      </div>

      <div style={{ marginTop: 48 }}>
        <div className="view-h">Recent operations</div>
        <div style={{ borderTop: '1px solid var(--line)' }}>
          {feed.map((f, i) => (
            <div key={i} style={{ display: 'flex', gap: 14, padding: '12px 0', borderBottom: '1px solid var(--line-soft)', alignItems: 'center' }}>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--fg-faint)', width: 40 }}>{f.t}</div>
              <span className={`tick-agent ${agents.find((a) => a.id === f.agent)?.color || ''}`} style={{ width: 100, textAlign: 'center' }}>{f.agent}</span>
              <span style={{ color: 'var(--fg-mute)', width: 90, fontFamily: 'var(--mono)', fontSize: 11 }}>{f.verb}</span>
              <span style={{ flex: 1, color: 'var(--fg)', fontSize: 13 }}>{f.obj}</span>
              {f.touched > 0 && <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--fg-faint)' }}>+{f.touched} pages</span>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function AgentDetailView({
  agentId,
  agents,
  onNavigate,
  onAgentsChanged,
}: {
  agentId: string;
  agents: AgentInfo[];
  onNavigate?: (view: View, id?: string) => void;
  onAgentsChanged?: () => void | Promise<void>;
}) {
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [skills, setSkills] = useState<string[]>([]);
  const [primaryDraft, setPrimaryDraft] = useState('');
  const [slotDrafts, setSlotDrafts] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<Record<string, boolean>>({});
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [saveError, setSaveError] = useState<string | null>(null);
  const [editorError, setEditorError] = useState<string | null>(null);
  const [newSkillName, setNewSkillName] = useState('');
  const [newSkillBody, setNewSkillBody] = useState('');
  const [skillError, setSkillError] = useState<string | null>(null);
  const [skillSaving, setSkillSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setLoadError(null);
    api.agentDetail(agentId)
      .then((next) => {
        if (!cancelled) setDetail(next);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : 'failed to load agent');
      });
    return () => { cancelled = true; };
  }, [agentId]);

  useEffect(() => {
    if (!detail) return;
    const primary = detail.slots.find((slot) => slot.primary) ?? detail.slots[0];
    setEnabled(detail.profile.enabled);
    setSkills(detail.profile.skills);
    setPrimaryDraft(primary ? (primary.override || primary.default) : '');
    setSlotDrafts(() => {
      const next: Record<string, string> = {};
      for (const slot of detail.slots) {
        if (!slot.primary) next[slot.slot_id] = slot.override || slot.default;
      }
      return next;
    });
    setEditing(() => {
      const next: Record<string, boolean> = {};
      for (const slot of detail.slots) {
        next[slot.slot_id] = Boolean(slot.override);
      }
      return next;
    });
    setSaveError(null);
    setSaveState('idle');
  }, [detail]);

  const primarySlot = useMemo(
    () => detail?.slots.find((slot) => slot.primary) ?? detail?.slots[0],
    [detail],
  );
  const knownAgent = agents.find((agent) => agent.id === agentId);
  const status = detail?.agent.status ?? knownAgent?.status ?? 'idle';
  const skillNameInvalid = Boolean(newSkillName.trim() && !isSafeSkillName(newSkillName.trim()));

  const save = useCallback(async () => {
    if (!detail || !primarySlot) return;
    const previous = detail;
    const slotOverrides: Record<string, string> = {};
    for (const slot of detail.slots) {
      if (slot.primary) continue;
      const draft = slotDrafts[slot.slot_id] ?? '';
      if (draft.trim() && draft !== slot.default) {
        slotOverrides[slot.slot_id] = draft;
      }
    }
    const promptOverride = primaryDraft.trim() && primaryDraft !== primarySlot.default
      ? primaryDraft
      : null;
    const optimistic: AgentDetail = {
      ...detail,
      profile: {
        ...detail.profile,
        enabled,
        skills,
        prompt_override: promptOverride ?? '',
        slot_overrides: slotOverrides,
      },
    };
    setDetail(optimistic);
    setSaveState('saving');
    setSaveError(null);
    try {
      await api.updateAgentProfile(agentId, {
        enabled,
        skills,
        prompt_override: promptOverride,
        slot_overrides: slotOverrides,
      });
      const fresh = await api.agentDetail(agentId);
      setDetail(fresh);
      await onAgentsChanged?.();
      setSaveState('saved');
      window.setTimeout(() => setSaveState('idle'), 1600);
    } catch (e) {
      setDetail(previous);
      setSaveState('error');
      if (e instanceof ApiError && e.status === 422) {
        setSaveError(`Placeholder validation failed: ${e.detail}`);
      } else {
        setSaveError(e instanceof Error ? e.message : 'save failed');
      }
    }
  }, [agentId, detail, enabled, onAgentsChanged, primaryDraft, primarySlot, skills, slotDrafts]);

  const addSkill = useCallback(async () => {
    const name = newSkillName.trim();
    const body = newSkillBody.trim();
    if (!name || !body) return;
    if (!isSafeSkillName(name)) {
      setSkillError('Skill name may only use letters, numbers, spaces, and . , ( ) & / -');
      return;
    }
    setSkillSaving(true);
    setSkillError(null);
    try {
      const created = await api.createAgentSkill({ name, body });
      setDetail((current) => current ? { ...current, skills: [...current.skills, created] } : current);
      setSkills((current) => current.includes(created.slug) ? current : [...current, created.slug]);
      setNewSkillName('');
      setNewSkillBody('');
    } catch (e) {
      setSkillError(e instanceof Error ? e.message : 'skill save failed');
    } finally {
      setSkillSaving(false);
    }
  }, [newSkillBody, newSkillName]);

  if (loadError) {
    return (
      <div className="view">
        <button className="chip muted" type="button" onClick={() => onNavigate?.('agents')}>back to agents</button>
        <p className="dash-error">{loadError}</p>
      </div>
    );
  }
  if (!detail || !primarySlot) {
    return (
      <div className="view">
        <p className="dash-empty">loading agent...</p>
      </div>
    );
  }

  const secondarySlots = detail.slots.filter((slot) => !slot.primary);

  return (
    <div className="view agent-detail-view">
      <button className="chip muted" type="button" onClick={() => onNavigate?.('agents')}>back to agents</button>

      <header className="agent-studio-header">
        <div>
          <div className={`agent-status ${status}`}>
            <span className="s-dot"/>{status}
          </div>
          <h1 className="view-title">{detail.agent.name}</h1>
          <p className="view-sub">{detail.agent.role}</p>
        </div>
        <label className="agent-toggle">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          <span>{enabled ? 'enabled' : 'disabled'}</span>
        </label>
      </header>

      {detail.profile.invalid && (
        <div className="agent-warning prominent">override ignored: {detail.profile.invalid_reason ?? 'invalid prompt override'}</div>
      )}
      {saveError && <div className="dash-error agent-save-error">{saveError}</div>}
      {editorError && <div className="dash-error agent-save-error">{editorError}</div>}

      <div className="agent-studio-layout">
        <main className="agent-studio-main">
          <section className="agent-panel">
            <div className="agent-section-head">
              <div>
                <div className="view-h">Prompt</div>
                <h2>{primarySlot.label}</h2>
              </div>
              <PromptState slot={primarySlot} draft={primaryDraft}/>
            </div>
            <PromptEditor
              slot={primarySlot}
              value={primaryDraft}
              editing={Boolean(editing[primarySlot.slot_id])}
              onEdit={() => {
                setPrimaryDraft(primarySlot.override || primarySlot.default);
                setEditing((current) => ({ ...current, [primarySlot.slot_id]: true }));
              }}
              onReset={() => {
                setPrimaryDraft(primarySlot.default);
                setEditing((current) => ({ ...current, [primarySlot.slot_id]: false }));
              }}
              onChange={setPrimaryDraft}
              onSave={save}
              onEditorError={setEditorError}
            />
          </section>

          {secondarySlots.length > 0 && (
            <section className="agent-panel">
              <div className="agent-section-head">
                <div>
                  <div className="view-h">Secondary slots</div>
                  <h2>Supporting templates</h2>
                </div>
              </div>
              <div className="agent-slot-list">
                {secondarySlots.map((slot) => (
                  <details key={slot.slot_id} className="agent-slot-accordion">
                    <summary>
                      <span>{slot.label}</span>
                      <PromptState slot={slot} draft={slotDrafts[slot.slot_id] ?? slot.default}/>
                    </summary>
                    <PromptEditor
                      slot={slot}
                      value={slotDrafts[slot.slot_id] ?? slot.default}
                      editing={Boolean(editing[slot.slot_id])}
                      onEdit={() => {
                        setSlotDrafts((current) => ({
                          ...current,
                          [slot.slot_id]: slot.override || slot.default,
                        }));
                        setEditing((current) => ({ ...current, [slot.slot_id]: true }));
                      }}
                      onReset={() => {
                        setSlotDrafts((current) => ({ ...current, [slot.slot_id]: slot.default }));
                        setEditing((current) => ({ ...current, [slot.slot_id]: false }));
                      }}
                      onChange={(value) => setSlotDrafts((current) => ({ ...current, [slot.slot_id]: value }))}
                      onSave={save}
                      onEditorError={setEditorError}
                    />
                  </details>
                ))}
              </div>
            </section>
          )}

          <section className="agent-panel">
            <div className="agent-section-head">
              <div>
                <div className="view-h">Skills</div>
                <h2>Additional instructions</h2>
              </div>
            </div>
            <p className="agent-note">Skills are reusable prompt-augmentation snippets. Tagged skills append under "Additional instructions" on this agent's primary prompt.</p>
            <SkillChips library={detail.skills} selected={skills} onToggle={(slug) => {
              setSkills((current) => current.includes(slug)
                ? current.filter((item) => item !== slug)
                : [...current, slug]);
            }}/>
            <div className="new-skill-box">
              <input
                value={newSkillName}
                onChange={(event) => setNewSkillName(event.target.value)}
                placeholder="+ New skill name"
                aria-invalid={skillNameInvalid}
                title="Skill names may only use letters, numbers, spaces, and . , ( ) & / -"
              />
              {skillNameInvalid && (
                <p className="agent-note">Skill names may only use letters, numbers, spaces, and . , ( ) & / -.</p>
              )}
              <textarea
                value={newSkillBody}
                onChange={(event) => setNewSkillBody(event.target.value)}
                placeholder="Reusable instruction snippet"
              />
              <button className="chip" type="button" disabled={skillSaving || skillNameInvalid || !newSkillName.trim() || !newSkillBody.trim()} onClick={addSkill}>
                {skillSaving ? 'creating' : '+ New skill'}
              </button>
              {skillError && <p className="dash-error">{skillError}</p>}
            </div>
          </section>
        </main>

        <aside className="agent-studio-rail">
          <button className="agent-save-button" type="button" disabled={saveState === 'saving'} onClick={save}>
            {saveState === 'saving' ? 'saving' : saveState === 'saved' ? 'saved' : 'save profile'}
          </button>
          <div className="agent-save-hint">cmd+enter from a prompt editor saves.</div>
          <section className="agent-rail-card">
            <div className="view-h">Queue</div>
            <div className="agent-queue-stats">
              <span><b>{detail.queue.queued}</b> queued</span>
              <span><b>{detail.queue.running}</b> running</span>
              <span><b>{detail.queue.failed}</b> failed</span>
            </div>
          </section>
          <section className="agent-rail-card">
            <div className="view-h">Model</div>
            <input className="agent-model-input" value={detail.profile.model ?? ''} disabled placeholder="model override disabled"/>
            <p className="agent-note">{detail.profile.model_note}</p>
          </section>
          <section className="agent-rail-card">
            <div className="view-h">Recent operations</div>
            <div className="agent-mini-feed">
              {detail.recent_feed.length === 0 && <p className="dash-empty">No recent operations.</p>}
              {detail.recent_feed.map((item) => (
                <div key={item.id ?? `${item.t}-${item.obj}`} className="agent-mini-row">
                  <span>{item.t}</span>
                  <b>{item.verb}</b>
                  <p>{item.obj}</p>
                </div>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}

function PromptEditor({
  slot,
  value,
  editing,
  onEdit,
  onReset,
  onChange,
  onSave,
  onEditorError,
}: {
  slot: AgentPromptSlot;
  value: string;
  editing: boolean;
  onEdit: () => void;
  onReset: () => void;
  onChange: (value: string) => void;
  onSave: () => void;
  onEditorError: (message: string | null) => void;
}) {
  const missing = missingPlaceholders(slot, value);
  return (
    <div className="agent-prompt-box">
      {slot.required_placeholders.length > 0 && (
        <div className="placeholder-chips">
          {slot.required_placeholders.map((name) => (
            <span key={name} className={missing.includes(name) ? 'missing' : ''}>{`{${name}}`}</span>
          ))}
        </div>
      )}
      {slot.invalid && <div className="agent-warning">override ignored: {slot.invalid_reason ?? 'invalid override'}</div>}
      {missing.length > 0 && (
        <div className="agent-warning">missing {missing.map((name) => `{${name}}`).join(', ')}</div>
      )}
      {editing ? (
        <div className="agent-code-editor">
          <CodeMirrorMarkdown
            value={value}
            onChange={onChange}
            onError={onEditorError}
            onModEnter={onSave}
          />
        </div>
      ) : (
        <pre className="prompt-well">{value}</pre>
      )}
      <div className="agent-editor-actions">
        {!editing && <button className="chip" type="button" onClick={onEdit}>Customize</button>}
        <button className="chip muted" type="button" onClick={onReset}>Reset to default</button>
      </div>
    </div>
  );
}

function PromptState({ slot, draft }: { slot: AgentPromptSlot; draft: string }) {
  const customized = Boolean(slot.override) || (draft.trim() && draft !== slot.default);
  return <span className={`prompt-state ${customized ? 'custom' : ''}`}>{customized ? 'customized' : 'default'}</span>;
}

function SkillChips({
  library,
  selected,
  onToggle,
}: {
  library: AgentSkill[];
  selected: string[];
  onToggle: (slug: string) => void;
}) {
  if (library.length === 0) {
    return <p className="dash-empty">No skills yet. Create one below.</p>;
  }
  return (
    <div className="skill-chip-wrap">
      {library.map((skill) => {
        const active = selected.includes(skill.slug);
        return (
          <button
            key={skill.slug}
            className={`skill-chip ${active ? 'active' : ''}`}
            type="button"
            onClick={() => onToggle(skill.slug)}
            title={skill.description ?? skill.slug}
          >
            {skill.name}
          </button>
        );
      })}
    </div>
  );
}

function missingPlaceholders(slot: AgentPromptSlot, text: string): string[] {
  return slot.required_placeholders.filter((name) => !containsPlaceholder(text, name));
}

function containsPlaceholder(text: string, name: string): boolean {
  return (
    text.includes(`{${name}}`) ||
    text.includes(`{${name}:`) ||
    text.includes(`{${name}!`) ||
    text.includes(`{${name}.`) ||
    text.includes(`{${name}[`)
  );
}

function isSafeSkillName(name: string): boolean {
  return /^[\w\s.,()&\/-]+$/u.test(name);
}
