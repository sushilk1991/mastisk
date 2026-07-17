import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';

interface Props {
  selectedDate: string | null;
  onSelect: (iso: string) => void;
}

const WEEKDAYS = [
  ['S', 'Sunday'], ['M', 'Monday'], ['T', 'Tuesday'], ['W', 'Wednesday'],
  ['T', 'Thursday'], ['F', 'Friday'], ['S', 'Saturday'],
] as const;
const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

function pad(n: number) { return n < 10 ? `0${n}` : `${n}`; }
function iso(y: number, m: number, d: number) { return `${y}-${pad(m)}-${pad(d)}`; }

function parseIso(s: string | null): { y: number; m: number } | null {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m] = s.split('-').map(Number);
  return { y, m };
}

function todayLocal(): { y: number; m: number; d: number } {
  const now = new Date();
  return { y: now.getFullYear(), m: now.getMonth() + 1, d: now.getDate() };
}

export function CalendarPicker({ selectedDate, onSelect }: Props) {
  const today = useMemo(todayLocal, []);
  const initial = parseIso(selectedDate) ?? { y: today.y, m: today.m };
  const [{ y, m }, setYm] = useState<{ y: number; m: number }>(initial);
  const [active, setActive] = useState<Set<string>>(new Set());

  // Snap to the month of the selected date when it changes (e.g. user clicks ← → arrows).
  useEffect(() => {
    const p = parseIso(selectedDate);
    if (p && (p.y !== y || p.m !== m)) setYm(p);
    // Intentionally exclude y/m — we only snap on selectedDate changes, not on user month navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate]);

  useEffect(() => {
    let cancelled = false;
    api.digestCalendar(y, m)
      .then((res) => { if (!cancelled) setActive(new Set(res.active_dates)); })
      .catch(() => { if (!cancelled) setActive(new Set()); });
    return () => { cancelled = true; };
  }, [y, m, selectedDate]);

  const firstDow = new Date(y, m - 1, 1).getDay(); // 0=Sun
  const daysInMonth = new Date(y, m, 0).getDate();
  const cells: Array<number | null> = [
    ...Array(firstDow).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];
  while (cells.length % 7 !== 0) cells.push(null);

  const goMonth = (delta: number) => {
    const d = new Date(y, m - 1 + delta, 1);
    setYm({ y: d.getFullYear(), m: d.getMonth() + 1 });
  };

  return (
    <div className="cal">
      <div className="cal-head">
        <button type="button" className="cal-nav" aria-label="Previous month" onClick={() => goMonth(-1)}>‹</button>
        <span className="cal-title">{MONTH_NAMES[m - 1]} {y}</span>
        <button type="button" className="cal-nav" aria-label="Next month" onClick={() => goMonth(1)}>›</button>
      </div>
      <div className="cal-dow">
        {WEEKDAYS.map(([short, full]) => <span key={full} title={full}>{short}</span>)}
      </div>
      <div className="cal-grid">
        {cells.map((day, i) => {
          if (day === null) return <span key={i} className="cal-cell cal-empty"/>;
          const dateIso = iso(y, m, day);
          const isToday = y === today.y && m === today.m && day === today.d;
          const isSelected = selectedDate === dateIso;
          const hasActivity = active.has(dateIso);
          const classes = ['cal-cell'];
          if (isToday) classes.push('cal-today');
          if (isSelected) classes.push('cal-selected');
          if (hasActivity) classes.push('cal-active');
          else classes.push('cal-inactive');
          return (
            <button
              key={i}
              type="button"
              className={classes.join(' ')}
              disabled={!hasActivity}
              onClick={() => hasActivity && onSelect(dateIso)}
              aria-current={isToday ? 'date' : undefined}
              aria-pressed={isSelected}
              aria-label={`${MONTH_NAMES[m - 1]} ${day}, ${y}${isToday ? ', today' : ''}${hasActivity ? ', digest ready' : ', no digest'}${isSelected ? ', selected' : ''}`}
              title={hasActivity ? `Open the ${dateIso} digest` : `No digest for ${dateIso}`}
            >
              <span className="cal-num">{day}</span>
              {hasActivity && <span className="cal-dot" aria-hidden="true"/>}
            </button>
          );
        })}
      </div>
      <div className="cal-legend" aria-label="Calendar legend">
        <span><i className="today"/>Today</span>
        <span><i className="digest"/>Digest ready</span>
      </div>
    </div>
  );
}
