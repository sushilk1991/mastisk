// ============================================================
// Save Research — Markdown Renderer
// ============================================================
// Converts markdown text to HTML for display in chat bubbles.
// XSS-safe: HTML-escapes input before processing markdown.

/**
 * Render markdown text to HTML.
 * @param {string} text - Markdown text
 * @returns {string} HTML string
 */
function renderMarkdown(text) {
  if (!text) return '';

  // Escape HTML first to prevent XSS
  let escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // Code blocks (must be before other processing)
  escaped = escaped.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre><code${lang ? ` class="language-${lang}"` : ''}>${code}</code></pre>`;
  });

  // Inline code
  escaped = escaped.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Horizontal rules (must be before bold/italic to avoid conflict)
  escaped = escaped.replace(/^[-*_]{3,}$/gm, '<hr>');

  // Bold
  escaped = escaped.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

  // Italic
  escaped = escaped.replace(/\*(.+?)\*/g, '<em>$1</em>');

  // Strikethrough
  escaped = escaped.replace(/~~(.+?)~~/g, '<del>$1</del>');

  // Headers
  escaped = escaped.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  escaped = escaped.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  escaped = escaped.replace(/^# (.+)$/gm, '<h1>$1</h1>');

  // Blockquotes
  escaped = escaped.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');

  // Merge consecutive blockquotes
  escaped = escaped.replace(/<\/blockquote>\n<blockquote>/g, '\n');

  // Tables
  escaped = processMarkdownTables(escaped);

  // Unordered lists
  escaped = escaped.replace(/^[*\-+] (.+)$/gm, '<li>$1</li>');
  // Wrap consecutive <li> in <ul>
  escaped = escaped.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');

  // Ordered lists
  escaped = escaped.replace(/^\d+\.\s+(.+)$/gm, '<oli>$1</oli>');
  // Wrap consecutive <oli> in <ol>
  escaped = escaped.replace(/((?:<oli>.*<\/oli>\n?)+)/g, (match) => {
    return '<ol>' + match.replace(/<\/?oli>/g, (tag) => tag.replace('oli', 'li')) + '</ol>';
  });

  // Paragraphs: split on double newlines
  escaped = escaped.split(/\n{2,}/).map(block => {
    const trimmed = block.trim();
    if (!trimmed) return '';
    // Don't wrap blocks that are already HTML elements
    if (/^<(h[1-6]|pre|ul|ol|blockquote|table|hr|li)/.test(trimmed)) return trimmed;
    return `<p>${trimmed}</p>`;
  }).join('');

  // Convert single newlines within paragraphs to <br> (but not in pre/code blocks)
  escaped = escaped.replace(/<p>([\s\S]*?)<\/p>/g, (match, content) => {
    return '<p>' + content.replace(/\n/g, '<br>') + '</p>';
  });

  return escaped;
}

/**
 * Process markdown tables to HTML.
 * @param {string} text - Partially-processed markdown text
 * @returns {string}
 */
function processMarkdownTables(text) {
  const lines = text.split('\n');
  const result = [];
  let i = 0;

  while (i < lines.length) {
    // Check if this could be a table header
    if (i + 1 < lines.length && lines[i].includes('|') && /^\|?\s*[-:]+[-| :]*$/.test(lines[i + 1])) {
      const headerCells = parseTableRow(lines[i]);
      const alignments = parseTableAlignments(lines[i + 1]);
      i += 2;

      let tableHtml = '<table><thead><tr>';
      for (let c = 0; c < headerCells.length; c++) {
        const align = alignments[c] ? ` style="text-align:${alignments[c]}"` : '';
        tableHtml += `<th${align}>${headerCells[c]}</th>`;
      }
      tableHtml += '</tr></thead><tbody>';

      // Parse body rows
      while (i < lines.length && lines[i].includes('|')) {
        const cells = parseTableRow(lines[i]);
        tableHtml += '<tr>';
        for (let c = 0; c < cells.length; c++) {
          const align = alignments[c] ? ` style="text-align:${alignments[c]}"` : '';
          tableHtml += `<td${align}>${cells[c]}</td>`;
        }
        tableHtml += '</tr>';
        i++;
      }

      tableHtml += '</tbody></table>';
      result.push(tableHtml);
    } else {
      result.push(lines[i]);
      i++;
    }
  }

  return result.join('\n');
}

/**
 * Parse a table row into cells.
 * @param {string} row
 * @returns {string[]}
 */
function parseTableRow(row) {
  return row
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map(cell => cell.trim());
}

/**
 * Parse table alignment row.
 * @param {string} row
 * @returns {Array<string|null>} 'left', 'center', 'right', or null
 */
function parseTableAlignments(row) {
  return row
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map(cell => {
      const trimmed = cell.trim();
      if (trimmed.startsWith(':') && trimmed.endsWith(':')) return 'center';
      if (trimmed.endsWith(':')) return 'right';
      if (trimmed.startsWith(':')) return 'left';
      return null;
    });
}

// Export for both Node.js (testing) and browser (extension)
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { renderMarkdown, processMarkdownTables, parseTableRow, parseTableAlignments };
}
