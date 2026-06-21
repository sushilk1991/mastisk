import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode, RefObject } from 'react';
import { api } from '../api';
import type {
  BookDetail, BookStatus, BookSummary, KindleImportResult, KindleReviewItem,
  QuoteDetail, QuoteSourceType, QuoteSummary, View,
} from '../types';

interface Props {
  liveKey: string;
  initialBookSlug: string | null;
  initialQuoteId: string | null;
  onNavigate: (view: View, id?: string) => void;
}

const STATUSES: (BookStatus | 'all')[] = ['all', 'want', 'reading', 'finished', 'abandoned'];
const SOURCE_TYPES: QuoteSourceType[] = ['book', 'article', 'podcast', 'conversation'];

type Tab = 'books' | 'quotes' | 'kindle';

export function LibraryView({ liveKey, initialBookSlug, initialQuoteId, onNavigate }: Props) {
  const [tab, setTab] = useState<Tab>(initialQuoteId ? 'quotes' : 'books');
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);

  const [bookStatus, setBookStatus] = useState<BookStatus | 'all'>('all');
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [selectedBook, setSelectedBook] = useState<string | null>(initialBookSlug);
  const [bookDetail, setBookDetail] = useState<BookDetail | null>(null);
  const [bookTitle, setBookTitle] = useState('');
  const [bookAuthor, setBookAuthor] = useState('');
  const [bookLookup, setBookLookup] = useState(true);
  const [highlightText, setHighlightText] = useState('');

  const [quotes, setQuotes] = useState<QuoteSummary[]>([]);
  const [selectedQuote, setSelectedQuote] = useState<string | null>(initialQuoteId);
  const [quoteDetail, setQuoteDetail] = useState<QuoteDetail | null>(null);
  const [quoteText, setQuoteText] = useState('');
  const [quoteSourceType, setQuoteSourceType] = useState<QuoteSourceType>('conversation');
  const [quoteSourceRef, setQuoteSourceRef] = useState('');
  const [quoteTags, setQuoteTags] = useState('');
  const [thoughtText, setThoughtText] = useState('');

  const [reviewItems, setReviewItems] = useState<KindleReviewItem[]>([]);
  const [importResult, setImportResult] = useState<KindleImportResult | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function loadBooks(nextStatus = bookStatus) {
    const rows = await api.libraryApi.books.list(nextStatus === 'all' ? undefined : nextStatus);
    setBooks(rows);
    setSelectedBook((current) => current || initialBookSlug || rows[0]?.slug || null);
  }

  async function loadBook(slug: string | null) {
    if (!slug) {
      setBookDetail(null);
      return;
    }
    setBookDetail(await api.libraryApi.books.get(slug));
  }

  async function loadQuotes() {
    const rows = await api.libraryApi.quotes.list();
    setQuotes(rows);
    setSelectedQuote((current) => current || initialQuoteId || rows[0]?.id || null);
  }

  async function loadQuote(id: string | null) {
    if (!id) {
      setQuoteDetail(null);
      return;
    }
    setQuoteDetail(await api.libraryApi.quotes.get(id));
  }

  async function loadReview() {
    setReviewItems(await api.libraryApi.kindle.review());
  }

  async function loadAll() {
    setErr(null);
    setLoading(true);
    try {
      await Promise.all([loadBooks(), loadQuotes(), loadReview()]);
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (initialBookSlug) {
      setTab('books');
      setSelectedBook(initialBookSlug);
    }
    if (initialQuoteId) {
      setTab('quotes');
      setSelectedQuote(initialQuoteId);
    }
  }, [initialBookSlug, initialQuoteId]);

  useEffect(() => { void loadAll(); }, [liveKey]);
  useEffect(() => { void loadBook(selectedBook).catch((e) => setErr(errorMessage(e))); }, [selectedBook, liveKey]);
  useEffect(() => { void loadQuote(selectedQuote).catch((e) => setErr(errorMessage(e))); }, [selectedQuote, liveKey]);

  async function createBook(e: FormEvent) {
    e.preventDefault();
    const title = bookTitle.trim();
    if (!title) return;
    setErr(null);
    try {
      const created = await api.libraryApi.books.create({
        title,
        author: bookAuthor.trim() || null,
        lookup: bookLookup,
      });
      setBooks((current) => [created, ...current.filter((book) => book.slug !== created.slug)]);
      setBookTitle('');
      setBookAuthor('');
      setShowCreate(false);
      await loadBooks();
      setSelectedBook(created.slug);
      onNavigate('library', `book:${created.slug}`);
    } catch (ex) {
      setErr(errorMessage(ex));
    }
  }

  async function patchBook(updates: Parameters<typeof api.libraryApi.books.patch>[1]) {
    if (!bookDetail) return;
    setErr(null);
    try {
      const updated = await api.libraryApi.books.patch(bookDetail.slug, updates);
      setBookDetail(updated);
      await loadBooks();
    } catch (ex) {
      setErr(errorMessage(ex));
    }
  }

  async function refreshBook() {
    if (!bookDetail) return;
    setErr(null);
    try {
      const updated = await api.libraryApi.books.refresh(bookDetail.slug);
      setBookDetail(updated);
      await loadBooks();
    } catch (ex) {
      setErr(errorMessage(ex));
    }
  }

  async function addHighlight(e: FormEvent) {
    e.preventDefault();
    if (!bookDetail) return;
    const text = highlightText.trim();
    if (!text) return;
    setErr(null);
    try {
      await api.libraryApi.books.addHighlight(bookDetail.slug, text);
      setHighlightText('');
      await loadBook(bookDetail.slug);
      await loadQuotes();
    } catch (ex) {
      setErr(errorMessage(ex));
    }
  }

  async function createQuote(e: FormEvent) {
    e.preventDefault();
    const text = quoteText.trim();
    if (!text) return;
    setErr(null);
    try {
      const created = await api.libraryApi.quotes.create({
        text,
        source_type: quoteSourceType,
        source_ref: quoteSourceRef.trim() || null,
        tags: splitTags(quoteTags),
      });
      setQuotes((current) => [created, ...current.filter((quote) => quote.id !== created.id)]);
      setQuoteText('');
      setQuoteSourceRef('');
      setQuoteTags('');
      setShowCreate(false);
      await loadQuotes();
      setSelectedQuote(created.id);
      onNavigate('library', `quote:${created.id}`);
    } catch (ex) {
      setErr(errorMessage(ex));
    }
  }

  async function addThought(e: FormEvent) {
    e.preventDefault();
    if (!quoteDetail) return;
    const text = thoughtText.trim();
    if (!text) return;
    setErr(null);
    try {
      const updated = await api.libraryApi.quotes.addThought(quoteDetail.id, text);
      setThoughtText('');
      setQuoteDetail(updated);
    } catch (ex) {
      setErr(errorMessage(ex));
    }
  }

  async function importKindle(e: FormEvent) {
    e.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setImportBusy(true);
    setErr(null);
    try {
      const result = await api.libraryApi.kindle.importFile(file);
      setImportResult(result);
      if (fileRef.current) fileRef.current.value = '';
      await Promise.all([loadBooks(), loadQuotes(), loadReview()]);
    } catch (ex) {
      setErr(errorMessage(ex));
    } finally {
      setImportBusy(false);
    }
  }

  async function retryReview(item: KindleReviewItem) {
    setErr(null);
    try {
      await api.libraryApi.kindle.retryAsQuote(item.id, {
        text: item.parsed_content || item.raw_block,
        source_type: 'conversation',
        tags: ['kindle-review'],
      });
      await Promise.all([loadQuotes(), loadReview()]);
    } catch (ex) {
      setErr(errorMessage(ex));
    }
  }

  async function dismissReview(item: KindleReviewItem) {
    setErr(null);
    try {
      await api.libraryApi.kindle.dismiss(item.id);
      await loadReview();
    } catch (ex) {
      setErr(errorMessage(ex));
    }
  }

  const createLabel = tab === 'books' ? '+ New book' : tab === 'quotes' ? '+ New quote' : '+ Import';

  return (
    <div className="view dash-view library-view">
      <header className="view-head">
        <div className="view-head-copy">
          <div className="view-h">Personal OS</div>
          <h1 className="view-title">Library</h1>
          <p className="view-sub">Books, quotes, and imported highlights worth keeping close.</p>
        </div>
        <div className="view-head-actions">
          <button className="new-action" type="button" onClick={() => setShowCreate((value) => !value)}>
            {showCreate ? 'Close' : createLabel}
          </button>
        </div>
      </header>

      <div className="dash-tabs" role="tablist" aria-label="Library sections">
        <button
          className={tab === 'books' ? 'active' : ''}
          type="button"
          role="tab"
          aria-selected={tab === 'books'}
          onClick={() => { setTab('books'); setShowCreate(false); }}
        >
          Books {books.length}
        </button>
        <button
          className={tab === 'quotes' ? 'active' : ''}
          type="button"
          role="tab"
          aria-selected={tab === 'quotes'}
          onClick={() => { setTab('quotes'); setShowCreate(false); }}
        >
          Quotes {quotes.length}
        </button>
        <button
          className={tab === 'kindle' ? 'active' : ''}
          type="button"
          role="tab"
          aria-selected={tab === 'kindle'}
          onClick={() => { setTab('kindle'); setShowCreate(false); }}
        >
          Kindle {reviewItems.length}
        </button>
      </div>

      {err && <p className="dash-error">{err}</p>}
      {loading && <LibrarySkeleton/>}

      {!loading && tab === 'books' && (
        <BooksTab
          books={books}
          status={bookStatus}
          detail={bookDetail}
          showCreate={showCreate}
          selected={selectedBook}
          title={bookTitle}
          author={bookAuthor}
          lookup={bookLookup}
          highlightText={highlightText}
          onStatus={async (status) => {
            setBookStatus(status);
            await loadBooks(status);
          }}
          onSelect={(slug) => {
            setSelectedBook(slug);
            onNavigate('library', `book:${slug}`);
          }}
          onTitle={setBookTitle}
          onAuthor={setBookAuthor}
          onLookup={setBookLookup}
          onCreate={createBook}
          onPatch={patchBook}
          onRefresh={refreshBook}
          onHighlightText={setHighlightText}
          onAddHighlight={addHighlight}
          onQuote={(id) => {
            setTab('quotes');
            setSelectedQuote(id);
            onNavigate('library', `quote:${id}`);
          }}
          onRequestCreate={() => setShowCreate(true)}
        />
      )}

      {!loading && tab === 'quotes' && (
        <QuotesTab
          quotes={quotes}
          detail={quoteDetail}
          selected={selectedQuote}
          showCreate={showCreate}
          text={quoteText}
          sourceType={quoteSourceType}
          sourceRef={quoteSourceRef}
          tags={quoteTags}
          thoughtText={thoughtText}
          onSelect={(id) => {
            setSelectedQuote(id);
            onNavigate('library', `quote:${id}`);
          }}
          onText={setQuoteText}
          onSourceType={setQuoteSourceType}
          onSourceRef={setQuoteSourceRef}
          onTags={setQuoteTags}
          onCreate={createQuote}
          onThoughtText={setThoughtText}
          onAddThought={addThought}
          onRequestCreate={() => setShowCreate(true)}
        />
      )}

      {!loading && tab === 'kindle' && (
        <KindleTab
          fileRef={fileRef}
          result={importResult}
          busy={importBusy}
          reviewItems={reviewItems}
          showCreate={showCreate}
          onImport={importKindle}
          onRetry={retryReview}
          onDismiss={dismissReview}
        />
      )}
    </div>
  );
}

