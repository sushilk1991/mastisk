import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api } from '../api';
import type {
  LearningAnswerResult, LearningGoal, LearningGoalDetail, LearningToday, Lesson,
  LessonChatMessage, LessonQuestion, View,
} from '../types';
import { MermaidBlock } from './MermaidBlock';

interface NavProps {
  onNavigate: (view: View, id?: string) => void;
}

const RATING_LABELS: Record<number, string> = {
  1: 'Again', 2: 'Hard', 3: 'Good', 4: 'Easy',
};
const CONFIDENCE_LABELS: Record<number, string> = {
  1: 'No idea', 2: 'Unsure', 3: 'Confident', 4: 'Certain',
};

// ───────────────────────────── Learning home ─────────────────────────────

export function LearningView({ onNavigate }: NavProps) {
  const [today, setToday] = useState<LearningToday | null>(null);
  const [goals, setGoals] = useState<LearningGoal[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([api.learning.today(), api.learning.goals()])
      .then(([t, g]) => { setToday(t); setGoals(g.goals); setErr(null); })
      .catch((e: Error) => setErr(e.message));
  }, []);

  useEffect(() => { load(); }, [load]);

  if (err) {
    return (
      <div className="view">
        <div className="view-h">Learning</div>
        <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>
          couldn't load learning: {err}
        </p>
      </div>
    );
  }
  if (!today || !goals) {
    return (
      <div className="view">
        <div className="view-h">Learning</div>
        <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p>
      </div>
    );
  }

  const lesson = today.lesson;
  return (
    <div className="view">
      <div className="view-h">Learning</div>
      <h1 className="view-title">Guru</h1>
      <p className="view-sub">
        One lesson a day, quizzes on a forgetting curve, everything compounds into your wiki.
        {today.streak > 0 && <> Current streak: <strong>{today.streak} day{today.streak === 1 ? '' : 's'}</strong>.</>}
      </p>

      {notice && (
        <p style={{ color: 'var(--fg-mute)', fontFamily: 'var(--mono)', fontSize: 12 }}>{notice}</p>
      )}

      <div className="learn-today-card">
        {lesson ? (
          <>
            <div className="learn-today-meta">
              Today · Day {lesson.day_number} of {lesson.goal_topic}
              {today.due_reviews > 0 && <> · {today.due_reviews} review{today.due_reviews === 1 ? '' : 's'} due</>}
            </div>
            <div className="learn-today-title">{lesson.title}</div>
            <div className="learn-today-actions">
              <button className="chip" onClick={() => onNavigate('learning_lesson', String(lesson.id))}>
                {lesson.status === 'completed' ? 'Revisit lesson' : 'Open lesson'}
              </button>
              {lesson.status === 'completed' && <span className="learn-status-chip done">completed</span>}
            </div>
          </>
        ) : goals.length > 0 ? (
          <>
            <div className="learn-today-meta">Today</div>
            <div className="learn-today-title">No lesson yet.</div>
            <div className="learn-today-actions">
              <button
                className="chip"
                onClick={() => {
                  api.learning.generateNow()
                    .then(() => setNotice('Guru is writing today\'s lesson — check back in a minute.'))
                    .catch((e: Error) => setNotice(e.message));
                }}
              >
                Generate now
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="learn-today-meta">Getting started</div>
            <div className="learn-today-title">Tell Guru what you want to learn.</div>
          </>
        )}
      </div>

      <NewGoalForm onCreated={() => { setNotice('Goal created — Guru is planning the syllabus.'); load(); }} />

      {goals.length > 0 && (
        <div className="learn-goals">
          {goals.map((g) => (
            <GoalCard key={g.id} goal={g} onChanged={load} onNavigate={onNavigate} />
          ))}
        </div>
      )}
    </div>
  );
}

