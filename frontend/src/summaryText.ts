import { htmlToText } from './sanitize';

// Summaries are written by the model as a single italic sentence, so they
// arrive wrapped in an emphasis marker: `*…*`, `_…_`, `<em>…</em>` or
// `<i>…</i>`. Every surface that shows a summary already styles it, so the
// wrapper is noise — and where the summary is rendered as text it shows up as
// literal asterisks or tags.

const TAG_WRAP = /^<(em|i)>([\s\S]*)<\/\1>$/i;

/**
 * Remove one emphasis marker that wraps the whole string. Emphasis inside the
 * summary is left alone, and so is `**bold**`.
 */
export function unwrapEmphasis(value: string | null | undefined): string {
  let text = (value ?? '').trim();
  // A summary can be double-wrapped (`<em>*…*</em>`); two passes is plenty.
  for (let pass = 0; pass < 2; pass += 1) {
    const tag = TAG_WRAP.exec(text);
    if (tag && !/<\/?(em|i)>/i.test(tag[2])) {
      text = tag[2].trim();
      continue;
    }
    const mark = text[0];
    if (
      text.length > 2
      && (mark === '*' || mark === '_')
      && text.endsWith(mark)
      && text[1] !== mark
      && text[text.length - 2] !== mark
      // `*a* and *b*` is two emphasised phrases, not one wrapper.
      && !(mark === '*' && text.slice(1, -1).replace(/\*\*/g, '').includes('*'))
    ) {
      text = text.slice(1, -1).trim();
      continue;
    }
    break;
  }
  return text;
}

/** A summary as plain text: tags dropped, wrapping emphasis marker removed. */
export function summaryText(value: string | null | undefined): string {
  if (!value) return '';
  return unwrapEmphasis(htmlToText(unwrapEmphasis(value)));
}
