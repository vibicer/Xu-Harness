/** Minimal safe markdown → HTML for chat bubbles. Escapes HTML first, then
 * applies a small CommonMark subset: headings, bold/italic/strike, inline code,
 * fenced code blocks, links, lists, quotes, hr, paragraphs, and GFM tables. */

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inline(s: string): string {
  let out = escapeHtml(s);
  out = out.replace(/`([^`\n]+)`/g, '<code class="ic">$1</code>');
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(?<!\w)\*([^*\n]+)\*(?!\w)/g, "<em>$1</em>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
  );
  // Autolink bare URLs (written without markdown brackets). Skipped when the
  // URL already sits inside a tag attribute or anchor (the []() rule above),
  // and trailing punctuation is kept outside the link.
  out = out.replace(
    /(?<![\w">=])(https?:\/\/[^\s<>]+)/g,
    (_m: string, url: string) => {
      const trimmed = url.replace(/[.,;:!?)\]]+$/, "");
      if (!trimmed) return url;
      return (
        `<a href="${trimmed}" target="_blank" rel="noopener noreferrer">` +
        `${trimmed}</a>` +
        url.slice(trimmed.length)
      );
    },
  );
  return out;
}

/** A GFM table row: leading and trailing pipe, optional surrounding space. */
function isTableRow(s: string): boolean {
  return /^\s*\|.*\|\s*$/.test(s);
}

/** Separator row between header and body: `| --- | :--: | ---: |` etc. */
function isTableSep(s: string): boolean {
  return /^\s*\|[\s:|-]+\|\s*$/.test(s) && /-/.test(s);
}

function splitCells(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

/** Consume a GFM table starting at `i`; returns { html, endIndex } or null. */
function tryTable(
  lines: string[],
  i: number,
  inline: (s: string) => string,
): { html: string; endIndex: number } | null {
  if (i + 1 >= lines.length) return null;
  if (!isTableRow(lines[i]) || !isTableSep(lines[i + 1])) return null;

  const head = splitCells(lines[i]);
  const body: string[][] = [];
  let j = i + 2;
  while (j < lines.length && isTableRow(lines[j])) {
    body.push(splitCells(lines[j]));
    j++;
  }
  const maxCols = Math.max(head.length, ...body.map((r) => r.length));
  const padTo = (cells: string[]): string[] => {
    const out = cells.slice();
    while (out.length < maxCols) out.push("");
    return out;
  };

  const html =
    '<div class="md-table-wrap"><table class="md-table"><thead><tr>' +
    padTo(head).map((c) => `<th>${inline(c)}</th>`).join("") +
    "</tr></thead><tbody>" +
    body
      .map((r) => `<tr>${padTo(r).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`)
      .join("") +
    "</tbody></table></div>";
  return { html, endIndex: j - 1 };
}

export function renderMarkdown(src: string): string {
  const lines = src.split("\n");
  const html: string[] = [];
  let para: string[] = [];
  let list: { ordered: boolean; items: string[]; start: number } | null = null;
  let orderedNext = 1;
  let orderedChain = false;
  let quote: string[] | null = null;
  let code: string[] | null = null;

  const flushPara = () => {
    if (para.length) {
      html.push(`<p>${para.map(inline).join("<br>")}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      const tag = list.ordered ? "ol" : "ul";
      const start = list.ordered && list.start !== 1 ? ` start="${list.start}"` : "";
      html.push(`<${tag}${start}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${tag}>`);
      if (list.ordered) orderedNext = list.start + list.items.length;
      list = null;
    }
  };
  const pushListItem = (ordered: boolean, text: string) => {
    flushPara();
    flushQuote();
    if (list && list.ordered !== ordered) flushList();
    if (!list) list = { ordered, items: [], start: ordered ? (orderedChain ? orderedNext : 1) : 1 };
    if (ordered) orderedChain = true;
    list.items.push(text);
  };
  const flushQuote = () => {
    if (quote) {
      html.push(`<blockquote>${quote.map(inline).join("<br>")}</blockquote>`);
      quote = null;
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      if (code) {
        html.push(
          `<pre class="code"><code>${escapeHtml(code.join("\n"))}</code></pre>`,
        );
        code = null;
      } else {
        flushPara(); flushList(); flushQuote();
        code = [];
      }
      continue;
    }
    if (code) {
      code.push(line);
      continue;
    }

    const table = tryTable(lines, i, inline);
    if (table) {
      flushPara(); flushList(); flushQuote();
      html.push(table.html);
      i = table.endIndex;
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      const level = heading[1].length;
      // An open paragraph/list/quote must flush BEFORE the heading, or the
      // heading is hoisted above content that precedes it in the source.
      flushPara(); flushList(); flushQuote();
      html.push(`<div class="md-h md-h${level}">${inline(heading[2])}</div>`);
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      flushPara(); flushList(); flushQuote();
      html.push("<hr>");
      continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) {
      pushListItem(false, bullet[1]);
      continue;
    }
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (numbered) {
      pushListItem(true, numbered[1]);
      continue;
    }
    const q = line.match(/^>\s?(.*)$/);
    if (q) {
      flushPara(); flushList();
      quote = quote ?? [];
      quote.push(q[1]);
      continue;
    }
    if (line.trim() === "") {
      flushPara(); flushList(); flushQuote();
      continue;
    }
    flushList(); flushQuote();
    para.push(line);
  }
  flushPara(); flushList(); flushQuote();
  if (code) {
    html.push(`<pre class="code"><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  }
  return html.join("");
}
