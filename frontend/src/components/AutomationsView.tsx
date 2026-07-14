import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { Automation, AutomationDetail, AutomationTriggers, View } from '../types';

interface Props {
  liveKey: number;
  onNavigate: (view: View, id?: string) => void;
}

export function AutomationsView({ liveKey }: Props) {
  const [tasks, setTasks] = useState<Automation[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AutomationDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.automations.list()
      .then((d) => setTasks(d.automations))
      .catch((e: Error) => setErr(e.message));
  }, []);

  useEffect(() => { load(); }, [load, liveKey]);

  useEffect(() => {
    if (!selected) { setDetail(null); return; }
    let live = true;
    api.automations.get(selected)
      .then((d) => { if (live) setDetail(d); })
      .catch((e: Error) => { if (live) setErr(e.message); });
    return () => { live = false; };
  }, [selected, liveKey]);

  async function runNow(slug: string) {
    setBusy(true);
    try {
      await api.automations.run(slug);
      load();
      if (selected === slug) setDetail(await api.automations.get(slug));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function toggle(task: Automation) {
    try {
      await api.automations.patch(task.slug, { active: !task.active });
      load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  if (err && !tasks) {
    return (
      <div className="view">
        <div className="view-h">System · Automations</div>
        <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>
          couldn't load automations: {err}
        </p>
      </div>
    );
  }

  return (
    <div className="view">
      <div className="view-h">System · Automations</div>
      <h1 className="view-title">Standing orders for your agents.</h1>
      <p className="view-sub">
        Describe a recurring job in plain words — a morning digest, a tracker, an
        alert — and it runs on your schedule against the wiki. The output lives in
        each automation's own note.
      </p>

      {err && <p className="dash-error">{err}</p>}

      <div style={{margin: '18px 0'}}>
        <button type="button" className="chip" onClick={() => setCreating(!creating)}>
          {creating ? 'cancel' : '+ new automation'}
        </button>
      </div>

      {creating && (
        <CreateForm
          onCreated={(slug) => { setCreating(false); load(); setSelected(slug); }}
          onError={setErr}
        />
      )}

      <div className="auto-list">
        {(tasks ?? []).map((t) => (
          <article
            key={t.slug}
            className={`auto-row ${selected === t.slug ? 'selected' : ''} ${t.active ? '' : 'paused'}`}
            onClick={() => setSelected(selected === t.slug ? null : t.slug)}
          >
            <div className="auto-main">
              <div className="auto-title-line">
                <span className="auto-name">{t.name}</span>
                <span className="auto-trigger">{triggerLabel(t.triggers)}</span>
                {!t.active && <span className="auto-paused-pill">paused</span>}
              </div>
              {t.last_run_error ? (
                <div className="auto-summary error">{t.last_run_error}</div>
              ) : t.last_run_summary ? (
                <div className="auto-summary">{t.last_run_summary}</div>
              ) : (
                <div className="auto-summary faint">hasn't run yet</div>
              )}
              {t.last_run_at && (
                <div className="auto-when">last ran {formatWhen(t.last_run_at)}</div>
              )}
            </div>
            <div className="auto-actions" onClick={(e) => e.stopPropagation()}>
              <button type="button" className="chip" disabled={busy} onClick={() => void runNow(t.slug)}>
                run now
              </button>
              <button type="button" className="chip muted" onClick={() => void toggle(t)}>
                {t.active ? 'pause' : 'resume'}
              </button>
            </div>
          </article>
        ))}
        {tasks && tasks.length === 0 && !creating && (
          <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>
            No automations yet. Try "Every morning, keep a digest of new wiki articles about AI agents."
          </p>
        )}
      </div>

      {detail && selected && (
        <DetailPanel detail={detail} onSaved={() => { load(); void api.automations.get(selected).then(setDetail); }} onError={setErr}/>
      )}
    </div>
  );
}

function CreateForm({ onCreated, onError }: {
  onCreated: (slug: string) => void;
  onError: (msg: string) => void;
}) {
  const [name, setName] = useState('');
  const [instructions, setInstructions] = useState('');
  const [schedule, setSchedule] = useState<'morning' | 'evening' | 'hourly' | 'manual'>('morning');
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!name.trim() || !instructions.trim()) return;
    setSaving(true);
    try {
      const created = await api.automations.create({
        name: name.trim(),
        instructions: instructions.trim(),
        triggers: PRESETS[schedule],
      });
      onCreated(created.slug);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="auto-create">
      <label className="auto-label" htmlFor="auto-create-name">Name</label>
      <input
        id="auto-create-name"
        type="text"
        placeholder="e.g. Morning agent digest"
        value={name}
        maxLength={120}
        onChange={(e) => setName(e.target.value)}
      />
      <label className="auto-label" htmlFor="auto-create-instructions">
        What should it keep doing? In your words.
      </label>
      <textarea
        id="auto-create-instructions"
        placeholder={'e.g. "Maintain a digest of new wiki articles about AI agents. Lead with what changed since yesterday; notify me only for major releases."'}
        value={instructions}
        rows={4}
        maxLength={8000}
        onChange={(e) => setInstructions(e.target.value)}
      />
      <div className="auto-create-foot">
        <div className="auto-presets" role="radiogroup" aria-label="Schedule">
          {(Object.keys(PRESETS) as (keyof typeof PRESETS)[]).map((key) => (
            <button
              key={key}
              type="button"
              className={`chip ${schedule === key ? '' : 'muted'}`}
              aria-pressed={schedule === key}
              onClick={() => setSchedule(key)}
            >
              {PRESET_LABELS[key]}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="chip"
          disabled={saving || !name.trim() || !instructions.trim()}
          onClick={() => void save()}
        >
          {saving ? 'creating…' : 'create'}
        </button>
      </div>
    </div>
  );
}

function DetailPanel({ detail, onSaved, onError }: {
  detail: AutomationDetail;
  onSaved: () => void;
  onError: (msg: string) => void;
}) {
  const [instructions, setInstructions] = useState(detail.instructions);
  const [saving, setSaving] = useState(false);
  useEffect(() => { setInstructions(detail.instructions); }, [detail.slug, detail.instructions]);

  const dirty = instructions.trim() !== detail.instructions;

  async function save() {
    setSaving(true);
    try {
      await api.automations.patch(detail.slug, { instructions: instructions.trim() });
      onSaved();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="auto-detail">
      <h2 className="auto-detail-h" id="auto-detail-instructions-label">Instructions</h2>
      <textarea
        aria-labelledby="auto-detail-instructions-label"
        value={instructions}
        rows={4}
        maxLength={8000}
        onChange={(e) => setInstructions(e.target.value)}
      />
      {dirty && (
        <button type="button" className="chip" disabled={saving} onClick={() => void save()}>
          {saving ? 'saving…' : 'save instructions'}
        </button>
      )}

      <h2 className="auto-detail-h">Latest output</h2>
      <pre className="auto-index">{detail.index_md || '(empty)'}</pre>

      <h2 className="auto-detail-h">Runs</h2>
      {detail.runs.length === 0 ? (
        <p style={{color:'var(--fg-faint)',fontFamily:'var(--mono)',fontSize:12}}>none yet</p>
      ) : (
        <div className="auto-runs">
          {detail.runs.map((r) => (
            <div key={r.id} className={`auto-run ${r.error ? 'error' : ''}`}>
              <span className="auto-run-when">{formatWhen(r.started_at)}</span>
              <span className="auto-run-trigger">{r.trigger}</span>
              <span className="auto-run-summary">{r.error || r.summary || r.mode || ''}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const PRESETS: Record<string, AutomationTriggers | undefined> = {
  morning: { windows: [{ start: '06:00', end: '10:00' }] },
  evening: { windows: [{ start: '18:00', end: '22:00' }] },
  hourly: { cron: '0 * * * *' },
  manual: undefined,
};

const PRESET_LABELS: Record<string, string> = {
  morning: 'every morning',
  evening: 'every evening',
  hourly: 'hourly',
  manual: 'manual only',
};

function triggerLabel(triggers: AutomationTriggers): string {
  if (triggers.cron) return `cron ${triggers.cron}`;
  if (triggers.windows?.length) {
    return triggers.windows.map((w) => `daily ${w.start}–${w.end}`).join(', ');
  }
  return 'manual';
}

function formatWhen(iso: string): string {
  const date = new Date(iso.includes('T') || iso.includes('+') ? iso : iso + 'Z');
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
