import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, FormEvent, ReactNode } from 'react';
import { api, FocusFullError } from '../api';
import { MarkdownBlock, VaultMarkdownEditor, type VaultEditorTarget } from './MarkdownEditor';
import type {
  CalendarToday, CaptureTriageItem, CaptureTriageTarget, Domain, JournalDay, JournalDaySummary,
  ChecklistTemplate, ContentDetail, ContentKind, ContentList, ContentStatus, ContentSummary, InventoryDetail, InventoryStatus, InventorySummary, NeedsReviewItem, PersonDetail, PersonSummary, Priority, ProjectDetail, ProjectSummary, ReminderRow, ResurfaceItem,
  RoutineGroups, RoutineProgress, RoutineRow, SlippingItem, TaskRow, TimeOfDay, View,
} from '../types';

interface LiveProps {
  liveKey: string;
}

interface NavProps {
  onNavigate: (view: View, id?: string) => void;
}

const TIME_GROUPS: TimeOfDay[] = ['morning', 'afternoon', 'evening', 'anytime'];
const TASK_GROUPS = ['overdue', 'today', 'upcoming', 'someday', 'done'] as const;
const PRIORITIES: { value: '' | 'high' | 'medium' | 'low'; label: string }[] = [
  { value: '', label: '-' },
  { value: 'high', label: 'high' },
  { value: 'medium', label: 'medium' },
  { value: 'low', label: 'low' },
];
const INVENTORY_STATUSES: InventoryStatus[] = ['owned', 'sold', 'discarded'];
const CONTENT_STATUSES: ContentStatus[] = ['idea', 'outline', 'editing', 'waiting', 'published', 'done'];
const CONTENT_KINDS: ContentKind[] = ['video', 'article', 'podcast', 'newsletter'];
type ErrorSetter = (message: string | null) => void;
type ChangeHandler = () => void | Promise<void>;

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : 'failed';
}

function runMutation(action: () => Promise<void>, setErr: ErrorSetter): void {
  setErr(null);
  void action().catch((e) => setErr(errorMessage(e)));
}