function BooksTab({
  books, status, detail, selected, showCreate, title, author, lookup, highlightText,
  onStatus, onSelect, onTitle, onAuthor, onLookup, onCreate, onPatch,
  onRefresh, onHighlightText, onAddHighlight, onQuote, onRequestCreate,
}: {
  books: BookSummary[];
  status: BookStatus | 'all';
  detail: BookDetail | null;
  selected: string | null;
  showCreate: boolean;
  title: string;
  author: string;
  lookup: boolean;
  highlightText: string;
  onStatus: (status: BookStatus | 'all') => Promise<void>;
  onSelect: (slug: string) => void;
  onTitle: (value: string) => void;
  onAuthor: (value: string) => void;
  onLookup: (value: boolean) => void;
  onCreate: (e: FormEvent) => void;
  onPatch: (updates: Parameters<typeof api.libraryApi.books.patch>[1]) => Promise<void>;
  onRefresh: () => Promise<void>;
  onHighlightText: (value: string) => void;
  onAddHighlight: (e: FormEvent) => void;
  onQuote: (id: string) => void;
  onRequestCreate: () => void;
}) {
  return (
    <div className="library-layout">
      <section className="library-list-pane">
        {showCreate && (
          <section className="create-panel library-create-panel" aria-label="New book">
            <div className="create-panel-title">New book</div>
            <form className="dash-inline-form library-create" onSubmit={onCreate}>
              <input value={title} onChange={(e) => onTitle(e.target.value)} placeholder="Title"/>
              <input value={author} onChange={(e) => onAuthor(e.target.value)} placeholder="Author"/>
              <label className="library-check">
                <input type="checkbox" checked={lookup} onChange={(e) => onLookup(e.target.checked)}/>
                <span>lookup metadata</span>
              </label>
              <button type="submit">Add book</button>
            </form>
          </section>
        )}
        <div className="library-filter-row">
          {STATUSES.map((item) => (
            <button key={item} className={status === item ? 'active' : ''} onClick={() => void onStatus(item)}>
              {item}
            </button>
          ))}
        </div>
        {books.length === 0 ? (
          <EmptyState action={{ label: '+ New book', onClick: onRequestCreate }}>No books yet. Add the first one from here.</EmptyState>
        ) : (
          <div className="library-card-grid">
            {books.map((book) => (
              <button
                key={book.slug}
                className={`library-book-card ${selected === book.slug ? 'active' : ''}`}
                onClick={() => onSelect(book.slug)}
              >
                {book.cover_url ? (
                  <img src={book.cover_url} alt=""/>
                ) : (
                  <span className="library-cover-placeholder">{book.title.slice(0, 1).toUpperCase()}</span>
                )}
                <span className="library-card-main">
                  <b>{book.title}</b>
                  <em>{book.author || 'Unknown author'}</em>
                  <span>{book.status} · {book.highlight_count} highlights</span>
                </span>
              </button>
            ))}
          </div>
        )}
      </section>
      <section className="library-detail-pane">
        {!detail ? <EmptyLine>Select a book.</EmptyLine> : (
          <>
            <div className="dash-section-head">
              <div>
                <h2>{detail.title}</h2>
                <p className="dash-muted">{detail.author || 'Unknown author'}</p>
              </div>
              <button className="chip" onClick={() => void onRefresh()}>refresh metadata</button>
            </div>
            <div className="library-book-controls">
              <select value={detail.status} onChange={(e) => void onPatch({ status: e.target.value })}>
                {STATUSES.filter((item) => item !== 'all').map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
              <div className="scale-setter">
                <span>Rating</span>
                {[1, 2, 3, 4, 5].map((value) => (
                  <button
                    key={value}
                    className={detail.rating === value ? 'active' : ''}
                    onClick={() => void onPatch({ rating: detail.rating === value ? null : value })}
                  >
                    {value}
                  </button>
                ))}
              </div>
            </div>
            {detail.summary && <p className="library-summary">{detail.summary}</p>}
            <form className="dash-inline-form" onSubmit={onAddHighlight}>
              <input value={highlightText} onChange={(e) => onHighlightText(e.target.value)} placeholder="Add highlight"/>
              <button type="submit">add</button>
            </form>
            <h3 className="dash-mini-h">Highlights</h3>
            {detail.highlights.length === 0 ? <EmptyLine>No highlights yet.</EmptyLine> : (
              <div className="dash-list compact">
                {detail.highlights.map((highlight) => (
                  <div className="dash-row library-highlight-row" key={highlight.id}>
                    <span>{highlight.text}</span>
                    {highlight.quote_id && (
                      <button className="chip muted" onClick={() => onQuote(highlight.quote_id!)}>quote</button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}

function QuotesTab({
  quotes, detail, selected, showCreate, text, sourceType, sourceRef, tags, thoughtText,
  onSelect, onText, onSourceType, onSourceRef, onTags, onCreate,
  onThoughtText, onAddThought, onRequestCreate,
}: {
  quotes: QuoteSummary[];
  detail: QuoteDetail | null;
  selected: string | null;
  showCreate: boolean;
  text: string;
  sourceType: QuoteSourceType;
  sourceRef: string;
  tags: string;
  thoughtText: string;
  onSelect: (id: string) => void;
  onText: (value: string) => void;
  onSourceType: (value: QuoteSourceType) => void;
  onSourceRef: (value: string) => void;
  onTags: (value: string) => void;
  onCreate: (e: FormEvent) => void;
  onThoughtText: (value: string) => void;
  onAddThought: (e: FormEvent) => void;
  onRequestCreate: () => void;
}) {
  return (
    <div className="library-layout">
      <section className="library-list-pane">
        {showCreate && (
          <section className="create-panel library-create-panel" aria-label="New quote">
            <div className="create-panel-title">New quote</div>
            <form className="library-quote-create" onSubmit={onCreate}>
              <textarea value={text} onChange={(e) => onText(e.target.value)} placeholder="Quote text"/>
              <div className="dash-inline-form">
                <select value={sourceType} onChange={(e) => onSourceType(e.target.value as QuoteSourceType)}>
                  {SOURCE_TYPES.map((item) => <option key={item} value={item}>{item}</option>)}
                </select>
                <input value={sourceRef} onChange={(e) => onSourceRef(e.target.value)} placeholder="Source"/>
                <input value={tags} onChange={(e) => onTags(e.target.value)} placeholder="Tags"/>
                <button type="submit">Save quote</button>
              </div>
            </form>
          </section>
        )}
        {quotes.length === 0 ? <EmptyState action={{ label: '+ New quote', onClick: onRequestCreate }}>No quotes yet. Capture one from scratch.</EmptyState> : (
          <div className="dash-list">
            {quotes.map((quote) => (
              <button
                key={quote.id}
                className={`dash-card library-quote-card ${selected === quote.id ? 'active' : ''}`}
                onClick={() => onSelect(quote.id)}
              >
                <p>{quote.text}</p>
                <span className="dash-tags">
                  <span>{quote.source_type}</span>
                  {quote.tags.map((tag) => <span key={tag}>{tag}</span>)}
                </span>
              </button>
            ))}
          </div>
        )}
      </section>
      <section className="library-detail-pane">
        {!detail ? <EmptyLine>Select a quote.</EmptyLine> : (
          <>
            <div className="dash-section-head">
              <h2>Quote</h2>
              <span className="dash-pill">{detail.source_type}</span>
            </div>
            <blockquote className="library-quote-detail">{detail.text}</blockquote>
            <div className="dash-tags">
              {detail.source_ref && <span>{detail.source_ref}</span>}
              {detail.tags.map((tag) => <span key={tag}>{tag}</span>)}
            </div>
            <form className="dash-inline-form" onSubmit={onAddThought}>
              <input value={thoughtText} onChange={(e) => onThoughtText(e.target.value)} placeholder="Add thought"/>
              <button type="submit">add</button>
            </form>
            <h3 className="dash-mini-h">Thoughts</h3>
            {detail.thoughts.length === 0 ? <EmptyLine>No thoughts yet.</EmptyLine> : (
              <div className="dash-list compact">
                {detail.thoughts.map((thought) => (
                  <div className="dash-row library-thought-row" key={`${thought.ts}:${thought.text}`}>
                    <span className="dash-muted">{thought.ts}</span>
                    <b>{thought.text}</b>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}

function KindleTab({
  fileRef, result, busy, reviewItems, showCreate, onImport, onRetry, onDismiss,
}: {
  fileRef: RefObject<HTMLInputElement>;
  result: KindleImportResult | null;
  busy: boolean;
  reviewItems: KindleReviewItem[];
  showCreate: boolean;
  onImport: (e: FormEvent) => void;
  onRetry: (item: KindleReviewItem) => Promise<void>;
  onDismiss: (item: KindleReviewItem) => Promise<void>;
}) {
  return (
    <div className="library-layout single">
      <section className="library-list-pane">
        {showCreate && (
          <section className="create-panel library-create-panel" aria-label="Import Kindle highlights">
            <div className="create-panel-title">Import Kindle highlights</div>
            <form className="dash-inline-form library-import-form" onSubmit={onImport}>
              <input ref={fileRef} type="file" accept=".txt,text/plain"/>
              <button type="submit" disabled={busy}>{busy ? 'importing' : 'Import'}</button>
            </form>
          </section>
        )}
        {result && (
          <div className="dash-tags">
            <span>{result.imported} imported</span>
            <span>{result.skipped_duplicates} duplicates</span>
            <span>{result.review_count} review</span>
          </div>
        )}
        <h2>Import review</h2>
        {reviewItems.length === 0 ? <EmptyState>No review items.</EmptyState> : (
          <div className="dash-list">
            {reviewItems.map((item) => (
              <div className="dash-card library-review-card" key={item.id}>
                <div className="dash-tags">
                  <span>{item.reason}</span>
                  {item.parsed_title && <span>{item.parsed_title}</span>}
                </div>
                <pre>{item.raw_block}</pre>
                <div className="dash-actions">
                  <button className="chip" onClick={() => void onRetry(item)}>retry as quote</button>
                  <button className="chip muted" onClick={() => void onDismiss(item)}>dismiss</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function EmptyLine({ children }: { children: ReactNode }) {
  return <p className="dash-empty">{children}</p>;
}

function EmptyState({
  children,
  action,
}: {
  children: ReactNode;
  action?: { label: string; onClick: () => void };
}) {
  return (
    <div className="dash-empty-state">
      <p>{children}</p>
      {action && <button className="chip" type="button" onClick={action.onClick}>{action.label}</button>}
    </div>
  );
}

function LibrarySkeleton() {
  return (
    <div className="dash-skeleton library-skeleton" aria-label="Loading library">
      <span/>
      <span/>
      <span/>
      <span/>
    </div>
  );
}

function splitTags(value: string): string[] {
  return value.split(/[,#]/).map((item) => item.trim()).filter(Boolean);
}

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : 'failed';
}
