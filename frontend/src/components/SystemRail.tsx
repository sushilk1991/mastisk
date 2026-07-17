import { useEffect, useState } from 'react';
import { api } from '../api';
import type { AgentInfo, CalendarStatus, FeedTick, IntegrationHealth, View } from '../types';
import { CalendarPicker } from './CalendarPicker';

interface Props {
  view: View;
  feed: FeedTick[];
  agents: AgentInfo[];
  selectedDate: string | null;
  onNavigate: (view: View, id?: string) => void;
}

const JUMPS: { id: View; l: string; d: string }[] = [
  { id: 'today',          l: 'Today',          d: 'Tasks, routines, log' },
  { id: 'tasks',          l: 'Tasks',          d: 'Due and open loops' },
  { id: 'projects',       l: 'Projects',       d: 'Areas and logs' },
  { id: 'routines',       l: 'Routines',       d: 'Streaks' },
  { id: 'journal',        l: 'Journal',        d: 'Timeline' },
  { id: 'inventory',      l: 'Inventory',      d: 'Possessions' },
  { id: 'content',        l: 'Content',        d: 'Draft pipeline' },
  { id: 'library',        l: 'Library',        d: 'Books and quotes' },
  { id: 'inbox_triage',   l: 'Inbox triage',   d: 'Needs classification' },
  { id: 'queue',          l: 'Reading queue',  d: 'Jobs & ingest' },
  { id: 'open_questions', l: 'Open questions', d: 'Unresolved threads' },
  { id: 'suggestions',    l: 'Suggestions',    d: 'Topics awaiting a page' },
  { id: 'automations',    l: 'Automations',    d: 'Prose-defined agents' },
  { id: 'graph',          l: 'Graph view',     d: 'Browse the wiki' },
  { id: 'agents',         l: 'Agents',         d: 'Live activity' },
  { id: 'settings',       l: 'Settings',       d: 'Models & keys' },
];