function NewGoalForm({ onCreated }: { onCreated: () => void }) {
  const [topic, setTopic] = useState('');
  const [days, setDays] = useState('');
  const [level, setLevel] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = () => {
    const t = topic.trim();
    if (!t || busy) return;
    setBusy(true);
    setErr(null);
    const body: { topic: string; timeline_days?: number; level?: string } = { topic: t };
    const d = Number(days);
    if (days.trim() && Number.isFinite(d) && d >= 1) body.timeline_days = Math.floor(d);
    if (level.trim()) body.level = level.trim();
    api.learning.createGoal(body)
      .then(() => { setTopic(''); setDays(''); setLevel(''); onCreated(); })
      .catch((e: Error) => setErr(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <div className="learn-new-goal">
      <input
        className="learn-input"
        placeholder="What do you want to learn? e.g. Rust async internals"
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') submit(); }}
      />
      <input
        className="learn-input learn-input-days"
        placeholder="days (optional)"
        inputMode="numeric"
        title="Timeline in days — shorter timelines pack more concepts into each lesson"
        value={days}
        onChange={(e) => setDays(e.target.value.replace(/[^0-9]/g, ''))}
      />
      <input
        className="learn-input learn-input-level"
        placeholder="your level (optional)"
        value={level}
        onChange={(e) => setLevel(e.target.value)}
      />
      <button className="chip" onClick={submit} disabled={busy || !topic.trim()}>
        {busy ? 'Creating…' : 'Start learning'}
      </button>
      {err && <span style={{ color: 'var(--danger)', fontFamily: 'var(--mono)', fontSize: 12 }}>{err}</span>}
    </div>
  );
}

function GoalCard({ goal, onChanged, onNavigate }: {
  goal: LearningGoal; onChanged: () => void;
} & NavProps) {
  const pct = goal.total_concepts > 0
    ? Math.round((goal.taught_concepts / goal.total_concepts) * 100)
    : 0;
  const act = (fn: Promise<void>) => fn.then(onChanged).catch(() => onChanged());
  return (
    <div className="learn-goal-card">
      <div className="learn-goal-head">
        <button
          className="learn-goal-topic learn-goal-link"
          onClick={() => onNavigate('learning_goal', String(goal.id))}
        >
          {goal.topic}
        </button>
        <span className={`learn-status-chip ${goal.status}`}>{goal.status}</span>
      </div>
      <div className="learn-goal-meta">
        {goal.total_concepts > 0
          ? <>{goal.taught_concepts}/{goal.total_concepts} concepts taught · {goal.mastered_concepts} mastered
              {goal.due_reviews > 0 && <> · {goal.due_reviews} due for review</>}</>
          : goal.syllabus_error
            ? <span style={{ color: 'var(--danger)' }}>syllabus failed: {goal.syllabus_error}</span>
            : 'planning syllabus…'}
        {goal.target_date && <> · target {goal.target_date}</>}
      </div>
      {goal.total_concepts > 0 && (
        <div className="learn-progress"><div className="learn-progress-fill" style={{ width: `${pct}%` }} /></div>
      )}
      <div className="learn-goal-actions">
        <button className="chip" onClick={() => onNavigate('learning_goal', String(goal.id))}>
          Open
        </button>
        {goal.status === 'paused'
          ? <button className="chip" onClick={() => act(api.learning.resumeGoal(goal.id))}>Resume</button>
          : goal.status !== 'archived' && (
              <button className="chip" onClick={() => act(api.learning.pauseGoal(goal.id))}>Pause</button>
            )}
        {goal.syllabus_error && (
          <button className="chip" onClick={() => act(api.learning.regenerateSyllabus(goal.id))}>Retry syllabus</button>
        )}
        <button
          className="chip"
          onClick={() => {
            if (window.confirm(`Archive "${goal.topic}"? Lessons and progress are kept.`)) {
              act(api.learning.archiveGoal(goal.id));
            }
          }}
        >
          Archive
        </button>
      </div>
    </div>
  );
}

// ───────────────────────────── Goal view ─────────────────────────────

const CONCEPT_STATUS_LABELS: Record<string, string> = {
  pending: 'upcoming', taught: 'learning', mastered: 'mastered',
};

export function GoalView({ goalId, onNavigate }: { goalId: number | null } & NavProps) {
  const [goal, setGoal] = useState<LearningGoalDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!goalId) { setErr('goal not found'); return; }
    api.learning.goal(goalId)
      .then((g) => { setGoal(g); setErr(null); })
      .catch((e: Error) => setErr(
        // Archived goals drop out of the API — say so instead of a raw 404.
        e.message.includes('not found') ? 'This goal was archived or removed.' : e.message,
      ));
  }, [goalId]);

  useEffect(() => { load(); }, [load]);

  if (err) {
    return (
      <div className="view">
        <div className="view-h">
          <button className="chip" onClick={() => onNavigate('learning')}>← Learning</button>
        </div>
        <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>
          couldn't load goal: {err}
        </p>
      </div>
    );
  }
  if (!goal) {
    return (
      <div className="view">
        <div className="view-h">
          <button className="chip" onClick={() => onNavigate('learning')}>← Learning</button>
        </div>
        <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p>
      </div>
    );
  }

  const pct = goal.total_concepts > 0
    ? Math.round((goal.taught_concepts / goal.total_concepts) * 100)
    : 0;
  const act = (fn: Promise<void>) => fn.then(load).catch(() => load());

  return (
    <div className="view">
      <div className="view-h">
        <button className="chip" onClick={() => onNavigate('learning')}>← Learning</button>
      </div>
      <div className="learn-today-meta">
        {goal.taught_concepts}/{goal.total_concepts || '?'} concepts taught
        · {goal.mastered_concepts} mastered
        {goal.due_reviews > 0 && <> · {goal.due_reviews} due for review</>}
        {goal.target_date && <> · target {goal.target_date}</>}
        {goal.level && <> · level: {goal.level}</>}
        <span className={`learn-status-chip ${goal.status}`} style={{ marginLeft: 8 }}>{goal.status}</span>
      </div>
      <h1 className="view-title">{goal.topic}</h1>
      {goal.description && <p className="view-sub">{goal.description}</p>}

      {goal.total_concepts > 0 && (
        <div className="learn-progress"><div className="learn-progress-fill" style={{ width: `${pct}%` }} /></div>
      )}

      <div className="learn-goal-actions" style={{ marginTop: 10 }}>
        {goal.status === 'paused'
          ? <button className="chip" onClick={() => act(api.learning.resumeGoal(goal.id))}>Resume</button>
          : goal.status !== 'archived' && (
              <button className="chip" onClick={() => act(api.learning.pauseGoal(goal.id))}>Pause</button>
            )}
        {goal.syllabus_error && (
          <button className="chip" onClick={() => act(api.learning.regenerateSyllabus(goal.id))}>Retry syllabus</button>
        )}
        <button
          className="chip"
          onClick={() => {
            if (window.confirm(`Archive "${goal.topic}"? Lessons and progress are kept.`)) {
              api.learning.archiveGoal(goal.id)
                .then(() => onNavigate('learning'))
                .catch(() => load());
            }
          }}
        >
          Archive
        </button>
      </div>

      {goal.syllabus_error && (
        <p style={{ color: 'var(--danger)', fontFamily: 'var(--mono)', fontSize: 12 }}>
          syllabus failed: {goal.syllabus_error}
        </p>
      )}

      <section className="learn-quiz-block">
        <h2 className="learn-sec-heading">Lessons</h2>
        {goal.lessons.length === 0 ? (
          <p style={{ color: 'var(--fg-mute)', fontSize: 13 }}>
            No lessons yet — Guru writes one lesson a day across your active goals.
          </p>
        ) : (
          <div className="learn-lesson-list">
            {goal.lessons.map((l) => (
              <button
                key={l.id}
                className="learn-lesson-row"
                onClick={() => onNavigate('learning_lesson', String(l.id))}
              >
                <span className="learn-lesson-day">Day {l.day_number}</span>
                <span className="learn-lesson-title">{l.title}</span>
                <span className="learn-lesson-date">{l.lesson_date}</span>
                {l.status === 'completed' && <span className="learn-status-chip done">completed</span>}
              </button>
            ))}
          </div>
        )}
      </section>

      <section className="learn-quiz-block">
        <h2 className="learn-sec-heading">Syllabus</h2>
        {goal.syllabus.length === 0 ? (
          <p style={{ color: 'var(--fg-mute)', fontSize: 13 }}>
            {goal.syllabus_error ? 'Syllabus generation failed — retry above.' : 'Guru is planning the syllabus…'}
          </p>
        ) : (
          <ol className="learn-syllabus">
            {goal.syllabus.map((item) => (
              <li key={item.id} className={`learn-syllabus-item ${item.status}`}>
                <div className="learn-syllabus-head">
                  <span className="learn-syllabus-title">{item.title}</span>
                  <span className={`learn-status-chip ${
                    // Distinct concept-level classes: the goal-level 'pending'
                    // chip is warn-orange, wrong for ordinary upcoming concepts.
                    item.status === 'mastered' ? 'done'
                      : item.status === 'taught' ? 'learning' : 'upcoming'
                  }`}>
                    {CONCEPT_STATUS_LABELS[item.status] ?? item.status}
                  </span>
                  {item.status === 'taught' && (
                    <span className="learn-recall-dots" title={`${item.recall_successes ?? 0}/3 recalls toward mastery`}>
                      {[1, 2, 3].map((n) => (
                        <span key={n} className={`learn-recall-dot ${(item.recall_successes ?? 0) >= n ? 'on' : ''}`} />
                      ))}
                    </span>
                  )}
                </div>
                {item.definition && <div className="learn-syllabus-def">{item.definition}</div>}
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}

// ───────────────────────────── Lesson view ─────────────────────────────

export function LessonView({ lessonId, onNavigate }: { lessonId: number | null } & NavProps) {
  const [lesson, setLesson] = useState<Lesson | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [completeErr, setCompleteErr] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!lessonId) return;
    api.learning.lesson(lessonId)
      .then((l) => { setLesson(l); setErr(null); })
      .catch((e: Error) => setErr(e.message));
  }, [lessonId]);

  useEffect(() => { load(); }, [load]);

  if (err) {
    return (
      <div className="view">
        <div className="view-h">Learning</div>
        <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>
          couldn't load lesson: {err}
        </p>
      </div>
    );
  }
  if (!lesson) {
    return (
      <div className="view">
        <div className="view-h">Learning</div>
        <p style={{ color: 'var(--fg-faint)', fontFamily: 'var(--mono)', fontSize: 12 }}>loading…</p>
      </div>
    );
  }

  const warmups = lesson.questions.filter((q) => q.kind === 'warmup');
  const checks = lesson.questions.filter((q) => q.kind === 'check');
  const allAnswered = lesson.questions.length > 0 && lesson.questions.every((q) => q.answered);

  return (
    <div className="view learn-lesson">
      <div className="view-h">
        <button className="chip" onClick={() => onNavigate('learning')}>← Learning</button>
        {lesson.goal_topic && (
          <button className="chip" onClick={() => onNavigate('learning_goal', String(lesson.goal_id))}>
            {lesson.goal_topic}
          </button>
        )}
      </div>
      <div className="learn-today-meta">
        Day {lesson.day_number} · {lesson.goal_topic} · {lesson.lesson_date}
        {lesson.status === 'completed' && <span className="learn-status-chip done" style={{ marginLeft: 8 }}>completed</span>}
      </div>
      <h1 className="view-title">{lesson.title}</h1>

      {warmups.length > 0 && (
        <section className="learn-quiz-block">
          <h2 className="learn-sec-heading">Warmup — recall before you read</h2>
          {warmups.map((q) => <QuestionCard key={q.id} question={q} onGraded={load} />)}
        </section>
      )}

      <div className="art-body">
        {lesson.sections.map((s, i) => (
          <section key={i} className={s.kind === 'callout' ? 'learn-callout' : undefined}>
            {s.heading && <h2 className="learn-sec-heading">{s.heading}</h2>}
            {s.kind === 'diagram'
              ? <MermaidBlock source={s.body_md} />
              : <div className="sec-body" dangerouslySetInnerHTML={{ __html: s.body_html }} />}
          </section>
        ))}
      </div>

      {checks.length > 0 && (
        <section className="learn-quiz-block">
          <h2 className="learn-sec-heading">Check yourself</h2>
          {checks.map((q) => <QuestionCard key={q.id} question={q} onGraded={load} />)}
        </section>
      )}

      {lesson.status !== 'completed' && (
        <div style={{ marginTop: 20 }}>
          <button
            className="chip"
            disabled={!allAnswered && lesson.questions.length > 0}
            title={allAnswered ? undefined : 'Answer every question first'}
            onClick={() => {
              api.learning.completeLesson(lesson.id)
                .then(() => { setCompleteErr(null); load(); })
                .catch((e: Error) => setCompleteErr(e.message));
            }}
          >
            Complete lesson
          </button>
          {completeErr && (
            <div style={{ color: 'var(--warn)', fontFamily: 'var(--mono)', fontSize: 12 }}>{completeErr}</div>
          )}
        </div>
      )}

      <GuruChat key={lesson.id} lessonId={lesson.id} />
    </div>
  );
}

// ───────────────────────────── Lesson chat ─────────────────────────────

function GuruChat({ lessonId }: { lessonId: number }) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<LessonChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || loaded) return;
    api.learning.chat(lessonId)
      .then((r) => { setMessages(r.messages); setLoaded(true); setErr(null); })
      .catch((e: Error) => setErr(e.message));
  }, [open, loaded, lessonId]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, pending, open]);

  const send = () => {
    const text = draft.trim();
    if (!text || busy || !loaded) return;
    setBusy(true);
    setErr(null);
    setDraft('');
    setPending(text);
    api.learning.sendChat(lessonId, text)
      .then((r) => setMessages((prev) => [...prev, ...r.messages]))
      .catch((e: Error) => {
        // Give the failed message back for a one-click retry, but never
        // clobber text the user typed while waiting.
        setDraft((d) => d || text);
        const offline = e instanceof ApiError && e.status === 503;
        setErr(offline ? 'Guru is offline right now — try again in a moment.' : e.message);
      })
      .finally(() => { setPending(null); setBusy(false); });
  };

  if (!open) {
    return (
      <button className="learn-chat-fab" onClick={() => setOpen(true)} title="Chat with Guru about this lesson">
        ◈ Ask Guru
      </button>
    );
  }

  return (
    <div
      className="learn-chat-panel"
      role="dialog"
      aria-label="Chat with Guru about this lesson"
      onKeyDown={(e) => { if (e.key === 'Escape') setOpen(false); }}
    >
      <div className="learn-chat-head">
        <span className="learn-chat-title">◈ Guru</span>
        <button className="learn-chat-close" onClick={() => setOpen(false)} aria-label="Close chat">×</button>
      </div>
      <div className="learn-chat-scroll" ref={scrollRef} aria-live="polite">
        {messages.length === 0 && !pending && loaded && (
          <p className="learn-chat-empty">
            Ask anything about this lesson — a fuzzy concept, a “why”, or a tangent.
            Guru knows the lesson but won't spoil unanswered quiz questions.
          </p>
        )}
        {messages.map((m) => (
          m.role === 'assistant' && m.content_html
            ? <div key={m.id} className="learn-chat-msg guru sec-body" dangerouslySetInnerHTML={{ __html: m.content_html }} />
            : <div key={m.id} className={`learn-chat-msg ${m.role === 'user' ? 'me' : 'guru'}`}>{m.content}</div>
        ))}
        {pending && <div className="learn-chat-msg me">{pending}</div>}
        {busy && <div className="learn-chat-msg guru learn-chat-thinking">thinking…</div>}
        {err && <div className="learn-chat-err">{err}</div>}
      </div>
      <div className="learn-chat-input-row">
        <textarea
          className="learn-chat-input"
          placeholder={loaded ? 'Ask Guru… (Enter to send)' : 'loading chat…'}
          aria-label="Message to Guru"
          value={draft}
          rows={2}
          autoFocus
          disabled={!loaded}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
          }}
        />
        <button className="chip" disabled={busy || !loaded || !draft.trim()} onClick={send}>Send</button>
      </div>
    </div>
  );
}

