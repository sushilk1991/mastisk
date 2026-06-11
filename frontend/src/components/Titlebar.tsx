import { Icon } from './icons';
import type { View } from '../types';

const CRUMB: Record<string, readonly string[]> = {
  today:   ['Personal OS', 'Today'],
  digest:  ['Today',  'Daily Digest'],
  article: ['Wiki',   'Concepts'],
  feed:    ['Today',  'Agent feed'],
  agents:  ['System', 'Agents'],
  graph:   ['System', 'Graph view'],
  mobile:  ['System', 'Mobile companion'],
  queue:   ['Today',  'Reading queue'],
  open_questions: ['Today', 'Open questions'],
  ingest:  ['System', 'Sources & ingest'],
  lint:    ['System', 'Health check'],
  settings:['System', 'Settings'],
  tasks: ['Personal OS', 'Tasks'],
  projects: ['Personal OS', 'Projects'],
  routines: ['Personal OS', 'Routines'],
  journal: ['Personal OS', 'Journal'],
  inbox_triage: ['Personal OS', 'Inbox triage'],
  blog:       ['Wiki', 'Blog Posts'],
  blog_post:  ['Wiki', 'Blog Posts', 'Draft'],
  tweets:     ['Wiki', 'Tweet Threads'],
  tweet_thread: ['Wiki', 'Tweet Threads', 'Thread'],
};

interface Props {
  view: View;
  articleTitle?: string;
  articleKind?: string;
  blogPostTitle?: string | null;
  blogPostStatus?: 'pending' | 'running' | 'done' | 'failed' | null;
  tweetThreadTitle?: string | null;
  tweetThreadStatus?: 'pending' | 'running' | 'done' | 'failed' | null;
  theme: 'light' | 'dark';
  onTheme: () => void;
  onToggleSide: () => void;
  onToggleRail: () => void;
  onAsk: () => void;
  onCapture?: () => void;
  onSearchClick: () => void;
}

const KIND_PLURAL: Record<string, string> = {
  Concept: 'Concepts',
  Entity: 'Entities',
  Source: 'Sources',
  Synthesis: 'Synthesis',
};

function buildCrumb(
  view: View,
  articleTitle?: string,
  articleKind?: string,
  blogPostTitle?: string | null,
  blogPostStatus?: 'pending' | 'running' | 'done' | 'failed' | null,
  tweetThreadTitle?: string | null,
  tweetThreadStatus?: 'pending' | 'running' | 'done' | 'failed' | null,
): string[] {
  const base = CRUMB[view] ?? ['Wiki'];
  // Copy — never mutate the module-level const
  const crumb = [...base];
  if (view === 'article') {
    if (articleKind && KIND_PLURAL[articleKind]) crumb[1] = KIND_PLURAL[articleKind];
    if (articleTitle) crumb.push(articleTitle);
  } else if (view === 'blog_post') {
    // Replace the static "Draft" segment with the live title once loaded;
    // before then show an in-progress hint so the crumb doesn't stay stale.
    const trimmed = blogPostTitle?.trim();
    if (trimmed) {
      crumb[crumb.length - 1] = trimmed;
    } else if (blogPostStatus === 'pending' || blogPostStatus === 'running') {
      crumb[crumb.length - 1] = 'Draft in progress';
    } else if (blogPostStatus === 'failed') {
      crumb[crumb.length - 1] = 'Draft failed';
    }
    // On null status (still loading the row) we leave the default "Draft".
  } else if (view === 'tweet_thread') {
    const trimmed = tweetThreadTitle?.trim();
    if (trimmed) {
      crumb[crumb.length - 1] = trimmed;
    } else if (tweetThreadStatus === 'pending' || tweetThreadStatus === 'running') {
      crumb[crumb.length - 1] = 'Thread in progress';
    } else if (tweetThreadStatus === 'failed') {
      crumb[crumb.length - 1] = 'Thread failed';
    }
  }
  return crumb.filter(Boolean);
}

export function Titlebar({
  view, articleTitle, articleKind, blogPostTitle, blogPostStatus,
  tweetThreadTitle, tweetThreadStatus,
  theme, onTheme, onToggleSide, onToggleRail, onAsk, onCapture, onSearchClick,
}: Props) {
  const crumb = buildCrumb(
    view, articleTitle, articleKind, blogPostTitle, blogPostStatus,
    tweetThreadTitle, tweetThreadStatus,
  );

  return (
    <div className="titlebar">
      <div className="tb-crumb">
        <span style={{color:'var(--fg-mute)'}}>mastisk</span>
        {crumb.map((c, i) => (
          <span key={`${view}-${i}-${c}`}>
            <span className="sep"> / </span>{c}
          </span>
        ))}
      </div>
      <div className="tb-search" onClick={onSearchClick} role="button">
        {Icon.search}
        <span style={{flex:1}}>Search wiki, notes, blog...</span>
        <kbd>⌘K</kbd>
      </div>
      <div className="tb-actions">
        <button
          className="tb-btn tb-search-btn"
          onClick={onSearchClick}
          title="Search (⌘K)"
          aria-label="Search"
        >{Icon.search}</button>
        <button className="tb-btn" onClick={onCapture} title="New note (⌘+)">+</button>
        <button className="tb-btn" onClick={onAsk} title="Ask Mastisk">{Icon.ask}</button>
        <button className="tb-btn" onClick={onToggleSide} title="Toggle sidebar">{Icon.panel}</button>
        <button className="tb-btn" onClick={onToggleRail} title="Toggle right rail" style={{transform:'scaleX(-1)'}}>{Icon.panel}</button>
        <button className="tb-btn" onClick={onTheme} title="Toggle theme">
          {theme === 'dark' ? Icon.sun : Icon.moon}
        </button>
      </div>
    </div>
  );
}
