import type { AgentInfo, FeedTick, View } from '../types';
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
  { id: 'inbox_triage',   l: 'Inbox triage',   d: 'Needs classification' },
  { id: 'digest',         l: 'Daily Digest',   d: 'Agent reading' },
  { id: 'queue',          l: 'Reading queue',  d: 'Jobs & ingest' },
  { id: 'open_questions', l: 'Open questions', d: 'Unresolved threads' },
  { id: 'graph',          l: 'Graph view',     d: 'Browse the wiki' },
  { id: 'agents',         l: 'Agents',         d: 'Live activity' },
  { id: 'settings',       l: 'Settings',       d: 'Models & keys' },
];

export function SystemRail({ feed, agents, selectedDate, onNavigate }: Props) {
  return (
    <aside className="rail">
      <div className="rail-section">
        <div className="rail-h">Calendar</div>
        <CalendarPicker
          selectedDate={selectedDate}
          onSelect={(iso) => onNavigate('digest', iso)}
        />
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