function QuestionCard({ question, onGraded }: { question: LessonQuestion; onGraded: () => void }) {
  const [answer, setAnswer] = useState('');
  const [confidence, setConfidence] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [needSelfGrade, setNeedSelfGrade] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<LearningAnswerResult | null>(null);

  const submit = (selfRating?: number) => {
    const a = answer.trim();
    if (!a || busy) return;
    setBusy(true);
    setErr(null);
    const body: { answer: string; confidence?: number; self_rating?: number } = { answer: a };
    if (confidence) body.confidence = confidence;
    if (selfRating) body.self_rating = selfRating;
    api.learning.answer(question.id, body)
      .then((r) => { setResult(r); setNeedSelfGrade(false); onGraded(); })
      .catch((e: Error) => {
        if (e.message.toLowerCase().includes('self_rating') || e.message.includes('503')) {
          setNeedSelfGrade(true);
          setErr('Grading is offline — rate your own recall against the rubric.');
        } else {
          setErr(e.message);
        }
      })
      .finally(() => setBusy(false));
  };

  // Already graded (from a previous visit) — render the stored grade.
  if (question.answered && !result) {
    return (
      <div className="learn-question graded">
        <div className="learn-q-text">{question.question}</div>
        <div className="learn-q-answer">{question.answer_text}</div>
        <GradePanel
          rating={question.rating ?? 0}
          feedback={question.grade?.feedback || ''}
          evidence={question.grade?.evidence || []}
          rubric={question.rubric}
          questionId={question.id}
          onOverridden={onGraded}
        />
      </div>
    );
  }

  if (result) {
    return (
      <div className="learn-question graded">
        <div className="learn-q-text">{question.question}</div>
        <div className="learn-q-answer">{answer}</div>
        <GradePanel
          rating={result.rating}
          feedback={result.feedback}
          evidence={result.evidence}
          rubric={result.rubric}
          questionId={question.id}
          onOverridden={(newRating) => {
            // Keep local state in sync: this card stays mounted, so without
            // this the chip would keep showing the pre-override rating.
            setResult({ ...result, rating: newRating });
            onGraded();
          }}
          mastered={result.mastered}
          conceptTitle={result.concept_title}
        />
      </div>
    );
  }

  return (
    <div className="learn-question">
      <div className="learn-q-text">{question.question}</div>
      <textarea
        className="learn-q-input"
        placeholder="Answer from memory — no peeking. Free recall is the whole point."
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        rows={3}
      />
      <div className="learn-q-foot">
        <span className="learn-q-conf-label">Confidence:</span>
        {[1, 2, 3, 4].map((c) => (
          <button
            key={c}
            className={`chip ${confidence === c ? 'learn-conf-active' : ''}`}
            onClick={() => setConfidence(c)}
          >
            {CONFIDENCE_LABELS[c]}
          </button>
        ))}
        {needSelfGrade ? (
          <>
            <span className="learn-q-conf-label">Self-grade:</span>
            {[1, 2, 3, 4].map((r) => (
              <button key={r} className="chip" disabled={busy} onClick={() => submit(r)}>
                {RATING_LABELS[r]}
              </button>
            ))}
          </>
        ) : (
          <button className="chip" disabled={busy || !answer.trim()} onClick={() => submit()}>
            {busy ? 'Grading…' : 'Grade me'}
          </button>
        )}
      </div>
      {err && <div style={{ color: 'var(--warn)', fontFamily: 'var(--mono)', fontSize: 12 }}>{err}</div>}
    </div>
  );
}

