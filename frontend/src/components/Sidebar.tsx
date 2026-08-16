import { useState } from 'react';
import type { VaultItem, PinnedItem, UserInfo, View } from '../types';

interface Props {
  vault: VaultItem[];
  pinned: PinnedItem[];
  user: UserInfo | null;
  currentView: View;
  currentArticle: string;
  onNavigate: (view: View, id?: string) => void;
  onAddRepo: () => void;
  onCaptureNote: () => void;
  onCreateBlog: () => void;
}

const SYS_VIEWS = new Set<View>([
  'today', 'digest', 'queue', 'feed', 'agents', 'graph', 'ingest', 'lint', 'settings',
  'open_questions', 'suggestions', 'automations', 'notes', 'library', 'tasks', 'projects', 'routines', 'journal', 'inbox_triage',
  'people', 'inventory', 'content',
  'roundtables', 'repos', 'blog', 'tweets',
]);

export function Sidebar({ vault, pinned, user, currentView, currentArticle, onNavigate, onAddRepo, onCaptureNote, onCreateBlog }: Props) {
  // Folders are collapsed by default. A label appearing in `opened` with value
  // true means the user has expanded it. Kept in-memory per session — no
  // persistence, since recall of "which sections were open last week" isn't a
  // useful signal and would need a storage contract.
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const toggle = (k: string) => setOpened((s) => ({ ...s, [k]: !s[k] }));

  return (
    <aside className="sidebar">
      {pinned.length > 0 && (
        <>
          <div className="side-section">Pinned</div>
          <div className="side-pin-section">
            {pinned.map((p) => (
              <div key={p.id} className="side-pin" onClick={() => onNavigate('article', p.id)}>
                <div className="dot"/><span>{p.label}</span>
              </div>
            ))}
          </div>
        </>
      )}
      {vault.map((item, i) => {
        if (item.kind === 'section') return <div key={i} className="side-section">{item.label}</div>;
        if (item.kind === 'folder') {
          const open = opened[item.label] === true;
          return (
            <div key={i}>
              <div className={`side-folder ${open ? '' : 'collapsed'}`} onClick={() => toggle(item.label)}>
                <span className="chev">▾</span><span>{item.label}</span><span className="count">{item.count}</span>
              </div>
              {open && (
                <div className="side-children">
                  {item.children.map((c) => <Row key={c.id} item={c} currentArticle={currentArticle} currentView={currentView} onNavigate={onNavigate} />)}
                </div>
              )}
            </div>
          );
        }
        return <Row key={item.id} item={item} currentArticle={currentArticle} currentView={currentView} onNavigate={onNavigate} />;
      })}
      {/* Hand-coded extras (not in backend vault_tree yet) */}
      <div className="side-section">Personal OS</div>
      <SideNavRow currentView={currentView} view="today" glyph="◆" label="Today" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="tasks" glyph="☑" label="Tasks" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="projects" glyph="▣" label="Projects" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="routines" glyph="↻" label="Routines" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="learning" glyph="◈" label="Learning" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="journal" glyph="◷" label="Journal" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="people" glyph="@" label="People" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="inventory" glyph="▤" label="Inventory" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="content" glyph="▥" label="Content" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="library" glyph="§" label="Library" onNavigate={onNavigate}/>
      <SideNavRow currentView={currentView} view="inbox_triage" glyph="?" label="Inbox triage" onNavigate={onNavigate}/>
      <div className="side-section">Wiki</div>
      <div className="side-row-group" style={{ display: 'flex', alignItems: 'center' }}>
        <div
          className={`side-row ${currentView === 'notes' || currentView === 'note' ? 'active' : ''}`}
          onClick={() => onNavigate('notes')}
          style={{ flex: 1 }}
        >
          <span className="glyph">✎</span>
          <span className="label">Notes</span>
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); onCaptureNote(); }}
          title="Capture a note"
          aria-label="Capture a note"
          style={{
            background: 'transparent', border: 'none', color: 'var(--fg-faint)',
            fontSize: 14, cursor: 'pointer', padding: '2px 6px',
          }}
        >
          +
        </button>
      </div>
      <div
        className={`side-row ${currentView === 'roundtables' || currentView === 'roundtable' ? 'active' : ''}`}
        onClick={() => onNavigate('roundtables')}
      >
        <span className="glyph">◎</span>
        <span className="label">Roundtables</span>
      </div>
      <div
        className={`side-row ${currentView === 'podcasts' || currentView === 'podcast' ? 'active' : ''}`}
        onClick={() => onNavigate('podcasts')}
      >
        <span className="glyph">⏵</span>
        <span className="label">Podcasts</span>
      </div>
      <div className="side-row-group" style={{ display: 'flex', alignItems: 'center' }}>
        <div
          className={`side-row ${currentView === 'repos' || currentView === 'repo' ? 'active' : ''}`}
          onClick={() => onNavigate('repos')}
          style={{ flex: 1 }}
        >
          <span className="glyph">⎇</span>
          <span className="label">Repos</span>
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); onAddRepo(); }}
          title="Add a repo"
          aria-label="Add a repo"
          style={{
            background: 'transparent', border: 'none', color: 'var(--fg-faint)',
            fontSize: 14, cursor: 'pointer', padding: '2px 6px',
          }}
        >
          +
        </button>
      </div>
      <div className="side-row-group" style={{ display: 'flex', alignItems: 'center' }}>
        <div
          className={`side-row ${currentView === 'blog' || currentView === 'blog_post' ? 'active' : ''}`}
          onClick={() => onNavigate('blog')}
          style={{ flex: 1 }}
        >
          <span className="glyph">✒</span>
          <span className="label">Blog Posts</span>
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); onCreateBlog(); }}
          title="Draft a blog post"
          aria-label="Draft a blog post"
          style={{
            background: 'transparent', border: 'none', color: 'var(--fg-faint)',
            fontSize: 14, cursor: 'pointer', padding: '2px 6px',
          }}
        >
          +
        </button>
      </div>
      <div
        className={`side-row ${currentView === 'tweets' || currentView === 'tweet_thread' ? 'active' : ''}`}
        onClick={() => onNavigate('tweets')}
      >
        <span className="glyph">#</span>
        <span className="label">Tweet Threads</span>
      </div>
      {user && (
        <div className="user-pill" onClick={() => onNavigate('ingest')} role="button" title="Import">
          <div className="user-avatar">{user.initials}</div>
          <div className="user-meta">
            <div className="user-name">{user.name}</div>
            <div className="user-sub">
              {user.stats.pages} {user.stats.pages === 1 ? 'page' : 'pages'}
              {user.stats.sources > 0 && ` · ${user.stats.sources} sources`}
              {user.stats.feeds > 0 && ` · ${user.stats.feeds} feeds`}
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}

function SideNavRow({
  currentView, view, glyph, label, onNavigate,
}: {
  currentView: View;
  view: View;
  glyph: string;
  label: string;
  onNavigate: Props['onNavigate'];
}) {
  return (
    <div
      className={`side-row ${currentView === view ? 'active' : ''}`}
      onClick={() => onNavigate(view)}
    >
      <span className="glyph">{glyph}</span>
      <span className="label">{label}</span>
    </div>
  );
}

function Row({ item, currentArticle, currentView, onNavigate }: { item: any; currentArticle: string; currentView: View; onNavigate: Props['onNavigate']; }) {
  const active = item.id === currentArticle || (item.id && item.id === currentView);
  const click = () => {
    if (SYS_VIEWS.has(item.id)) onNavigate(item.id as View);
    else onNavigate('article', item.id);
  };
  return (
    <div className={`side-row ${active ? 'active' : ''}`} onClick={click}>
      <span className="glyph">{item.glyph}</span>
      <span className="label">{item.label}</span>
      {item.badge && <span className={`badge ${item.badge === 'live' ? 'live' : ''}`}>{item.badge}</span>}
      {item.hot && <span style={{color:'var(--accent)', fontSize:8}}>●</span>}
    </div>
  );
}