export function TodayView({ liveKey, onNavigate }: LiveProps & NavProps) {
  const today = localIsoToday();
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [focus, setFocus] = useState<TaskRow[]>([]);
  const [slipping, setSlipping] = useState<SlippingItem[]>([]);
  const [resurface, setResurface] = useState<ResurfaceItem | null>(null);
  const [routines, setRoutines] = useState<RoutineGroups | null>(null);
  const [journal, setJournal] = useState<JournalDay | null>(null);
  const [reminders, setReminders] = useState<ReminderRow[]>([]);
  const [calendar, setCalendar] = useState<CalendarToday | null>(null);
  const [calendarErr, setCalendarErr] = useState<string | null>(null);
  const [calendarBusy, setCalendarBusy] = useState(false);
  const [entry, setEntry] = useState('');
  const [err, setErr] = useState<string | null>(null);

  async function loadCalendarToday() {
    setCalendarErr(null);
    try {
      setCalendar(await api.calendar.today(today));
    } catch (e) {
      setCalendar(null);
      setCalendarErr(errorMessage(e));
    }
  }

  async function load() {
    setErr(null);
    const calendarLoad = loadCalendarToday();
    try {
      const [taskRows, routineRows, reminderRows, focusRows, slippingRows, resurfaceItem] = await Promise.all([
        api.tasks.list({ status: 'open' }),
        api.routinesApi.list(false),
        api.remindersApi.list(),
        api.focus.list(today),
        api.slipping.list(),
        api.resurface.get(today),
      ]);
      setTasks(taskRows);
      setRoutines(routineRows);
      setReminders(reminderRows);
      setFocus(focusRows);
      setSlipping(slippingRows);
      setResurface(resurfaceItem);
      try {
        setJournal(await api.journalApi.get(today));
      } catch {
        setJournal(null);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed');
    }
    await calendarLoad;
  }

  useEffect(() => { void load(); }, [today, liveKey]);
  useEffect(() => {
    const reload = () => { void load(); };
    window.addEventListener('mastisk-calendar-sync', reload);
    return () => window.removeEventListener('mastisk-calendar-sync', reload);
  }, [today, liveKey]);

  async function syncCalendar() {
    setCalendarBusy(true);
    setCalendarErr(null);
    try {
      await api.calendar.sync();
      window.dispatchEvent(new Event('mastisk-calendar-sync'));
      await loadCalendarToday();
    } catch (e) {
      setCalendarErr(errorMessage(e));
    } finally {
      setCalendarBusy(false);
    }
  }

  async function appendLog(e: FormEvent) {
    e.preventDefault();
    const text = entry.trim();
    if (!text) return;
    setErr(null);
    try {
      await api.journalApi.appendLog(today, text);
      setEntry('');
      await load();
    } catch (err) {
      setErr(errorMessage(err));
    }
  }

  const dueTasks = tasks
    .filter((task) => task.status === 'open' && task.due && datePart(task.due) <= today)
    .sort(compareTasksByDue);
  const pendingReminders = reminders.filter((r) => r.status === 'pending' && localDatePart(r.fire_at) <= today);
  const firedToday = reminders.filter((r) => ['sent', 'late', 'notify_failed'].includes(r.status) && localDatePart(r.fired_at || r.fire_at) === today);
  const logLines = sectionLines(journal?.sections?.Log).slice(-4);
  const focusedUids = new Set(focus.map((task) => task.uid).filter(Boolean));

  return (
    <div className="view dash-view">
      <div className="view-h">Personal OS</div>
      <h1 className="view-title">Today</h1>
      <p className="view-sub">{formatLongDate(today)}</p>

      {err && <p className="dash-error">{err}</p>}

      <div className="dash-grid dash-grid-2">
        <FocusPanel focus={focus} today={today} onChanged={load}/>
        <CalendarPanel
          calendar={calendar}
          calendarErr={calendarErr}
          calendarBusy={calendarBusy}
          onSync={syncCalendar}
        />
      </div>

      {resurface && <ResurfaceCard item={resurface}/>}

      <section className="dash-section">
        <div className="dash-section-head">
          <h2>Due now</h2>
          <button className="chip" onClick={() => onNavigate('tasks')}>all tasks</button>
        </div>
        {dueTasks.length === 0 ? (
          <EmptyLine>No due or overdue open tasks.</EmptyLine>
        ) : (
          <div className="dash-list">
            {dueTasks.map((task) => (
              <TaskLine
                key={task.uid}
                task={task}
                today={today}
                onChanged={load}
                focusDate={today}
                focused={focusedUids.has(task.uid)}
              />
            ))}
          </div>
        )}
      </section>

      {slipping.length > 0 && <SlippingRail items={slipping} onChanged={load}/>}

      <section className="dash-section">
        <div className="dash-section-head">
          <h2>Routines</h2>
          <button className="chip" onClick={() => onNavigate('routines')}>all routines</button>
        </div>
        {!routines || allRoutines(routines).length === 0 ? (
          <EmptyLine>No routines yet.</EmptyLine>
        ) : (
          TIME_GROUPS.map((group) => routines[group]?.length ? (
            <RoutineGroup key={group} label={group} routines={routines[group]} onChanged={load}/>
          ) : null)
        )}
      </section>

      <section className="dash-section">
        <div className="dash-section-head">
          <h2>Journal log</h2>
          <button className="chip" onClick={() => onNavigate('journal')}>timeline</button>
        </div>
        {logLines.length === 0 ? (
          <EmptyLine>No journal entries today.</EmptyLine>
        ) : (
          <div className="dash-list compact">
            {logLines.map((line, idx) => <div key={`${line}-${idx}`} className="dash-row">{line}</div>)}
          </div>
        )}
        <form className="dash-inline-form" onSubmit={appendLog}>
          <input value={entry} onChange={(e) => setEntry(e.target.value)} placeholder="Append to today's log" />
          <button type="submit">Add</button>
        </form>
      </section>

      <section className="dash-section">
        <div className="dash-section-head">
          <h2>Reminders</h2>
        </div>
        {[...pendingReminders, ...firedToday].length === 0 ? (
          <EmptyLine>No pending or fired reminders today.</EmptyLine>
        ) : (
          <div className="dash-strip">
            {[...pendingReminders, ...firedToday].slice(0, 8).map((reminder) => (
              <div key={reminder.id} className={`dash-pill ${reminder.status === 'late' ? 'warn' : ''}`}>
                <span>{formatTime(reminder.fire_at)}</span>
                <b>{reminder.title || reminder.kind}</b>
                <em>{reminder.status}</em>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

export function TasksView({ liveKey }: LiveProps) {
  const today = localIsoToday();
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [focus, setFocus] = useState<TaskRow[]>([]);
  const [domains, setDomains] = useState<Domain[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [status, setStatus] = useState('open');
  const [domain, setDomain] = useState('');
  const [project, setProject] = useState('');
  const [dueWindow, setDueWindow] = useState('all');
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    setErr(null);
    try {
      const [taskRows, domainRows, projectRows] = await Promise.all([
        api.tasks.list(status === 'all' ? {} : { status }),
        api.domainsApi.list(),
        api.projectsApi.list(),
      ]);
      const focusRows = await api.focus.list(today);
      setTasks(taskRows);
      setFocus(focusRows);
      setDomains(domainRows);
      setProjects(projectRows);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed');
    }
  }

  useEffect(() => { void load(); }, [status, liveKey]);

  const filtered = useMemo(() => tasks
    .filter((task) => !domain || task.domain === domain)
    .filter((task) => !project || task.project === project)
    .filter((task) => dueWindow === 'all' || taskBucket(task, today) === dueWindow)
    .sort(compareTasksByDue), [tasks, domain, project, dueWindow, today]);
  const grouped = groupTasks(filtered, today);
  const focusedUids = new Set(focus.map((task) => task.uid).filter(Boolean));

  return (
    <div className="view dash-view">
      <div className="view-h">Personal OS</div>
      <h1 className="view-title">Tasks</h1>
      <div className="dash-filters">
        <Select label="Status" value={status} onChange={setStatus} options={[
          ['open', 'open'], ['done', 'done'], ['all', 'all'],
        ]}/>
        <Select label="Domain" value={domain} onChange={setDomain} options={[
          ['', 'all domains'], ...domains.map((d) => [d.slug, d.name] as [string, string]),
        ]}/>
        <Select label="Project" value={project} onChange={setProject} options={[
          ['', 'all projects'], ...projects.map((p) => [p.slug, p.name] as [string, string]),
        ]}/>
        <Select label="Due" value={dueWindow} onChange={setDueWindow} options={[
          ['all', 'all'], ['overdue', 'overdue'], ['today', 'today'], ['upcoming', 'upcoming'], ['someday', 'someday'], ['done', 'done'],
        ]}/>
      </div>
      {err && <p className="dash-error">{err}</p>}
      {filtered.length === 0 ? (
        <EmptyLine>No tasks match these filters.</EmptyLine>
      ) : TASK_GROUPS.map((group) => (
        <TaskGroup
          key={group}
          title={group}
          tasks={grouped[group]}
          today={today}
          onChanged={load}
          focusDate={today}
          focusedUids={focusedUids}
        />
      ))}
    </div>
  );
}

export function ProjectsView({ liveKey }: LiveProps) {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [templates, setTemplates] = useState<ChecklistTemplate[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [newName, setNewName] = useState('');
  const [newType, setNewType] = useState<'project' | 'area' | 'retainer'>('project');
  const [newTemplate, setNewTemplate] = useState('');
  const [newRecurringItems, setNewRecurringItems] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [projectEditor, setProjectEditor] = useState<VaultEditorTarget | null>(null);
  const selectedRef = useRef<string | null>(null);
  const detailRequestRef = useRef(0);

  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  async function loadList() {
    setErr(null);
    try {
      const [rows, templateRows] = await Promise.all([
        api.projectsApi.list(),
        api.projectsApi.checklistTemplates(),
      ]);
      setProjects(rows);
      setTemplates(templateRows);
      setSelected((current) => current || rows[0]?.slug || null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed');
    }
  }

  async function loadDetail(slug: string | null) {
    const requestId = detailRequestRef.current + 1;
    detailRequestRef.current = requestId;
    if (!slug) {
      setDetail(null);
      setTasks([]);
      return;
    }
    setErr(null);
    try {
      const [projectDetail, openTasks] = await Promise.all([
        api.projectsApi.get(slug),
        api.tasks.list({ status: 'open', project: slug }),
      ]);
      if (detailRequestRef.current !== requestId || selectedRef.current !== slug) return;
      setDetail(projectDetail);
      setTasks(openTasks);
    } catch (e) {
      if (detailRequestRef.current !== requestId || selectedRef.current !== slug) return;
      setErr(errorMessage(e));
    }
  }

  useEffect(() => { void loadList(); }, [liveKey]);
  useEffect(() => { void loadDetail(selected); }, [selected, liveKey]);

  async function patchStatus(status: string) {
    if (!detail) return;
    await api.projectsApi.patch(detail.slug, { status });
    await loadList();
    await loadDetail(detail.slug);
  }

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    const created = await api.projectsApi.create({
      name,
      type: newType,
      template: newTemplate || null,
      recurring_items: newRecurringItems
        .split(/[\n,]/)
        .map((item) => item.trim())
        .filter(Boolean),
    });
    setNewName('');
    setNewType('project');
    setNewTemplate('');
    setNewRecurringItems('');
    await loadList();
    setSelected(created.slug);
  }

  async function addMilestone(text: string) {
    if (!detail) return;
    const slug = detail.slug;
    const updated = await api.projectsApi.addMilestone(slug, text);
    if (selectedRef.current === slug) setDetail(updated);
    await loadList();
  }

  async function setMilestoneDone(position: number, done: boolean, expectedText: string) {
    if (!detail) return;
    const slug = detail.slug;
    const updated = await api.projectsApi.setMilestoneDone(slug, position, done, expectedText);
    if (selectedRef.current === slug) setDetail(updated);
    await loadList();
  }

  async function addTime(hours: number, text: string, entryDate: string) {
    if (!detail) return;
    const slug = detail.slug;
    const updated = await api.projectsApi.addTime(slug, {
      date: entryDate || null,
      hours,
      text,
    });
    if (selectedRef.current === slug) setDetail(updated);
    await loadList();
  }

  return (
    <div className="view dash-view">
      <div className="view-h">Personal OS</div>
      <h1 className="view-title">Projects</h1>
      {err && <p className="dash-error">{err}</p>}
      <form className="dash-inline-form project-create" onSubmit={(event) => runMutation(() => createProject(event), setErr)}>
        <input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="Project name"/>
        <select value={newType} onChange={(event) => setNewType(event.target.value as 'project' | 'area' | 'retainer')}>
          <option value="project">project</option>
          <option value="area">area</option>
          <option value="retainer">retainer</option>
        </select>
        <select value={newTemplate} onChange={(event) => setNewTemplate(event.target.value)}>
          <option value="">no template</option>
          {templates.map((template) => (
            <option key={template.name} value={template.name}>
              {template.name} ({template.task_count})
            </option>
          ))}
        </select>
        {newType === 'retainer' && (
          <input
            value={newRecurringItems}
            onChange={(event) => setNewRecurringItems(event.target.value)}
            placeholder="Recurring items"
          />
        )}
        <button type="submit">create</button>
      </form>
      {projects.length === 0 ? (
        <EmptyLine>No projects yet.</EmptyLine>
      ) : (
        <div className="dash-split">
          <div className="dash-list">
            {projects.map((project) => (
              <button
                key={project.slug}
                className={`dash-card project-card ${selected === project.slug ? 'active' : ''}`}
                onClick={() => setSelected(project.slug)}
              >
                <div className="dash-card-title">{project.name}</div>
                <div className="dash-tags">
                  <span>{project.type}</span>
                  {project.domain && <span>{project.domain}</span>}
                  <span>{project.status}</span>
                  <span>{project.open_task_count} open</span>
                </div>
              </button>
            ))}
          </div>
          <div className="dash-panel">
            {!detail ? <EmptyLine>Select a project.</EmptyLine> : (
              <>
                <div className="dash-section-head">
                  <h2>{detail.name}</h2>
                  <div className="dash-actions">
                    <button className="chip" type="button" onClick={() => setProjectEditor({ path: detail.path, title: detail.name })}>
                      edit
                    </button>
                    <select value={detail.status} onChange={(e) => runMutation(() => patchStatus(e.target.value), setErr)}>
                      {['active', 'paused', 'someday', 'done'].map((s) => <option key={s} value={s}>{s}</option>)}
                    </select>
                  </div>
                </div>
                {detail.type === 'retainer' && <span className="dash-pill">retainer</span>}
                <KeyValueBlock values={detail.frontmatter}/>
                <MilestonesBlock
                  detail={detail}
                  onAdd={(text) => runMutation(() => addMilestone(text), setErr)}
                  onToggle={(position, done, expectedText) => runMutation(() => setMilestoneDone(position, done, expectedText), setErr)}
                />
                <TimeBlock detail={detail} onAdd={(hours, text, entryDate) => runMutation(() => addTime(hours, text, entryDate), setErr)}/>
                {detail.retainer && <RetainerBlock detail={detail}/>}
                <h3 className="dash-mini-h">Open tasks</h3>
                {tasks.length === 0 ? <EmptyLine>No open tasks in this project.</EmptyLine> : (
                  <div className="dash-list compact">
                    {tasks.map((task) => <TaskLine key={task.uid} task={task} today={localIsoToday()} onChanged={() => { void loadDetail(detail.slug); }}/>)}
                  </div>
                )}
                <h3 className="dash-mini-h">Recent log</h3>
                <LogPreview body={detail.body}/>
              </>
            )}
          </div>
        </div>
      )}
      {projectEditor && (
        <VaultMarkdownEditor
          target={projectEditor}
          onClose={() => setProjectEditor(null)}
          onSaved={async () => {
            await loadList();
            await loadDetail(selectedRef.current);
          }}
        />
      )}
    </div>
  );
}

export function RoutinesView({ liveKey }: LiveProps) {
  const [active, setActive] = useState<RoutineGroups | null>(null);
  const [archived, setArchived] = useState<RoutineGroups | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    setErr(null);
    try {
      const [activeRows, archivedRows] = await Promise.all([
        api.routinesApi.list(false),
        api.routinesApi.list(true),
      ]);
      setActive(activeRows);
      setArchived(archivedRows);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed');
    }
  }

  useEffect(() => { void load(); }, [liveKey]);

  const archivedOnly = archived ? allRoutines(archived).filter((r) => r.archived) : [];

  return (
    <div className="view dash-view">
      <div className="view-h">Personal OS</div>
      <h1 className="view-title">Routines</h1>
      {err && <p className="dash-error">{err}</p>}
      {!active || allRoutines(active).length === 0 ? (
        <EmptyLine>No active routines yet.</EmptyLine>
      ) : TIME_GROUPS.map((group) => active[group]?.length ? (
        <section className="dash-section" key={group}>
          <h2>{labelTime(group)}</h2>
          <div className="routine-grid">
            {active[group].map((routine) => <RoutineCard key={routine.slug} routine={routine} liveKey={liveKey} onChanged={load}/>)}
          </div>
        </section>
      ) : null)}

      <section className="dash-section">
        <button className="dash-collapse" onClick={() => setShowArchived((v) => !v)}>
          Archived routines ({archivedOnly.length})
        </button>
        {showArchived && (
          <div className="dash-list compact">
            {archivedOnly.length === 0 ? <EmptyLine>No archived routines.</EmptyLine> : archivedOnly.map((routine) => (
              <div className="dash-row" key={routine.slug}>
                <span>{routine.name}</span>
                <span className="dash-muted">{labelTime(routine.time_of_day)}</span>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

export function JournalView({ liveKey }: LiveProps) {
  const today = localIsoToday();
  const [days, setDays] = useState<JournalDaySummary[]>([]);
  const [selected, setSelected] = useState<string>(today);
  const [detail, setDetail] = useState<JournalDay | null>(null);
  const [entry, setEntry] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [journalEditor, setJournalEditor] = useState<VaultEditorTarget | null>(null);
  const [photoBusy, setPhotoBusy] = useState(false);
  const [photoMsg, setPhotoMsg] = useState<string | null>(null);
  const photoInputRef = useRef<HTMLInputElement | null>(null);

  async function loadDays() {
    setErr(null);
    try {
      const rows = await api.journalApi.list(60);
      setDays(rows);
      if (!selected && rows[0]) setSelected(rows[0].date);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed');
    }
  }

  async function loadDetail(day: string) {
    try {
      setDetail(await api.journalApi.get(day));
    } catch {
      setDetail(null);
    }
  }

  useEffect(() => { void loadDays(); }, [liveKey]);
  useEffect(() => { void loadDetail(selected); }, [selected, liveKey]);

  async function appendLog(e: FormEvent) {
    e.preventDefault();
    const text = entry.trim();
    if (!text) return;
    setErr(null);
    try {
      await api.journalApi.appendLog(selected, text);
      setEntry('');
      await loadDays();
      await loadDetail(selected);
    } catch (err) {
      setErr(errorMessage(err));
    }
  }

  async function uploadJournalPhoto(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.currentTarget.value = '';
    if (!file) return;
    setErr(null);
    setPhotoMsg(null);
    setPhotoBusy(true);
    try {
      const res = await api.ingest.uploadJournalPhoto(file, selected);
      setPhotoMsg(res.ocr_status === 'done' ? 'photo added' : 'photo saved · OCR pending');
      await loadDays();
      await loadDetail(selected);
    } catch (err) {
      setErr(errorMessage(err));
    } finally {
      setPhotoBusy(false);
    }
  }

  async function setScale(kind: 'mood' | 'energy', value: number | null) {
    const updated = await api.journalApi.patch(selected, { [kind]: value });
    setDetail(updated);
    await loadDays();
  }

  return (
    <div className="view dash-view">
      <div className="view-h">Personal OS</div>
      <h1 className="view-title">Journal</h1>
      {err && <p className="dash-error">{err}</p>}
      <div className="dash-split journal-split">
        <div className="dash-list">
          {days.length === 0 ? <EmptyLine>No journal days yet.</EmptyLine> : days.map((day) => (
            <button
              key={day.date}
              className={`dash-card journal-day ${selected === day.date ? 'active' : ''}`}
              onClick={() => setSelected(day.date)}
            >
              <b>{formatLongDate(day.date)}</b>
              <span>{day.log_count} log entries</span>
              <div className="dash-tags">
                {day.mood && <span>mood {day.mood}</span>}
                {day.energy && <span>energy {day.energy}</span>}
                {day.has_reflections && <span>reflections</span>}
              </div>
            </button>
          ))}
        </div>
        <div className="dash-panel">
          <div className="dash-section-head">
            <h2>{formatLongDate(selected)}</h2>
            <div className="dash-actions">
              <button className="chip" onClick={() => setSelected(today)}>today</button>
              <input
                ref={photoInputRef}
                type="file"
                accept="image/png,image/jpeg,image/jpg,image/webp,image/heic"
                onChange={uploadJournalPhoto}
                style={{display:'none'}}
              />
              <button
                className="chip"
                type="button"
                disabled={photoBusy}
                onClick={() => photoInputRef.current?.click()}
              >
                {photoBusy ? 'adding…' : 'photo'}
              </button>
              <button
                className="chip"
                type="button"
                disabled={!detail}
                onClick={() => detail && setJournalEditor({ path: detail.path, title: formatLongDate(selected) })}
              >
                edit
              </button>
            </div>
          </div>
          {photoMsg && <p className="dash-muted">{photoMsg}</p>}
          <ScaleSetter label="Mood" value={detail?.mood ?? null} onSet={(v) => runMutation(() => setScale('mood', v), setErr)}/>
          <ScaleSetter label="Energy" value={detail?.energy ?? null} onSet={(v) => runMutation(() => setScale('energy', v), setErr)}/>
          <form className="dash-inline-form" onSubmit={appendLog}>
            <input value={entry} onChange={(e) => setEntry(e.target.value)} placeholder="Append to log" />
            <button type="submit">Add</button>
          </form>
          {!detail ? (
            <EmptyLine>No entries for this day yet.</EmptyLine>
          ) : (
            <>
              <SectionBlock title="Log" body={detail.sections.Log}/>
              <SectionBlock title="Tasks" body={detail.sections.Tasks}/>
              <SectionBlock title="Reflections" body={detail.sections.Reflections}/>
              <h3 className="dash-mini-h">Day assembly</h3>
              <div className="dash-list compact">
                {detail.tasks.slice(0, 8).map((task) => <TaskLine key={task.uid} task={task} today={selected} onChanged={() => { void loadDetail(selected); }}/>)}
                {detail.routine_completions.map((routine) => (
                  <div className="dash-row" key={`${routine.routine_id}-${routine.date}`}>
                    <span>{routine.name}</span>
                    <span className="dash-muted">{labelTime(routine.time_of_day)}</span>
                  </div>
                ))}
                {detail.tasks.length === 0 && detail.routine_completions.length === 0 && (
                  <EmptyLine>No tasks or routine completions assembled for this day.</EmptyLine>
                )}
              </div>
            </>
          )}
        </div>
      </div>
      {journalEditor && (
        <VaultMarkdownEditor
          target={journalEditor}
          onClose={() => setJournalEditor(null)}
          onSaved={async () => {
            await loadDays();
            await loadDetail(selected);
          }}
        />
      )}
    </div>
  );
}

export function PeopleView({ liveKey }: LiveProps) {
  const [people, setPeople] = useState<PersonSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<PersonDetail | null>(null);
  const [newName, setNewName] = useState('');
  const [interaction, setInteraction] = useState('');
  const [followUpAt, setFollowUpAt] = useState('');
  const [err, setErr] = useState<string | null>(null);

  async function loadList() {
    setErr(null);
    try {
      const rows = await api.peopleApi.list();
      setPeople(rows);
      setSelected((current) => current || rows[0]?.slug || null);
    } catch (e) {
      setErr(errorMessage(e));
    }
  }

  async function loadDetail(slug: string | null) {
    if (!slug) {
      setDetail(null);
      setFollowUpAt('');
      return;
    }
    setErr(null);
    try {
      const row = await api.peopleApi.get(slug);
      setDetail(row);
      setFollowUpAt(followUpIsoToDatetimeLocal(row.follow_up_at));
    } catch (e) {
      setDetail(null);
      setErr(errorMessage(e));
    }
  }

  useEffect(() => { void loadList(); }, [liveKey]);
  useEffect(() => { void loadDetail(selected); }, [selected, liveKey]);

  async function createPerson(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    const created = await api.peopleApi.create({ name });
    setNewName('');
    await loadList();
    setSelected(created.slug);
  }

  async function addInteraction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!detail) return;
    const text = interaction.trim();
    if (!text) return;
    const updated = await api.peopleApi.addInteraction(detail.slug, text);
    setInteraction('');
    setDetail(updated);
    await loadList();
  }

  async function saveFollowUp() {
    if (!detail) return;
    const updated = await api.peopleApi.patch(detail.slug, {
      follow_up_at: followUpDatetimeLocalToIso(followUpAt),
    });
    setDetail(updated);
    setFollowUpAt(followUpIsoToDatetimeLocal(updated.follow_up_at));
    await loadList();
  }

  async function archiveSelected() {
    if (!detail) return;
    await api.peopleApi.delete(detail.slug);
    const next = people.find((person) => person.slug !== detail.slug)?.slug ?? null;
    setSelected(next);
    setDetail(null);
    await loadList();
  }

  return (
    <div className="view dash-view">
      <div className="view-h">Personal OS</div>
      <h1 className="view-title">People</h1>
      {err && <p className="dash-error">{err}</p>}
      <form className="dash-inline-form project-create" onSubmit={(event) => runMutation(() => createPerson(event), setErr)}>
        <input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="Person name"/>
        <button type="submit">create</button>
      </form>
      {people.length === 0 ? (
        <EmptyLine>No people yet.</EmptyLine>
      ) : (
        <div className="dash-split people-split">
          <div className="dash-list">
            {people.map((person) => (
              <button
                key={person.slug}
                className={`dash-card project-card ${selected === person.slug ? 'active' : ''}`}
                onClick={() => setSelected(person.slug)}
              >
                <div className="dash-card-title">{person.name}</div>
                <div className="dash-tags">
                  {person.birthday_soon && <span className="warn">birthday soon</span>}
                  {person.birthday && <span>{formatPersonDate(person.birthday)}</span>}
                  {person.last_interaction_at && <span>last {person.last_interaction_at.slice(0, 10)}</span>}
                </div>
              </button>
            ))}
          </div>
          <div className="dash-panel">
            {!detail ? <EmptyLine>Select a person.</EmptyLine> : (
              <>
                <div className="dash-section-head">
                  <h2>{detail.name}</h2>
                  <button className="chip muted" onClick={() => runMutation(archiveSelected, setErr)}>archive</button>
                </div>
                <div className="dash-tags">
                  {detail.birthday && <span>birthday {formatPersonDate(detail.birthday)}</span>}
                  {detail.anniversary && <span>anniversary {formatPersonDate(detail.anniversary)}</span>}
                  {detail.last_interaction_at && <span>last interaction {detail.last_interaction_at}</span>}
                </div>

                <section className="dash-section nested">
                  <h3 className="dash-mini-h">Follow-up</h3>
                  <div className="dash-inline-form people-followup">
                    <input
                      type="datetime-local"
                      value={followUpAt}
                      onChange={(event) => setFollowUpAt(event.target.value)}
                    />
                    <button type="button" onClick={() => runMutation(saveFollowUp, setErr)}>save</button>
                    <button
                      type="button"
                      className="muted"
                      onClick={() => {
                        setFollowUpAt('');
                        runMutation(async () => {
                          if (!detail) return;
                          const updated = await api.peopleApi.patch(detail.slug, { follow_up_at: null });
                          setDetail(updated);
                          await loadList();
                        }, setErr);
                      }}
                    >
                      clear
                    </button>
                  </div>
                </section>

                <section className="dash-section nested">
                  <h3 className="dash-mini-h">Facts</h3>
                  <FactBlock facts={detail.facts}/>
                </section>

                <SectionBlock title="Notes" body={detail.body}/>

                <section className="dash-section nested">
                  <h3 className="dash-mini-h">Interactions</h3>
                  <form className="dash-inline-form" onSubmit={(event) => runMutation(() => addInteraction(event), setErr)}>
                    <input value={interaction} onChange={(event) => setInteraction(event.target.value)} placeholder="Add interaction"/>
                    <button type="submit">add</button>
                  </form>
                  {detail.interactions.length === 0 ? <EmptyLine>No interactions yet.</EmptyLine> : (
                    <div className="dash-list compact people-timeline">
                      {[...detail.interactions].reverse().map((item) => (
                        <div key={`${item.ts}:${item.text}`} className="dash-row">
                          <span className="dash-muted">{item.ts}</span>
                          <b>{item.text}</b>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function InventoryView({ liveKey }: LiveProps) {
  const [items, setItems] = useState<InventorySummary[]>([]);
  const [totalValue, setTotalValue] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<InventoryDetail | null>(null);
  const [statusFilter, setStatusFilter] = useState('');
  const [locationFilter, setLocationFilter] = useState('');
  const [newName, setNewName] = useState('');
  const [newAcquired, setNewAcquired] = useState('');
  const [newValue, setNewValue] = useState('');
  const [newLocation, setNewLocation] = useState('');
  const [editName, setEditName] = useState('');
  const [editAcquired, setEditAcquired] = useState('');
  const [editValue, setEditValue] = useState('');
  const [editStatus, setEditStatus] = useState<InventoryStatus>('owned');
  const [editLocation, setEditLocation] = useState('');
  const [editPhoto, setEditPhoto] = useState('');
  const [editNotes, setEditNotes] = useState('');
  const [err, setErr] = useState<string | null>(null);

  async function loadList() {
    setErr(null);
    try {
      const result = await api.inventoryApi.list({
        status: statusFilter || undefined,
        location: locationFilter.trim() || undefined,
      });
      setItems(result.items);
      setTotalValue(result.total_value);
      setSelected((current) => (
        current && result.items.some((item) => item.id === current)
          ? current
          : result.items[0]?.id ?? null
      ));
    } catch (e) {
      setErr(errorMessage(e));
    }
  }

  async function loadDetail(id: string | null) {
    if (!id) {
      setDetail(null);
      return;
    }
    setErr(null);
    try {
      setDetail(await api.inventoryApi.get(id));
    } catch (e) {
      setDetail(null);
      setErr(errorMessage(e));
    }
  }

  useEffect(() => { void loadList(); }, [liveKey, statusFilter, locationFilter]);
  useEffect(() => { void loadDetail(selected); }, [selected, liveKey]);
  useEffect(() => {
    if (!detail) return;
    setEditName(detail.name);
    setEditAcquired(detail.acquired ?? '');
    setEditValue(detail.value == null ? '' : String(detail.value));
    setEditStatus(INVENTORY_STATUSES.includes(detail.status as InventoryStatus) ? detail.status as InventoryStatus : 'owned');
    setEditLocation(detail.location ?? '');
    setEditPhoto(detail.photo ?? '');
    setEditNotes(detail.body ?? '');
  }, [detail]);

  async function createItem(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = newName.trim();
    if (!name) return;
    const value = optionalNumber(newValue);
    if (value === undefined) {
      setErr('value must be a number');
      return;
    }
    const created = await api.inventoryApi.create({
      name,
      acquired: newAcquired || null,
      value,
      status: 'owned',
      location: newLocation.trim() || null,
    });
    setNewName('');
    setNewAcquired('');
    setNewValue('');
    setNewLocation('');
    await loadList();
    setSelected(created.id);
  }

  async function saveDetail() {
    if (!detail) return;
    const name = editName.trim();
    if (!name) {
      setErr('name must be non-blank');
      return;
    }
    const value = optionalNumber(editValue);
    if (value === undefined) {
      setErr('value must be a number');
      return;
    }
    const updated = await api.inventoryApi.patch(detail.id, {
      name,
      acquired: editAcquired || null,
      value,
      status: editStatus,
      location: editLocation.trim() || null,
      photo: editPhoto.trim() || null,
      notes: editNotes,
    });
    setDetail(updated);
    await loadList();
  }

  async function archiveDetail() {
    if (!detail) return;
    await api.inventoryApi.delete(detail.id);
    setDetail(null);
    await loadList();
  }

  return (
    <div className="view dash-view">
      <div className="view-h">Personal OS</div>
      <div className="dash-section-head">
        <h1 className="view-title">Inventory</h1>
        <a className="chip" href={api.inventoryApi.exportUrl}>export CSV</a>
      </div>
      <div className="dash-tags">
        <span>total value {formatInventoryValue(totalValue)}</span>
        <span>{items.length} items</span>
      </div>
      {err && <p className="dash-error">{err}</p>}

      <form className="dash-inline-form project-create" onSubmit={(event) => runMutation(() => createItem(event), setErr)}>
        <input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="Item name"/>
        <input type="date" value={newAcquired} onChange={(event) => setNewAcquired(event.target.value)} />
        <input type="number" min="0" step="0.01" value={newValue} onChange={(event) => setNewValue(event.target.value)} placeholder="Value"/>
        <input value={newLocation} onChange={(event) => setNewLocation(event.target.value)} placeholder="Location"/>
        <button type="submit">create</button>
      </form>

      <div className="dash-filters">
        <Select label="Status" value={statusFilter} onChange={setStatusFilter} options={[
          ['', 'all'], ...INVENTORY_STATUSES.map((status) => [status, status] as [string, string]),
        ]}/>
        <label>
          <span>Location</span>
          <input value={locationFilter} onChange={(event) => setLocationFilter(event.target.value)} placeholder="exact match" />
        </label>
      </div>

      {items.length === 0 ? (
        <EmptyLine>No inventory items match these filters.</EmptyLine>
      ) : (
        <div className="dash-split">
          <div className="dash-list">
            {items.map((item) => (
              <button
                key={item.id}
                className={`dash-card project-card ${selected === item.id ? 'active' : ''}`}
                onClick={() => setSelected(item.id)}
              >
                <div className="dash-card-title">{item.name}</div>
                <div className="dash-tags">
                  <span className={item.status === 'owned' ? undefined : 'warn'}>{item.status}</span>
                  {item.location && <span>{item.location}</span>}
                  {item.value != null && <span>{formatInventoryValue(item.value)}</span>}
                </div>
              </button>
            ))}
          </div>
          <div className="dash-panel">
            {!detail ? <EmptyLine>Select an item.</EmptyLine> : (
              <>
                <div className="dash-section-head">
                  <h2>{detail.name}</h2>
                  <button className="chip muted" type="button" onClick={() => runMutation(archiveDetail, setErr)}>archive</button>
                </div>
                <div className="dash-inline-form">
                  <input value={editName} onChange={(event) => setEditName(event.target.value)} placeholder="Name"/>
                  <input type="date" value={editAcquired} onChange={(event) => setEditAcquired(event.target.value)} />
                  <select value={editStatus} onChange={(event) => setEditStatus(event.target.value as InventoryStatus)}>
                    {INVENTORY_STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}
                  </select>
                  <input type="number" min="0" step="0.01" value={editValue} onChange={(event) => setEditValue(event.target.value)} placeholder="Value"/>
                  <input value={editLocation} onChange={(event) => setEditLocation(event.target.value)} placeholder="Location"/>
                  <input value={editPhoto} onChange={(event) => setEditPhoto(event.target.value)} placeholder="Photo path"/>
                  <button type="button" onClick={() => runMutation(saveDetail, setErr)}>save</button>
                </div>
                <textarea className="dash-notes-edit" value={editNotes} onChange={(event) => setEditNotes(event.target.value)} placeholder="Notes"/>
                <KeyValueBlock values={{
                  acquired: detail.acquired,
                  value: detail.value,
                  status: detail.status,
                  location: detail.location,
                  photo: detail.photo,
                  path: detail.path,
                }}/>
                <SectionBlock title="Notes" body={detail.body}/>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export function ContentView({ liveKey, onNavigate }: LiveProps & NavProps) {
  const [data, setData] = useState<ContentList | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ContentDetail | null>(null);
  const [kindFilter, setKindFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [domainFilter, setDomainFilter] = useState('');
  const [newTitle, setNewTitle] = useState('');
  const [newKind, setNewKind] = useState<ContentKind>('article');
  const [newDomain, setNewDomain] = useState('');
  const [newChannel, setNewChannel] = useState('');
  const [newOutline, setNewOutline] = useState('');
  const [newTemplate, setNewTemplate] = useState('');
  const [editStatus, setEditStatus] = useState<ContentStatus>('idea');
  const [editChannel, setEditChannel] = useState('');
  const [editUrl, setEditUrl] = useState('');
  const [editPublishDate, setEditPublishDate] = useState('');
  const [draftingSlug, setDraftingSlug] = useState<string | null>(null);
  const [archivingSlug, setArchivingSlug] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [contentEditor, setContentEditor] = useState<VaultEditorTarget | null>(null);

  async function loadList() {
    setErr(null);
    try {
      const result = await api.contentApi.list({
        kind: kindFilter || undefined,
        status: statusFilter || undefined,
        domain: domainFilter.trim() || undefined,
      });
      setData(result);
      setSelected((current) => (
        current && result.items.some((item) => item.slug === current)
          ? current
          : result.items[0]?.slug ?? null
      ));
    } catch (e) {
      setErr(errorMessage(e));
    }
  }

  async function loadDetail(slug: string | null) {
    if (!slug) {
      setDetail(null);
      return;
    }
    setErr(null);
    try {
      setDetail(await api.contentApi.get(slug));
    } catch (e) {
      setDetail(null);
      setErr(errorMessage(e));
    }
  }

  useEffect(() => { void loadList(); }, [liveKey, kindFilter, statusFilter, domainFilter]);
  useEffect(() => { void loadDetail(selected); }, [selected, liveKey]);
  useEffect(() => {
    if (!detail) return;
    setEditStatus(CONTENT_STATUSES.includes(detail.status as ContentStatus) ? detail.status as ContentStatus : 'idea');
    setEditChannel(detail.channel ?? '');
    setEditUrl(detail.url ?? '');
    setEditPublishDate(detail.publish_date ?? '');
  }, [detail]);

  async function createItem(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const title = newTitle.trim();
    if (!title) return;
    const created = await api.contentApi.create({
      title,
      kind: newKind,
      status: 'idea',
      domain: newDomain.trim() || null,
      channel: newChannel.trim() || null,
      outline: newOutline.trim() || null,
      checklist_template: newTemplate.trim() || null,
    });
    setNewTitle('');
    setNewDomain('');
    setNewChannel('');
    setNewOutline('');
    setNewTemplate('');
    await loadList();
    setSelected(created.slug);
  }

  async function saveDetail() {
    if (!detail) return;
    const updated = await api.contentApi.patch(detail.slug, {
      status: editStatus,
      channel: editChannel.trim() || null,
      url: editUrl.trim() || null,
      publish_date: editPublishDate || null,
    });
    setDetail(updated);
    await loadList();
  }

  async function moveStatus(slug: string, status: ContentStatus) {
    const updated = await api.contentApi.patch(slug, { status });
    if (detail?.slug === slug) setDetail(updated);
    await loadList();
  }

  async function advanceDetail() {
    if (!detail) return;
    await moveStatus(detail.slug, nextContentStatus(detail.status));
  }

  async function spawnDraft() {
    if (!detail) return;
    setDraftingSlug(detail.slug);
    try {
      const result = await api.contentApi.draft(detail.slug);
      onNavigate('blog_post', String(result.blog_post_id));
    } finally {
      setDraftingSlug(null);
    }
  }

  async function archiveDetail() {
    if (!detail) return;
    const slug = detail.slug;
    setArchivingSlug(slug);
    try {
      await api.contentApi.delete(slug);
      setDetail(null);
      setSelected(null);
      await loadList();
    } finally {
      setArchivingSlug(null);
    }
  }

  const items = data?.items ?? [];
  const kanban = data?.kanban ?? {};

  return (
    <div className="view dash-view">
      <div className="view-h">Personal OS</div>
      <h1 className="view-title">Content</h1>
      {err && <p className="dash-error">{err}</p>}

      <form className="dash-inline-form project-create" onSubmit={(event) => runMutation(() => createItem(event), setErr)}>
        <input value={newTitle} onChange={(event) => setNewTitle(event.target.value)} placeholder="Title"/>
        <select value={newKind} onChange={(event) => setNewKind(event.target.value as ContentKind)}>
          {CONTENT_KINDS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
        </select>
        <input value={newDomain} onChange={(event) => setNewDomain(event.target.value)} placeholder="Domain"/>
        <input value={newChannel} onChange={(event) => setNewChannel(event.target.value)} placeholder="Channel"/>
        <input value={newTemplate} onChange={(event) => setNewTemplate(event.target.value)} placeholder="Checklist template"/>
        <textarea value={newOutline} onChange={(event) => setNewOutline(event.target.value)} placeholder="Outline"/>
        <button type="submit">create</button>
      </form>

      <div className="dash-filters">
        <Select label="Kind" value={kindFilter} onChange={setKindFilter} options={[
          ['', 'all'], ...CONTENT_KINDS.map((kind) => [kind, kind] as [string, string]),
        ]}/>
        <Select label="Status" value={statusFilter} onChange={setStatusFilter} options={[
          ['', 'all'], ...CONTENT_STATUSES.map((status) => [status, status] as [string, string]),
        ]}/>
        <label>
          <span>Domain</span>
          <input value={domainFilter} onChange={(event) => setDomainFilter(event.target.value)} placeholder="exact match"/>
        </label>
      </div>

      <section className="dash-section content-kanban">
        <div className="dash-section-head">
          <h2>Kanban</h2>
          <span className="dash-muted">{items.length} items</span>
        </div>
        <div className="content-kanban-grid">
          {CONTENT_STATUSES.map((status) => (
            <div className="dash-card content-column" key={status}>
              <div className="dash-section-head">
                <h3>{status}</h3>
                <span className="dash-muted">{(kanban[status] ?? []).length}</span>
              </div>
              {(kanban[status] ?? []).length === 0 ? (
                <EmptyLine>Empty.</EmptyLine>
              ) : (
                <div className="dash-list compact">
                  {(kanban[status] ?? []).map((item) => (
                    <ContentCard
                      key={item.slug}
                      item={item}
                      active={selected === item.slug}
                      onSelect={() => setSelected(item.slug)}
                      onMove={(next) => runMutation(() => moveStatus(item.slug, next), setErr)}
                    />
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      {items.length === 0 ? (
        <EmptyLine>No content items match these filters.</EmptyLine>
      ) : (
        <div className="dash-split">
          <div className="dash-list">
            {items.map((item) => (
              <ContentCard
                key={item.slug}
                item={item}
                active={selected === item.slug}
                onSelect={() => setSelected(item.slug)}
                onMove={(next) => runMutation(() => moveStatus(item.slug, next), setErr)}
              />
            ))}
          </div>
          <div className="dash-panel">
            {!detail ? <EmptyLine>Select a content item.</EmptyLine> : (
              <>
                <div className="dash-section-head">
                  <h2>{detail.title}</h2>
                  <div className="dash-actions">
                    <button className="chip" type="button" onClick={() => setContentEditor({ path: detail.path, title: detail.title })}>
                      edit
                    </button>
                    <button
                      className="chip"
                      type="button"
                      disabled={
                        !['article', 'newsletter'].includes(detail.kind)
                          || draftingSlug === detail.slug
                      }
                      onClick={() => runMutation(spawnDraft, setErr)}
                    >
                      {draftingSlug === detail.slug ? 'spawning' : 'spawn draft'}
                    </button>
                    <button
                      className="chip muted"
                      type="button"
                      disabled={archivingSlug === detail.slug}
                      onClick={() => runMutation(archiveDetail, setErr)}
                    >
                      {archivingSlug === detail.slug ? 'archiving' : 'archive'}
                    </button>
                  </div>
                </div>
                <div className="dash-tags">
                  <span>{detail.kind}</span>
                  <span>{detail.status}</span>
                  {detail.domain && <span>{detail.domain}</span>}
                  {detail.channel && <span>{detail.channel}</span>}
                  {detail.needs_triage && <span className="warn">needs triage</span>}
                </div>
                <div className="dash-inline-form">
                  <select value={editStatus} onChange={(event) => setEditStatus(event.target.value as ContentStatus)}>
                    {CONTENT_STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}
                  </select>
                  <input value={editChannel} onChange={(event) => setEditChannel(event.target.value)} placeholder="Channel"/>
                  <input value={editUrl} onChange={(event) => setEditUrl(event.target.value)} placeholder="URL"/>
                  <input type="date" value={editPublishDate} onChange={(event) => setEditPublishDate(event.target.value)} />
                  <button type="button" onClick={() => runMutation(saveDetail, setErr)}>save</button>
                  <button type="button" className="muted" onClick={() => runMutation(advanceDetail, setErr)}>advance</button>
                </div>
                <KeyValueBlock values={{
                  path: detail.path,
                  publish_date: detail.publish_date,
                  url: detail.url,
                }}/>
                <SectionBlock title="Outline" body={detail.body}/>
                <section className="dash-section nested">
                  <h3 className="dash-mini-h">Linked tasks</h3>
                  {detail.tasks.length === 0 ? <EmptyLine>No open tasks.</EmptyLine> : (
                    <div className="dash-list compact">
                      {detail.tasks.map((task) => (
                        <TaskLine
                          key={task.uid}
                          task={task}
                          today={localIsoToday()}
                          onChanged={() => { void loadDetail(detail.slug); }}
                        />
                      ))}
                    </div>
                  )}
                </section>
              </>
            )}
          </div>
        </div>
      )}
      {contentEditor && (
        <VaultMarkdownEditor
          target={contentEditor}
          onClose={() => setContentEditor(null)}
          onSaved={async () => {
            await loadList();
            await loadDetail(selected);
          }}
        />
      )}
    </div>
  );
}

export function InboxTriageView({ liveKey }: LiveProps) {
  const [items, setItems] = useState<CaptureTriageItem[]>([]);
  const [needsReview, setNeedsReview] = useState<NeedsReviewItem[]>([]);
  const [tab, setTab] = useState<'triage' | 'review'>('triage');
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    setErr(null);
    try {
      const [triageRows, reviewRows] = await Promise.all([
        api.captureTriage.list(),
        api.needsReview.list(),
      ]);
      setItems(triageRows);
      setNeedsReview(reviewRows);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'failed');
    }
  }

  useEffect(() => { void load(); }, [liveKey]);

  async function act(item: CaptureTriageItem, type: CaptureTriageTarget) {
    setBusy(item.id);
    setErr(null);
    try {
      await api.captureTriage.reclassify(item.id, type);
      await load();
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  async function dismissReview(item: NeedsReviewItem) {
    setBusy(`review:${item.id}`);
    setErr(null);
    try {
      await api.needsReview.dismiss(item.id);
      await load();
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="view dash-view">
      <div className="view-h">Personal OS</div>
      <h1 className="view-title">Inbox triage</h1>
      <div className="dash-tabs">
        <button className={tab === 'triage' ? 'active' : ''} onClick={() => setTab('triage')}>
          Triage {items.length}
        </button>
        <button className={tab === 'review' ? 'active' : ''} onClick={() => setTab('review')}>
          Needs review {needsReview.length}
        </button>
      </div>
      {err && <p className="dash-error">{err}</p>}
      {tab === 'review' ? (
        needsReview.length === 0 ? (
          <EmptyLine>No items need review.</EmptyLine>
        ) : (
          <div className="dash-list">
            {needsReview.map((item) => (
              <div className="dash-card triage-card" key={item.id}>
                <div className="dash-row-main">
                  <b>{item.title}</b>
                  {item.excerpt && <span className="dash-muted">{item.excerpt}</span>}
                </div>
                <div className="dash-tags">
                  <span>{formatReviewReason(item.reason)}</span>
                  <span>{item.entity_type}</span>
                </div>
                <div className="dash-actions">
                  <a className="chip" href={item.link}>review</a>
                  <button
                    className="chip muted"
                    disabled={busy === `review:${item.id}`}
                    onClick={() => void dismissReview(item)}
                  >
                    dismiss
                  </button>
                </div>
              </div>
            ))}
          </div>
        )
      ) : items.length === 0 ? (
        <EmptyLine>No captures need triage.</EmptyLine>
      ) : (
        <div className="dash-list">
          {items.map((item) => (
            <div className="dash-card triage-card" key={item.id}>
              <div className="dash-row-main">
                <b>{item.original_text}</b>
                <span className="dash-muted">
                  {item.detected_type}
                  {typeof item.confidence === 'number' ? ` · ${Math.round(item.confidence * 100)}%` : ''}
                </span>
              </div>
              <div className="dash-tags">
                {triageTargets(item).map((target) => (
                  <button
                    key={target}
                    className={`chip ${target === item.detected_type ? 'active' : ''}`}
                    disabled={busy === item.id}
                    onClick={() => void act(item, target)}
                  >
                    {target === item.detected_type ? `accept ${target}` : target}
                  </button>
                ))}
                <button className="chip muted" disabled={busy === item.id} onClick={() => void act(item, 'dismiss')}>
                  {item.kind === 'task' ? 'keep task' : 'dismiss'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function FocusPanel({ focus, today, onChanged }: { focus: TaskRow[]; today: string; onChanged: ChangeHandler }) {
  const byPosition = new Map(focus.map((task, idx) => [task.position ?? idx + 1, task]));
  return (
    <section className="dash-card focus-panel">
      <div className="dash-section-head">
        <h2>Top-3 focus</h2>
        <span className="dash-muted">{focus.length}/3</span>
      </div>
      <div className="focus-slots">
        {[1, 2, 3].map((position) => {
          const task = byPosition.get(position);
          return (
            <div key={position} className="focus-slot">
              <span className="focus-index">{position}</span>
              {task ? (
                <FocusTaskRow task={task} today={today} onChanged={onChanged}/>
              ) : (
                <p className="dash-empty">No focus task.</p>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function FocusTaskRow({ task, today, onChanged }: { task: TaskRow; today: string; onChanged: ChangeHandler }) {
  const bucket = taskBucket(task, today);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className={`focus-task-row ${task.checked ? 'done' : ''}`}>
      <button
        className="focus-star on"
        onClick={() => runMutation(async () => {
          await api.focus.remove(today, task.uid);
          await onChanged();
        }, setErr)}
        aria-label="Remove from daily focus"
        title="Remove from daily focus"
      >
        ★
      </button>
      <div>
        <b>{task.text}</b>
        <span>
          {task.status === 'done' ? 'done' : task.due ? formatDue(task.due) : 'no due date'}
          {bucket === 'overdue' ? ' · overdue' : ''}
        </span>
        {err && <em>{err}</em>}
      </div>
    </div>
  );
}

function SlippingRail({ items, onChanged }: { items: SlippingItem[]; onChanged: ChangeHandler }) {
  const [err, setErr] = useState<string | null>(null);
  async function act(item: SlippingItem, kind: 'snooze' | 'mute' | 'unmute') {
    setErr(null);
    try {
      if (kind === 'snooze') {
        await api.slipping.snooze(item.entity_type, item.entity_id, 7);
      } else if (kind === 'mute') {
        await api.slipping.mute(item.entity_type, item.entity_id);
      } else {
        await api.slipping.unmute(item.entity_type, item.entity_id);
      }
      await onChanged();
    } catch (e) {
      setErr(errorMessage(e));
    }
  }
  return (
    <section className="dash-section slipping-rail">
      <div className="dash-section-head">
        <h2>Slipping</h2>
        <span className="dash-muted">{items.length}</span>
      </div>
      {err && <p className="dash-error">{err}</p>}
      <div className="dash-strip">
        {items.map((item) => (
          <div className="dash-pill slipping-pill" key={`${item.entity_type}:${item.entity_id}`}>
            <b>{item.title}</b>
            <span>{item.stale_since}</span>
            {item.domain && <em>{item.domain}</em>}
            {item.slipping_muted && <em>muted</em>}
            <button onClick={() => void act(item, 'snooze')}>1w</button>
            <button onClick={() => void act(item, item.slipping_muted ? 'unmute' : 'mute')}>
              {item.slipping_muted ? 'unmute' : 'mute'}
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

function ResurfaceCard({ item }: { item: ResurfaceItem }) {
  return (
    <section className="dash-card resurface-card">
      <div className="dash-section-head">
        <h2>Resurfacing</h2>
        <span className="dash-muted">{item.kind}</span>
      </div>
      <a href={item.link}>{item.title}</a>
      {item.excerpt && <p>{item.excerpt}</p>}
    </section>
  );
}

function CalendarPanel({
  calendar,
  calendarErr,
  calendarBusy,
  onSync,
}: {
  calendar: CalendarToday | null;
  calendarErr: string | null;
  calendarBusy: boolean;
  onSync: () => Promise<void>;
}) {
  const status = calendar?.status.status ?? 'loading';
  const events = calendar?.events ?? [];
  return (
    <section className="dash-card calendar-panel">
      <div className="dash-section-head">
        <h2>Calendar</h2>
        <span className={`dash-muted ${status === 'disconnected' ? 'warn' : ''}`}>{status}</span>
      </div>
      {calendarErr ? (
        <p className="dash-error">{calendarErr}</p>
      ) : !calendar ? (
        <EmptyLine>Loading calendar.</EmptyLine>
      ) : status === 'unconfigured' ? (
        <EmptyLine>Connect Google Calendar with <code>mastisk calendar-connect</code>.</EmptyLine>
      ) : status === 'not_synced' ? (
        <div className="dash-list compact">
          <EmptyLine>Calendar not synced yet.</EmptyLine>
          <button className="chip" disabled={calendarBusy} onClick={() => void onSync()}>
            {calendarBusy ? 'syncing' : 'sync now'}
          </button>
        </div>
      ) : status === 'disconnected' ? (
        <p className="dash-error">{calendar.status.error || 'Calendar OAuth expired.'}</p>
      ) : events.length === 0 ? (
        <EmptyLine>No events today.</EmptyLine>
      ) : (
        <div className="dash-list compact">
          {events.map((event) => (
            <div key={`${event.calendar_id}:${event.id}`} className="dash-row">
              <span className={event.all_day ? 'dash-pill mini' : 'dash-muted'}>
                {event.all_day ? 'all-day' : formatTime(event.start)}
              </span>
              <span>{event.summary}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function TaskGroup({
  title, tasks, today, onChanged, focusDate, focusedUids,
}: {
  title: string;
  tasks: TaskRow[];
  today: string;
  onChanged: ChangeHandler;
  focusDate?: string;
  focusedUids?: Set<string>;
}) {
  if (tasks.length === 0) return null;
  return (
    <section className="dash-section">
      <h2>{title}</h2>
      <div className="dash-list">
        {tasks.map((task) => (
          <TaskLine
            key={task.uid}
            task={task}
            today={today}
            onChanged={onChanged}
            focusDate={focusDate}
            focused={focusedUids?.has(task.uid) ?? false}
          />
        ))}
      </div>
    </section>
  );
}

function TaskLine({
  task, today, onChanged, focusDate, focused = false,
}: {
  task: TaskRow;
  today: string;
  onChanged: ChangeHandler;
  focusDate?: string;
  focused?: boolean;
}) {
  const [due, setDue] = useState(task.due ?? '');
  const [priority, setPriority] = useState<Priority>(task.priority);
  const [err, setErr] = useState<string | null>(null);
  const [swapFocus, setSwapFocus] = useState<TaskRow[] | null>(null);
  useEffect(() => {
    setDue(task.due ?? '');
    setPriority(task.priority);
    setSwapFocus(null);
  }, [task.due, task.priority, task.uid, focused]);
  const bucket = taskBucket(task, today);

  async function savePatch(next: { due?: string | null; priority?: Priority }) {
    await api.tasks.patch(task.uid, next);
    await onChanged();
  }

  async function toggleFocus(replaceUid?: string) {
    if (!focusDate) return;
    setErr(null);
    try {
      if (focused && !replaceUid) {
        await api.focus.remove(focusDate, task.uid);
      } else {
        await api.focus.add(focusDate, task.uid, replaceUid);
      }
      setSwapFocus(null);
      await onChanged();
    } catch (e) {
      if (e instanceof FocusFullError) {
        setSwapFocus(e.focus);
        return;
      }
      setErr(errorMessage(e));
    }
  }

  return (
    <div className={`dash-card task-line ${task.checked ? 'done' : ''}`}>
      <button
        className="task-check"
        onClick={() => runMutation(async () => {
          await api.tasks.toggle(task.uid);
          await onChanged();
        }, setErr)}
        aria-label="Toggle task"
      >
        {task.checked ? 'x' : ''}
      </button>
      <div className="dash-row-main">
        <div className="task-title-line">
          {focusDate && (
            <button
              className={`focus-star ${focused ? 'on' : ''}`}
              onClick={() => void toggleFocus()}
              aria-label={focused ? 'Remove from daily focus' : 'Add to daily focus'}
              title={focused ? 'Remove from daily focus' : 'Add to daily focus'}
            >
              {focused ? '★' : '☆'}
            </button>
          )}
          <b>{task.text}</b>
        </div>
        <span className="dash-muted">
          {task.due ? `${formatDue(task.due)}${bucket === 'overdue' ? ' · overdue' : ''}` : 'no due date'}
          {task.project ? ` · ${task.project}` : ''}
        </span>
        <div className="dash-tags">
          {task.priority && <span>{task.priority}</span>}
          {task.recurrence && <span>recurs</span>}
          {task.needs_triage && <span className="warn">needs triage</span>}
          {task.recurrence_unparsed && <span className="warn">recurrence unparsed</span>}
        </div>
        {err && <span className="dash-error">{err}</span>}
        {swapFocus && (
          <div className="focus-swap">
            <span>Replace focus:</span>
            {swapFocus.map((focusedTask) => (
              <button key={focusedTask.uid} onClick={() => void toggleFocus(focusedTask.uid)}>
                {focusedTask.text}
              </button>
            ))}
            <button className="muted" onClick={() => setSwapFocus(null)}>cancel</button>
          </div>
        )}
      </div>
      <div className="task-edit">
        <input
          value={due}
          placeholder="due"
          onChange={(e) => setDue(e.target.value)}
          onBlur={() => {
            if ((task.due ?? '') !== due) runMutation(() => savePatch({ due: due.trim() || null }), setErr);
          }}
        />
        <select
          value={priority ?? ''}
          onChange={(e) => {
            const next = (e.target.value || null) as Priority;
            setPriority(next);
            runMutation(() => savePatch({ priority: next }), setErr);
          }}
        >
          {PRIORITIES.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
        </select>
      </div>
    </div>
  );
}

function RoutineGroup({ label, routines, onChanged }: { label: TimeOfDay; routines: RoutineRow[]; onChanged: ChangeHandler }) {
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="routine-group">
      <h3 className="dash-mini-h">{labelTime(label)}</h3>
      {err && <p className="dash-error">{err}</p>}
      <div className="dash-list compact">
        {routines.map((routine) => (
          <div key={routine.slug} className="dash-row">
            <button
              className={`mini-toggle ${routine.completed_today ? 'on' : ''}`}
              onClick={() => runMutation(async () => {
                await api.routinesApi.toggle(routine.slug);
                await onChanged();
              }, setErr)}
            >
              {routine.completed_today ? 'done' : 'mark'}
            </button>
            <span>{routine.name}</span>
            <span className="dash-muted">streak {routine.streak.current}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RoutineCard({ routine, liveKey, onChanged }: { routine: RoutineRow; liveKey: string; onChanged: ChangeHandler }) {
  const [progress, setProgress] = useState<RoutineProgress | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.routinesApi.progress(routine.slug, 30).then(setProgress).catch(() => setProgress(null));
  }, [routine.slug, liveKey]);

  return (
    <div className="dash-card routine-card">
      <div className="dash-section-head">
        <h3>{routine.name}</h3>
        <button
          className={`mini-toggle ${routine.completed_today ? 'on' : ''}`}
          onClick={() => runMutation(async () => {
            await api.routinesApi.toggle(routine.slug);
            await onChanged();
          }, setErr)}
        >
          {routine.completed_today ? 'done' : 'today'}
        </button>
      </div>
      {err && <p className="dash-error">{err}</p>}
      {routine.description && <p>{routine.description}</p>}
      <div className="dash-tags">
        <span>current {routine.streak.current}</span>
        <span>longest {routine.streak.longest}</span>
        <span>{Math.round(routine.streak.rate_30d * 100)}% / 30d</span>
        {routine.streak.fixed && <span>{routine.streak.fixed.days_done}/{routine.streak.fixed.target_days}</span>}
      </div>
      <ProgressBars dates={progress?.completion_dates ?? []}/>
      {routine.streak.fixed && (
        <div className="routine-fixed">
          <div style={{ width: `${routine.streak.fixed.target_days ? (routine.streak.fixed.days_done / routine.streak.fixed.target_days) * 100 : 0}%` }}/>
        </div>
      )}
      <button
        className="chip muted"
        onClick={() => runMutation(async () => {
          await api.routinesApi.archive(routine.slug);
          await onChanged();
        }, setErr)}
      >
        archive
      </button>
    </div>
  );
}

function ProgressBars({ dates }: { dates: string[] }) {
  const set = new Set(dates);
  const days = lastNDays(30);
  return (
    <div className="routine-bars" aria-label="30-day completion graph">
      {days.map((day) => <span key={day} className={set.has(day) ? 'on' : ''} title={day}/>)}
    </div>
  );
}

function MilestonesBlock({
  detail,
  onAdd,
  onToggle,
}: {
  detail: ProjectDetail;
  onAdd: (text: string) => void;
  onToggle: (position: number, done: boolean, expectedText: string) => void;
}) {
  const [text, setText] = useState('');
  const progress = detail.milestone_progress;
  return (
    <section className="dash-section nested">
      <div className="dash-section-head">
        <h3 className="dash-mini-h">Milestones</h3>
        <span className="dash-muted">{progress.done}/{progress.total}</span>
      </div>
      <div className="project-progress" aria-label="milestone progress">
        <span style={{ width: `${progress.percent}%` }}/>
      </div>
      {detail.milestones.length === 0 ? <EmptyLine>No milestones yet.</EmptyLine> : (
        <div className="dash-list compact">
          {detail.milestones.map((milestone) => (
            <label key={milestone.position} className="project-check-row">
              <input
                type="checkbox"
                checked={milestone.done}
                onChange={(event) => onToggle(milestone.position, event.target.checked, milestone.text)}
              />
              <span>{milestone.text}</span>
            </label>
          ))}
        </div>
      )}
      <form className="dash-inline-form" onSubmit={(event) => {
        event.preventDefault();
        const clean = text.trim();
        if (!clean) return;
        onAdd(clean);
        setText('');
      }}>
        <input value={text} onChange={(event) => setText(event.target.value)} placeholder="Add milestone"/>
        <button type="submit">add</button>
      </form>
    </section>
  );
}

function TimeBlock({
  detail,
  onAdd,
}: {
  detail: ProjectDetail;
  onAdd: (hours: number, text: string, entryDate: string) => void;
}) {
  const [entryDate, setEntryDate] = useState(localIsoToday());
  const [hours, setHours] = useState('');
  const [text, setText] = useState('');
  return (
    <section className="dash-section nested">
      <h3 className="dash-mini-h">Time</h3>
      <div className="dash-tags">
        <span>{detail.time_totals.total_hours}h total</span>
        <span>{detail.time_totals.last_30_days_hours}h / 30d</span>
      </div>
      <form className="dash-inline-form" onSubmit={(event) => {
        event.preventDefault();
        const parsed = Number(hours);
        const clean = text.trim();
        if (!Number.isFinite(parsed) || parsed <= 0 || !clean) return;
        onAdd(parsed, clean, entryDate);
        setHours('');
        setText('');
      }}>
        <input type="date" value={entryDate} onChange={(event) => setEntryDate(event.target.value)}/>
        <input type="number" min="0.25" step="0.25" value={hours} onChange={(event) => setHours(event.target.value)} placeholder="Hours"/>
        <input value={text} onChange={(event) => setText(event.target.value)} placeholder="Work done"/>
        <button type="submit">add</button>
      </form>
      {detail.time_entries.length > 0 && (
        <div className="dash-list compact">
          {detail.time_entries.slice(0, 5).map((entry) => (
            <div key={entry.position} className="dash-row">
              <span>{entry.date} · {entry.hours}h</span>
              <b>{entry.text}</b>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function RetainerBlock({ detail }: { detail: ProjectDetail }) {
  if (!detail.retainer) return null;
  const state = detail.retainer;
  return (
    <section className="dash-section nested">
      <div className="dash-section-head">
        <h3 className="dash-mini-h">{state.current_month}</h3>
        <span className="dash-muted">{state.open} open · due {state.month_end}</span>
      </div>
      {state.tasks.length === 0 ? <EmptyLine>No current-month retainer tasks.</EmptyLine> : (
        <div className="dash-list compact">
          {state.tasks.map((task) => (
            <div key={task.uid} className="dash-row">
              <span>{task.status}</span>
              <b>{task.text}</b>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function KeyValueBlock({ values }: { values: Record<string, unknown> }) {
  const rows = Object.entries(values).filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (rows.length === 0) return <EmptyLine>No frontmatter fields.</EmptyLine>;
  return (
    <div className="kv-block">
      {rows.map(([key, value]) => (
        <div key={key}>
          <span>{key}</span>
          <b>{String(value)}</b>
        </div>
      ))}
    </div>
  );
}

function FactBlock({ facts }: { facts: Record<string, unknown> }) {
  const rows = Object.entries(facts).filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (rows.length === 0) return <EmptyLine>No facts yet.</EmptyLine>;
  return (
    <div className="kv-block">
      {rows.map(([key, value]) => (
        <div key={key}>
          <span>{key}</span>
          <b>{formatFactValue(value)}</b>
        </div>
      ))}
    </div>
  );
}

function LogPreview({ body }: { body: string }) {
  const lines = sectionLines(parseMarkdownSections(body).Log).slice(-6).reverse();
  if (lines.length === 0) return <EmptyLine>No project log entries.</EmptyLine>;
  return (
    <div className="dash-list compact">
      {lines.map((line, idx) => (
        <div key={`${line}-${idx}`} className="dash-row">
          <MarkdownBlock source={line}/>
        </div>
      ))}
    </div>
  );
}

function SectionBlock({ title, body }: { title: string; body?: string }) {
  const lines = sectionLines(body);
  return (
    <section className="dash-section nested">
      <h3 className="dash-mini-h">{title}</h3>
      {lines.length === 0 ? <EmptyLine>No {title.toLowerCase()} yet.</EmptyLine> : (
        <div className="dash-list compact">
          {lines.map((line, idx) => (
            <div key={`${line}-${idx}`} className="dash-row">
              <MarkdownBlock source={line}/>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function ContentCard({
  item,
  active,
  onSelect,
  onMove,
}: {
  item: ContentSummary;
  active: boolean;
  onSelect: () => void;
  onMove: (status: ContentStatus) => void;
}) {
  return (
    <div className={`dash-card project-card ${active ? 'active' : ''}`}>
      <button className="content-card-main" type="button" onClick={onSelect}>
        <div className="dash-card-title">{item.title}</div>
        <div className="dash-tags">
          <span>{item.kind}</span>
          <span>{item.status}</span>
          {item.domain && <span>{item.domain}</span>}
          {item.needs_triage && <span className="warn">needs triage</span>}
        </div>
      </button>
      <select
        value={CONTENT_STATUSES.includes(item.status as ContentStatus) ? item.status : 'idea'}
        onChange={(event) => onMove(event.target.value as ContentStatus)}
        aria-label={`Move ${item.title}`}
      >
        {CONTENT_STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}
      </select>
    </div>
  );
}

function ScaleSetter({ label, value, onSet }: { label: string; value: number | null; onSet: (value: number | null) => void }) {
  return (
    <div className="scale-setter">
      <span>{label}</span>
      {[1, 2, 3, 4, 5].map((n) => (
        <button key={n} className={value === n ? 'active' : ''} onClick={() => onSet(value === n ? null : n)}>{n}</button>
      ))}
    </div>
  );
}

function optionalNumber(value: string): number | null | undefined {
  const text = value.trim();
  if (!text) return null;
  const number = Number(text);
  return Number.isFinite(number) && number >= 0 ? number : undefined;
}

function nextContentStatus(status: string): ContentStatus {
  const current = CONTENT_STATUSES.indexOf(status as ContentStatus);
  if (current < 0) return 'idea';
  return CONTENT_STATUSES[Math.min(current + 1, CONTENT_STATUSES.length - 1)];
}

function formatInventoryValue(value: number | null | undefined): string {
  if (value == null) return '-';
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value);
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: [string, string][] }) {
  return (
    <label>
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
}

function EmptyLine({ children }: { children: ReactNode }) {
  return <p className="dash-empty">{children}</p>;
}

function allRoutines(groups: RoutineGroups): RoutineRow[] {
  return TIME_GROUPS.flatMap((group) => groups[group] ?? []);
}

function groupTasks(tasks: TaskRow[], today: string): Record<typeof TASK_GROUPS[number], TaskRow[]> {
  return {
    overdue: tasks.filter((task) => taskBucket(task, today) === 'overdue'),
    today: tasks.filter((task) => taskBucket(task, today) === 'today'),
    upcoming: tasks.filter((task) => taskBucket(task, today) === 'upcoming'),
    someday: tasks.filter((task) => taskBucket(task, today) === 'someday'),
    done: tasks.filter((task) => taskBucket(task, today) === 'done'),
  };
}

function taskBucket(task: TaskRow, today: string): typeof TASK_GROUPS[number] {
  if (task.status !== 'open') return 'done';
  const due = datePart(task.due);
  if (!due) return 'someday';
  if (due < today) return 'overdue';
  if (due === today) return 'today';
  return 'upcoming';
}

function compareTasksByDue(a: TaskRow, b: TaskRow): number {
  const ad = a.due ?? '9999-99-99';
  const bd = b.due ?? '9999-99-99';
  return ad.localeCompare(bd) || a.text.localeCompare(b.text);
}

function triageTargets(item: CaptureTriageItem): CaptureTriageTarget[] {
  const detected = item.detected_type as CaptureTriageTarget;
  const common: CaptureTriageTarget[] = ['task', 'journal', 'note', 'project_update', 'routine_done', 'person', 'quote', 'inventory', 'content'];
  const allowed = common.filter((target) => target !== 'routine_done' || hasRoutineCandidate(item));
  const primary = detected === 'routine_done' && !hasRoutineCandidate(item) ? [] : [detected];
  return [...primary, ...allowed.filter((target) => target !== detected)];
}

function hasRoutineCandidate(item: CaptureTriageItem): boolean {
  const routine = item.capture.routine;
  return typeof routine === 'string' && routine.trim().length > 0;
}

function parseMarkdownSections(markdown: string): Record<string, string> {
  const lines = markdown.split(/\r?\n/);
  const sections: Record<string, string[]> = {};
  let current = '';
  for (const line of lines) {
    const match = /^##\s+(.+?)\s*$/.exec(line);
    if (match) {
      current = match[1].trim();
      sections[current] = sections[current] || [];
      continue;
    }
    if (current) sections[current].push(line);
  }
  return Object.fromEntries(Object.entries(sections).map(([key, value]) => [key, value.join('\n').trim()]));
}

function sectionLines(body?: string): string[] {
  return (body ?? '').split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

function datePart(value?: string | null): string {
  if (!value) return '';
  return value.slice(0, 10);
}

function localDatePart(value?: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return datePart(value);
  return isoFromDate(date);
}

function localIsoToday(): string {
  return isoFromDate(new Date());
}

function followUpIsoToDatetimeLocal(value?: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/.exec(value)?.[1] ?? '';
  }
  return datetimeLocalParts(date);
}

function followUpDatetimeLocalToIso(value: string): string | null {
  const text = value.trim();
  if (!text) return null;
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  // datetime-local has no zone; treat it as browser-local capture time and
  // persist an ISO value with that local offset so the instant is stable.
  return `${datetimeLocalParts(date)}:00${localOffsetSuffix(date)}`;
}

function datetimeLocalParts(date: Date): string {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  const hh = String(date.getHours()).padStart(2, '0');
  const min = String(date.getMinutes()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}T${hh}:${min}`;
}

function localOffsetSuffix(date: Date): string {
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? '+' : '-';
  const absMinutes = Math.abs(offsetMinutes);
  const hh = String(Math.floor(absMinutes / 60)).padStart(2, '0');
  const mm = String(absMinutes % 60).padStart(2, '0');
  return `${sign}${hh}:${mm}`;
}

function isoFromDate(date: Date): string {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
}

function lastNDays(n: number): string[] {
  const today = new Date();
  const days: string[] = [];
  for (let i = n - 1; i >= 0; i -= 1) {
    const day = new Date(today);
    day.setDate(today.getDate() - i);
    days.push(isoFromDate(day));
  }
  return days;
}

function formatDue(value: string): string {
  const day = datePart(value);
  const time = value.includes('T') ? value.slice(11, 16) : '';
  return time ? `${day} ${time}` : day;
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(11, 16) || value;
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatLongDate(value: string): string {
  const date = new Date(`${value}T12:00:00`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}

function formatPersonDate(value: string): string {
  if (/^\d{2}-\d{2}$/.test(value)) return value;
  return datePart(value);
}

function formatFactValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => String(item)).join(', ');
  if (value && typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function labelTime(value: TimeOfDay): string {
  return value;
}

function formatReviewReason(value: string): string {
  return value.replace(/_/g, ' ');
}
