import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { ArticlePreview } from '../types';
import { SafeHtml } from './SafeHtml';

// Tuning knobs. Defaults match the spec — 300ms to open, 150ms grace to move
// the cursor from the link into the card without it disappearing mid-traverse.
const OPEN_DELAY_MS = 300;
const CLOSE_GRACE_MS = 150;
const CARD_ID = 'wiki-hover-card';
const CARD_WIDTH = 280;           // matches .wiki-hover-card max-width in CSS
const ESTIMATED_CARD_HEIGHT = 140; // only used for above/below flip; final size is measured
const GAP = 8;                     // px between link edge and card edge
const VIEWPORT_PAD = 8;            // px from viewport edge we never cross

// Tiny in-memory LRU keyed by article id. Hovers are bursty and usually repeat
// (same links in the reader rail), so even a 64-entry cache effectively
// eliminates the network for a single reading session.
const CACHE_CAPACITY = 64;
const cache = new Map<string, ArticlePreview>();
function cacheGet(id: string): ArticlePreview | undefined {
  const hit = cache.get(id);
  if (hit === undefined) return undefined;
  // Touch: delete + reinsert to mark as most-recently-used.
  cache.delete(id);
  cache.set(id, hit);
  return hit;
}
function cacheSet(id: string, v: ArticlePreview): void {
  if (cache.has(id)) cache.delete(id);
  cache.set(id, v);
  while (cache.size > CACHE_CAPACITY) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

interface Placement { left: number; top: number; flipped: boolean; }

/**
 * Compute popover position relative to the viewport.
 *
 * Default: below the link, anchored to its left edge. If the card would clip
 * off the bottom of the viewport AND there is more room above than below, we
 * flip to above. Horizontally we always clamp into the viewport with
 * VIEWPORT_PAD breathing room.
 *
 * Returns fixed-position coordinates — the card renders into a portal at the
 * document root and uses ``position: fixed`` so it is immune to the main
 * element's scroll offset.
 */
function placeCard(
  anchor: DOMRect,
  cardW: number,
  cardH: number,
  vw: number,
  vh: number,
): Placement {
  // Horizontal: anchor to link's left; clamp into viewport.
  let left = anchor.left;
  if (left + cardW + VIEWPORT_PAD > vw) left = vw - cardW - VIEWPORT_PAD;
  if (left < VIEWPORT_PAD) left = VIEWPORT_PAD;

  // Vertical: below by default. Flip to above only if we'd clip AND above has more room.
  const spaceBelow = vh - anchor.bottom - GAP;
  const spaceAbove = anchor.top - GAP;
  const flipped = cardH > spaceBelow && spaceAbove > spaceBelow;
  const top = flipped ? anchor.top - GAP - cardH : anchor.bottom + GAP;

  return { left, top, flipped };
}

interface Anchor { rect: DOMRect; target: string; linkEl: HTMLElement; }

export function WikiLinkHoverProvider() {
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const [preview, setPreview] = useState<ArticlePreview | null>(null);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const cardRef = useRef<HTMLDivElement | null>(null);

  // Timers are refs so handlers (which are recreated each event) share them.
  const openTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  // Track which link "won" the hover race — guards against stale async preview
  // fetches resolving after the user has already moved on to a different link.
  const pendingTarget = useRef<string | null>(null);
  // Track whether the cursor is currently inside the card so we don't hide it
  // mid-move when the cursor crosses the gap between link and card.
  const insideCard = useRef<boolean>(false);

  useEffect(() => {
    const clearOpen = () => {
      if (openTimer.current !== null) {
        window.clearTimeout(openTimer.current);
        openTimer.current = null;
      }
    };
    const clearClose = () => {
      if (closeTimer.current !== null) {
        window.clearTimeout(closeTimer.current);
        closeTimer.current = null;
      }
    };

    const dismiss = () => {
      pendingTarget.current = null;
      setAnchor((prev) => {
        if (prev) prev.linkEl.removeAttribute('aria-describedby');
        return null;
      });
      setPreview(null);
      setPlacement(null);
    };

    const scheduleOpen = (linkEl: HTMLElement, target: string) => {
      clearOpen();
      clearClose();
      pendingTarget.current = target;
      openTimer.current = window.setTimeout(() => {
        // Re-measure at fire time — the link may have reflowed during the wait.
        const rect = linkEl.getBoundingClientRect();
        linkEl.setAttribute('aria-describedby', CARD_ID);
        setAnchor({ rect, target, linkEl });

        const cached = cacheGet(target);
        if (cached) {
          setPreview(cached);
        } else {
          setPreview(null); // loading state
          api.articlePreview(target)
            .then((p) => {
              cacheSet(target, p);
              // Only apply if this target is still the one the user is on.
              if (pendingTarget.current === target) setPreview(p);
            })
            .catch(() => {
              // Treat a network/server error as "not available" so the card
              // still gives the user something honest rather than a spinner
              // that never resolves.
              if (pendingTarget.current === target) {
                setPreview({ id: target, exists: false });
              }
            });
        }
      }, OPEN_DELAY_MS);
    };

    const scheduleClose = () => {
      clearOpen();
      clearClose();
      closeTimer.current = window.setTimeout(() => {
        if (!insideCard.current) dismiss();
      }, CLOSE_GRACE_MS);
    };

    const onOver = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (!t) return;
      // Ignore hovers that originate inside the card — those are handled by
      // the card's own onMouseEnter, not the document-level listener.
      if (cardRef.current && cardRef.current.contains(t)) return;

      const linkEl = t.closest<HTMLElement>('.link[data-target]');
      if (!linkEl) return;
      const target = linkEl.getAttribute('data-target');
      if (!target) return;

      // If we're already showing a card for this same link, do nothing —
      // otherwise mouseover bubbling (re-firing on every inner element the
      // cursor crosses) would reset the debounce.
      if (anchor && anchor.linkEl === linkEl) {
        clearClose();
        return;
      }
      scheduleOpen(linkEl, target);
    };

    const onOut = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (!t) return;
      const linkEl = t.closest<HTMLElement>('.link[data-target]');
      if (!linkEl) return;
      // relatedTarget is the element we moved INTO. If it's still inside the
      // same link (text nodes etc.) or inside the card, don't close.
      const related = e.relatedTarget as Node | null;
      if (related && (linkEl.contains(related) || cardRef.current?.contains(related))) return;
      scheduleClose();
    };

    const onScroll = () => dismiss();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') dismiss(); };

    document.addEventListener('mouseover', onOver);
    document.addEventListener('mouseout', onOut);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('keydown', onKey);
    return () => {
      clearOpen();
      clearClose();
      document.removeEventListener('mouseover', onOver);
      document.removeEventListener('mouseout', onOut);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('keydown', onKey);
    };
    // Intentionally depends on `anchor` so the "same link" short-circuit sees
    // the latest value — the listeners are cheap to re-register.
  }, [anchor]);

  // Position pass: when anchor changes, place the card using an estimated
  // height, then on the next frame re-measure and correct if needed so the
  // flip decision uses the real height. This avoids a flash of wrong position
  // while still letting us anchor via fixed coords from the first render.
  useEffect(() => {
    if (!anchor) { setPlacement(null); return; }
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    setPlacement(placeCard(anchor.rect, CARD_WIDTH, ESTIMATED_CARD_HEIGHT, vw, vh));
    const id = window.requestAnimationFrame(() => {
      const el = cardRef.current;
      if (!el) return;
      const h = el.offsetHeight || ESTIMATED_CARD_HEIGHT;
      setPlacement(placeCard(anchor.rect, CARD_WIDTH, h, vw, vh));
    });
    return () => window.cancelAnimationFrame(id);
  }, [anchor, preview]);

  if (!anchor || !placement) return null;

  return (
    <div
      ref={cardRef}
      id={CARD_ID}
      role="tooltip"
      className={`wiki-hover-card${placement.flipped ? ' flipped' : ''}`}
      style={{ left: placement.left, top: placement.top }}
      onMouseEnter={() => {
        insideCard.current = true;
        if (closeTimer.current !== null) {
          window.clearTimeout(closeTimer.current);
          closeTimer.current = null;
        }
      }}
      onMouseLeave={() => {
        insideCard.current = false;
        // Reuse the same close timer semantics on leave from the card itself.
        closeTimer.current = window.setTimeout(() => {
          if (anchor) anchor.linkEl.removeAttribute('aria-describedby');
          setAnchor(null);
          setPreview(null);
          setPlacement(null);
        }, CLOSE_GRACE_MS);
      }}
    >
      <WikiHoverBody preview={preview} targetId={anchor.target}/>
    </div>
  );
}

function WikiHoverBody({ preview, targetId }: { preview: ArticlePreview | null; targetId: string }) {
  if (preview === null) {
    return (
      <div className="wh-loading">
        <div className="wh-kind-row">
          <span className="wh-kind-badge loading"/>
        </div>
        <div className="wh-title-skel"/>
        <div className="wh-summary-skel"/>
      </div>
    );
  }
  if (!preview.exists) {
    return (
      <>
        <div className="wh-kind-row">
          <span className="wh-kind-badge missing">not yet compiled</span>
          <span className="wh-slug">{targetId}</span>
        </div>
        <div className="wh-missing-msg">
          The Compiler hasn't produced this article yet. The link is a
          placeholder until an agent fills it in.
        </div>
      </>
    );
  }
  return (
    <>
      <div className="wh-kind-row">
        <span className={`wh-kind-badge ${preview.kind.toLowerCase()}`}>
          <span className="wh-kind-dot"/>{preview.kind}
        </span>
      </div>
      <div className="wh-title">{preview.title}</div>
      {preview.summary && (
        <SafeHtml className="wh-summary" html={preview.summary}/>
      )}
    </>
  );
}