export function SystemRail({ feed, agents, selectedDate, onNavigate }: Props) {
  const [calendar, setCalendar] = useState<CalendarStatus | null>(null);
  const [integrations, setIntegrations] = useState<IntegrationHealth | null>(null);
  const [calendarBusy, setCalendarBusy] = useState(false);
  const [calendarErr, setCalendarErr] = useState<string | null>(null);

  async function loadCalendarStatus(options: { clearError?: boolean } = {}) {
    if (options.clearError !== false) setCalendarErr(null);
    try {
      const [status, health] = await Promise.all([
        api.calendar.status(),
        api.integrations.health(),
      ]);
      setIntegrations(health);
      setCalendar({
        ...health.calendar,
        credentials_configured: status.credentials_configured,
      });
    } catch (e) {
      setCalendarErr(e instanceof Error ? e.message : 'calendar status failed');
    }
  }

  async function syncCalendar() {
    setCalendarBusy(true);
    setCalendarErr(null);
    try {
      const result = await api.calendar.sync();
      setCalendar(result.status);
      window.dispatchEvent(new Event('mastisk-calendar-sync'));
    } catch (e) {
      setCalendarErr(e instanceof Error ? e.message : 'calendar sync failed');
      await loadCalendarStatus({ clearError: false });
    } finally {
      setCalendarBusy(false);
    }
  }

  async function connectCalendar() {
    const popup = window.open('', 'mastisk-calendar-oauth', 'popup,width=560,height=720');
    if (!popup) {
      setCalendarErr('Allow pop-ups for Mastisk, then try connecting again.');
      return;
    }
    setCalendarBusy(true);
    setCalendarErr(null);
    try {
      const result = await api.calendar.startConnection();
      popup.location.href = result.authorization_url;
    } catch (e) {
      popup.close();
      setCalendarErr(e instanceof Error ? e.message : 'calendar connection failed');
    } finally {
      setCalendarBusy(false);
    }
  }

  async function disconnectCalendar() {
    setCalendarBusy(true);
    setCalendarErr(null);
    try {
      await api.calendar.disconnect();
      await loadCalendarStatus();
      window.dispatchEvent(new Event('mastisk-calendar-sync'));
    } catch (e) {
      setCalendarErr(e instanceof Error ? e.message : 'calendar disconnect failed');
    } finally {
      setCalendarBusy(false);
    }
  }

  useEffect(() => {
    void loadCalendarStatus();
    const connected = (event: MessageEvent) => {
      if (event.data?.type !== 'mastisk-calendar-connected') return;
      void loadCalendarStatus();
      window.dispatchEvent(new Event('mastisk-calendar-sync'));
    };
    window.addEventListener('message', connected);
    return () => window.removeEventListener('message', connected);
  }, []);

  return (
    <aside className="rail">
      <div className="rail-section">
        <div className="rail-h">Digest archive</div>
        <CalendarPicker
          selectedDate={selectedDate}
          onSelect={(iso) => onNavigate('digest', iso)}
        />
        <p className="rail-calendar-note">Choose a marked day to revisit what your agents surfaced.</p>
      </div>

      <div className="rail-section">
        <div className="rail-h">Calendar connection</div>
        <div className="rel-row" style={{flexDirection:'column',alignItems:'flex-start',gap:4}}>
          <div style={{color:'var(--fg)',fontSize:13}}>
            {calendar ? calendar.status : 'loading'}
          </div>
          {calendar?.last_synced_at && (
            <div style={{fontFamily:'var(--mono)',fontSize:10,color:'var(--fg-faint)'}}>
              synced {formatRailDate(calendar.last_synced_at)}
            </div>
          )}
          {calendar?.status === 'unconfigured' && (
            <div style={{fontSize:11,color:'var(--fg-faint)',lineHeight:1.5}}>
              Connect read-only Google Calendar to see events in Today.
            </div>
          )}
          {(calendar?.status === 'disconnected' || calendarErr || calendar?.last_error) && (
            <div style={{fontSize:11,color:'#c53030'}}>
              {calendarErr || calendar?.last_error || calendar?.error || 'OAuth expired'}
            </div>
          )}
          {calendar?.last_error_at && (
            <div style={{fontFamily:'var(--mono)',fontSize:10,color:'var(--fg-faint)'}}>
              error {formatRailDate(calendar.last_error_at)}
            </div>
          )}
          <div style={{display:'flex',gap:6,flexWrap:'wrap'}}>
            {calendar?.status === 'unconfigured' ? (
              calendar.credentials_configured ? (
                <button className="chip" disabled={calendarBusy} onClick={() => void connectCalendar()}>
                  {calendarBusy ? 'opening…' : 'connect'}
                </button>
              ) : (
                <button className="chip" onClick={() => onNavigate('today')}>set up in Today</button>
              )
            ) : (
              <button className="chip" disabled={calendarBusy} onClick={() => void syncCalendar()}>
                {calendarBusy ? 'syncing' : 'sync'}
              </button>
            )}
            {calendar && calendar.status !== 'unconfigured' && (
              <button className="chip muted" disabled={calendarBusy} onClick={() => void disconnectCalendar()}>
                disconnect
              </button>
            )}
          </div>
        </div>
      </div>

      <div className="rail-section">
        <div className="rail-h">Integrations</div>
        <div className="rel-row" style={{flexDirection:'column',alignItems:'flex-start',gap:4}}>
          <div style={{fontSize:13,color:'var(--fg)'}}>
            push: {integrations ? `${integrations.push.backend}${integrations.push.configured ? '' : ' (off)'}` : 'loading'}
          </div>
          {!!integrations?.push.notify_failed_last_24h && (
            <div style={{fontSize:11,color:'#c53030'}}>
              {integrations.push.notify_failed_last_24h} push failures in 24h
            </div>
          )}
          {integrations && (
            <div style={{fontFamily:'var(--mono)',fontSize:10,color:'var(--fg-faint)'}}>
              claude {integrations.bridges.claude.available ? 'ok' : 'missing'} · ollama {integrations.bridges.ollama_chat.model}
            </div>
          )}
          {integrations && (
            <div style={{fontFamily:'var(--mono)',fontSize:10,color:'var(--fg-faint)'}}>
              ingest {integrations.ingest.markitdown || integrations.ingest.docling ? 'docs' : 'no-docs'} · whisper {integrations.ingest.mlx_whisper ? 'ok' : 'missing'}
            </div>
          )}
        </div>
      </div>

      <div className="rail-section">
        <div className="rail-h">Jump to</div>
        {JUMPS.map((j) => (
          <div key={j.id} className="rel-row"
            onClick={() => onNavigate(j.id)}
            style={{flexDirection:'column',alignItems:'flex-start',gap:2}}
          >
            <div style={{color:'var(--fg)',fontSize:13}}>{j.l}</div>
            <div style={{fontFamily:'var(--mono)',fontSize:10,color:'var(--fg-faint)'}}>{j.d}</div>
          </div>
        ))}
      </div>

      <div className="rail-section">
        <div className="rail-h">Live feed
          <span style={{display:'inline-flex',alignItems:'center',gap:5,fontSize:9,color:'var(--accent)'}}>
            <span style={{width:5,height:5,borderRadius:'50%',background:'var(--accent)',animation:'pulse 1.6s infinite'}}/>
            LIVE
          </span>
        </div>
        {feed.slice(0, 8).map((f, i) => (
          <div key={i} className="tick-row">
            <div className="tick-time">{f.t}</div>
            <div className="tick-body">
              <span className={`tick-agent ${agents.find((a) => a.id === f.agent)?.color || ''}`}>{f.agent}</span>
              <span className="tick-verb"> {f.verb}</span>{' '}
              <span className="tick-obj">{f.obj}</span>
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}

function formatRailDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
