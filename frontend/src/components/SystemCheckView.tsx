import { useEffect, useState } from 'react';
import { api } from '../api';

type Stats = Awaited<ReturnType<typeof api.stats>>;
type Ping  = Awaited<ReturnType<typeof api.pingBridges>>;

export function SystemCheckView() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [ping, setPing]   = useState<Ping | null>(null);
  const [busy, setBusy]   = useState(false);

  useEffect(() => { void api.stats().then(setStats); }, []);

  const runPing = async () => {
    setBusy(true); setPing(null);
    try {
      setPing(await api.pingBridges());
    } catch (e) {
      alert(String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!stats) return <div className="view"><p style={{color:'var(--fg-faint)'}}>loading…</p></div>;

  return (
    <div className="view">
      <div className="view-h">System · Health check</div>
      <h1 className="view-title">Is everything wired up?</h1>
      <p className="view-sub">
        Live snapshot of your DB, agent activity, identity files, and LLM bridges.
      </p>

      <section style={sectionStyle}>
        <h2 style={h2Style}>Content</h2>
        <KV k="Articles"      v={stats.counts.articles}/>
        <KV k="Sources"       v={stats.counts.sources}/>
        <KV k="Links"         v={stats.counts.links}/>
        <KV k="Feed entries"  v={stats.counts.feed_entries}/>
        <KV k="Signals"       v={stats.counts.signals}/>
      </section>

      <section style={sectionStyle}>
        <h2 style={h2Style}>Agents</h2>
        <KV k="Feeds enabled" v={stats.feeds_enabled}/>
        <KV k="Jobs"          v={formatJobs(stats.jobs)}/>
        <KV k="Last feed fetch"    v={stats.last_feed_fetch || 'never'}/>
        <KV k="Last agent activity" v={stats.last_agent_activity || 'never'}/>
      </section>

      <section style={sectionStyle}>
        <h2 style={h2Style}>Identity (vault/_self)</h2>
        {Object.entries(stats.self_files).map(([name, present]) => (
          <KV key={name} k={`${name}.md`} v={present ? '✓ present' : '✗ missing'}
              ok={present}/>
        ))}
      </section>

      <section style={sectionStyle}>
        <h2 style={h2Style}>Vault</h2>
        <KV k="Path"   v={stats.vault.path}/>
        <KV k="iCloud" v={stats.vault.icloud ? '✓ synced via iCloud Drive' : 'local only'} ok={stats.vault.icloud}/>
      </section>

      <section style={sectionStyle}>
        <h2 style={h2Style}>LLM configuration</h2>
        {Object.entries(stats.llm).map(([k, v]) => (
          <KV key={k} k={k} v={String(v)}/>
        ))}
      </section>

      <section style={sectionStyle}>
        <h2 style={h2Style}>Bridge ping</h2>
        <p style={{color:'var(--fg-mute)',fontSize:13,margin:'0 0 12px'}}>
          Tests that Claude and Ollama actually respond. Takes ~10–20 seconds.
        </p>
        <button onClick={runPing} disabled={busy} style={{
          padding:'9px 16px', borderRadius:6,
          background: busy ? 'var(--bg-sunk)' : 'var(--accent)',
          color: busy ? 'var(--fg-faint)' : 'var(--fg-inv)',
          fontSize:13, fontWeight:500,
          cursor: busy ? 'wait' : 'pointer',
        }}>{busy ? 'pinging…' : 'Run bridge ping'}</button>
        {ping && (
          <div style={{marginTop:16}}>
            <KV k="Claude"        v={ping.claude.ok ? `✓ ${ping.claude.sample}` : `✗ ${ping.claude.error}`} ok={ping.claude.ok}/>
            <KV k="Ollama chat"   v={ping.ollama_chat.ok ? `✓ ${ping.ollama_chat.sample}` : `✗ ${ping.ollama_chat.error}`} ok={ping.ollama_chat.ok}/>
            <KV k="Ollama embed"  v={ping.ollama_embed.ok ? `✓ dim=${ping.ollama_embed.dim}` : `✗ ${ping.ollama_embed.error}`} ok={ping.ollama_embed.ok}/>
          </div>
        )}
      </section>
    </div>
  );
}

// `{done: 29508, failed: 661}` → "29,508 done · 661 failed"
function formatJobs(jobs: unknown): string {
  if (!jobs || typeof jobs !== 'object') return '—';
  const parts = Object.entries(jobs as Record<string, unknown>)
    .map(([status, count]) => `${typeof count === 'number' ? count.toLocaleString() : String(count)} ${status}`);
  return parts.length ? parts.join(' · ') : '—';
}

function KV({ k, v, ok }: { k: string; v: unknown; ok?: boolean }) {
  const color = ok === false ? 'var(--danger)' : ok === true ? 'var(--kind-source)' : 'var(--fg)';
  return (
    <div style={{display:'grid',gridTemplateColumns:'180px 1fr',gap:12,padding:'6px 0',borderTop:'1px solid var(--line-soft)',alignItems:'baseline'}}>
      <div style={{fontFamily:'var(--mono)',fontSize:11,color:'var(--fg-faint)',textTransform:'uppercase',letterSpacing:'0.05em'}}>{k}</div>
      <div style={{fontFamily:'var(--mono)',fontSize:12,color,wordBreak:'break-word'}}>{String(v)}</div>
    </div>
  );
}

const sectionStyle: React.CSSProperties = {
  margin: '24px 0',
  padding: '18px 20px',
  border: '1px solid var(--line-soft)',
  borderRadius: 8,
  background: 'var(--bg-elev)',
};
const h2Style: React.CSSProperties = {
  fontFamily:'var(--mono)', fontSize:11, textTransform:'uppercase', letterSpacing:'0.08em',
  color:'var(--fg-faint)', margin:'0 0 6px',
};