function GradePanel({
  rating, feedback, evidence, rubric, questionId, onOverridden, mastered, conceptTitle,
}: {
  rating: number;
  feedback: string;
  evidence: { key_point: string; met: boolean; quote: string | null }[];
  rubric?: { key_points: string[]; common_mistakes: string[]; model_answer: string };
  questionId: number;
  onOverridden: (newRating: number) => void;
  mastered?: boolean;
  conceptTitle?: string;
}) {
  const [overriding, setOverriding] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="learn-grade">
      <div className="learn-grade-head">
        <span className={`learn-rating r${rating}`}>{RATING_LABELS[rating] ?? rating}</span>
        {mastered && conceptTitle && (
          <span className="learn-status-chip done">mastered: {conceptTitle}</span>
        )}
      </div>
      {feedback && <p className="learn-grade-feedback">{feedback}</p>}
      {evidence.length > 0 && (
        <ul className="learn-evidence">
          {evidence.map((e, i) => (
            <li key={i} className={e.met ? 'met' : 'missed'}>
              {e.met ? '✓' : '✗'} {e.key_point}
              {e.quote && <span className="learn-evidence-quote"> — “{e.quote}”</span>}
            </li>
          ))}
        </ul>
      )}
      {rubric?.model_answer && (
        <p className="learn-model-answer"><strong>Model answer:</strong> {rubric.model_answer}</p>
      )}
      <div className="learn-q-foot">
        {overriding ? (
          <>
            <span className="learn-q-conf-label">Set grade:</span>
            {[1, 2, 3, 4].map((r) => (
              <button
                key={r}
                className="chip"
                onClick={() => {
                  api.learning.override(questionId, r)
                    .then((res) => { setOverriding(false); setErr(null); onOverridden(res.rating); })
                    .catch((e: Error) => { setOverriding(false); setErr(e.message); });
                }}
              >
                {RATING_LABELS[r]}
              </button>
            ))}
          </>
        ) : (
          <button className="chip" onClick={() => setOverriding(true)}>Override grade</button>
        )}
      </div>
      {err && <div style={{ color: 'var(--warn)', fontFamily: 'var(--mono)', fontSize: 12 }}>{err}</div>}
    </div>
  );
}
