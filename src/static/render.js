/* One markdown renderer for Ask and Library.
   The excerpt string stays plain text. Images, PDFs, and mermaid diagrams
   are siblings, so citation highlights can keep using the raw text offsets.
   Mermaid runs only when the caller asks. An open fence stays a code block
   until that caller asks for a drawing. */
(function () {
  const MEDIA_EXT = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".pdf": "pdf",
  };
  let diagramSeq = 0;
  let mermaidReady = false;

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
    );
  }

  function unescapeHtml(text) {
    return String(text)
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&");
  }

  function encPath(name) {
    return String(name).split("/").map(encodeURIComponent).join("/");
  }

  function posixStem(path) {
    const parts = String(path).split("/").filter((part) => part.length > 0);
    const base = parts.length ? parts[parts.length - 1] : "";
    const dot = base.lastIndexOf(".");
    if (dot <= 0) return base;
    return base.slice(0, dot);
  }

  function suffixKind(name) {
    const base = String(name).split("/").pop() || "";
    const dot = base.lastIndexOf(".");
    if (dot <= 0) return "";
    return MEDIA_EXT[base.slice(dot).toLowerCase()] || "";
  }

  function safeParts(raw) {
    if (typeof raw !== "string" || !raw || raw.includes("\0") || raw.includes("\\")) return null;
    if (raw.startsWith("/")) return null;
    const parts = raw.split("/");
    if (!parts.length || parts.some((part) => !part || part === "." || part === ".." || part.startsWith("."))) {
      return null;
    }
    return parts;
  }

  function optionsOf(options) {
    const opts = options || {};
    return {
      sourceFile: typeof opts.sourceFile === "string" ? opts.sourceFile : "",
      names: Array.isArray(opts.names) ? opts.names : [],
      noteNames: Array.isArray(opts.noteNames) ? opts.noteNames : [],
      citations: opts.citations !== false,
      mermaid: opts.mermaid === "draw" ? "draw" : "source",
    };
  }

  function resolveWikilink(target, names) {
    if (typeof target !== "string" || !Array.isArray(names)) return null;
    let raw = target.split("|")[0];
    raw = raw.split("#")[0].trim();
    const parts = safeParts(raw);
    if (!parts) return null;
    const want = parts.join("/");
    const lowered = new Map();
    for (const name of names) {
      if (typeof name !== "string") continue;
      lowered.set(name.toLowerCase(), name);
    }
    const withMd = want.toLowerCase().endsWith(".md") ? want : want + ".md";
    const exact = lowered.get(want.toLowerCase()) || lowered.get(withMd.toLowerCase());
    if (exact) return exact;
    const stem = posixStem(want).toLowerCase();
    const stems = names.filter((name) => typeof name === "string" && posixStem(name).toLowerCase() === stem);
    if (stems.length === 1) return stems[0];
    return null;
  }

  function linkifyWikilinks(escaped, noteNames) {
    const raw = unescapeHtml(escaped);
    const re = /\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]/g;
    let out = "";
    let last = 0;
    let match;
    while ((match = re.exec(raw))) {
      const offset = match.index;
      const embed = offset > 0 && raw[offset - 1] === "!";
      const path = embed ? null : resolveWikilink(match[1], noteNames);
      if (!path) continue;
      out += escapeHtml(raw.slice(last, offset));
      const label = match[2] != null ? match[2] : match[1];
      out += `<button type="button" class="wikilink" data-path="${escapeHtml(path)}">${escapeHtml(label)}</button>`;
      last = offset + match[0].length;
    }
    out += escapeHtml(raw.slice(last));
    return out;
  }

  function noteDir(sourceFile) {
    const parts = safeParts(sourceFile);
    if (!parts || parts.length < 2) return [];
    return parts.slice(0, -1);
  }

  function uniqueBasename(fileName, names) {
    const want = fileName.toLowerCase();
    const hits = names.filter((name) => {
      if (typeof name !== "string") return false;
      return (name.split("/").pop() || "").toLowerCase() === want;
    });
    return hits.length === 1 ? hits[0] : "";
  }

  function resolveImagePath(ref, sourceFile) {
    const kind = suffixKind(ref);
    if (!kind) return "";
    const parts = safeParts(ref);
    if (!parts) return "";
    return noteDir(sourceFile).concat(parts).join("/");
  }

  function resolveWikiMedia(target, names) {
    const kind = suffixKind(target);
    if (!kind) return "";
    const parts = safeParts(target);
    if (!parts) return "";
    const want = parts.join("/");
    if (!names.length) return want.includes("/") ? want : "";
    const exact = names.find((name) => typeof name === "string" && name.toLowerCase() === want.toLowerCase());
    if (exact) return exact;
    if (!want.includes("/")) return uniqueBasename(want, names);
    return "";
  }

  function mediaTag(rel, alt) {
    const kind = suffixKind(rel);
    const src = "/api/media/" + encPath(rel);
    if (kind === "image") {
      return `<img src="${escapeHtml(src)}" alt="${escapeHtml(alt || "")}">`;
    }
    if (kind === "pdf") {
      return `<iframe class="media-frame" src="${escapeHtml(src)}" title="${escapeHtml(alt || rel)}"></iframe>`;
    }
    return "";
  }

  function renderText(text, opts) {
    const bits = [];
    const re = /`([^`]+)`/g;
    let last = 0;
    let match;
    while ((match = re.exec(text))) {
      bits.push({ t: "text", v: text.slice(last, match.index) });
      bits.push({ t: "code", v: match[1] });
      last = match.index + match[0].length;
    }
    bits.push({ t: "text", v: text.slice(last) });
    return bits.map((bit) => {
      if (bit.t === "code") return `<code>${escapeHtml(bit.v)}</code>`;
      return renderPlain(bit.v, opts);
    }).join("");
  }

  function renderPlain(text, opts) {
    const re = /!\[([^\]]*)\]\(([^)\s]+)\)|!\[\[([^\]]+)\]\]/g;
    let out = "";
    let last = 0;
    let match;
    while ((match = re.exec(text))) {
      out += inlineHtml(text.slice(last, match.index), opts);
      if (match[3] != null) {
        const rel = resolveWikiMedia(match[3], opts.names);
        out += rel ? mediaTag(rel, match[3]) : inlineHtml(match[0], opts);
      } else if (/^https?:\/\//i.test(match[2])) {
        const label = match[1] || match[2];
        out += `<a href="${escapeHtml(match[2])}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
      } else {
        const rel = resolveImagePath(match[2], opts.sourceFile);
        out += rel ? mediaTag(rel, match[1]) : inlineHtml(match[0], opts);
      }
      last = match.index + match[0].length;
    }
    out += inlineHtml(text.slice(last), opts);
    return out;
  }

  function inlineHtml(text, opts) {
    let html = escapeHtml(text);
    html = linkifyWikilinks(html, opts.noteNames);
    html = html.replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      (_, label, href) => `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
    );
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
    if (opts.citations) {
      html = html.replace(/\[(\d+)\]/g, '<button type="button" class="cite" data-cite="$1">[$1]</button>');
    }
    return html;
  }

  function splitTableRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((cell) => cell.trim());
  }

  function isTableSep(line) {
    if (!line || !line.includes("-")) return false;
    const cells = splitTableRow(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  }

  function fenceLang(info) {
    return String(info || "").replace(/\r/g, "").trim().split(/\s+/)[0].toLowerCase();
  }

  function fenceHtml(info, code, opts) {
    const lang = fenceLang(info);
    const body = String(code || "").replace(/\r\n/g, "\n").replace(/\n$/, "");
    if (lang === "mermaid" && opts.mermaid === "draw" && body.trim()) {
      return `<div class="diagram" data-diagram="${escapeHtml(body)}"></div>`;
    }
    return `<pre tabindex="0"><code>${escapeHtml(body)}</code></pre>`;
  }

  function mermaidBodies(text) {
    const src = String(text || "").replace(/\r\n/g, "\n");
    const bodies = [];
    const re = /```([^\n`]*)\n?([\s\S]*?)```/g;
    let match;
    while ((match = re.exec(src))) {
      if (fenceLang(match[1]) !== "mermaid") continue;
      const body = String(match[2] || "").replace(/\n$/, "").trim();
      if (body) bodies.push(body);
    }
    return bodies;
  }

  function excerptText(text) {
    const raw = String(text || "");
    const src = raw.replace(/\r\n/g, "\n");
    let removed = false;
    const next = src.replace(/```([^\n`]*)\n?([\s\S]*?)```/g, (full, info, code) => {
      if (fenceLang(info) === "mermaid" && String(code || "").trim()) {
        removed = true;
        return "";
      }
      return full;
    });
    if (!removed) return raw;
    return next.replace(/\n{3,}/g, "\n\n").trim();
  }

  function appendDiagrams(card, text) {
    if (!card || card.querySelector(".excerpt-diagrams")) return false;
    const bodies = mermaidBodies(text);
    if (!bodies.length) return false;
    const box = document.createElement("div");
    box.className = "excerpt-diagrams";
    for (const source of bodies) {
      const node = document.createElement("div");
      node.className = "diagram";
      node.setAttribute("data-diagram", source);
      box.append(node);
    }
    const preview = card.querySelector(".preview");
    if (preview) card.insertBefore(box, preview);
    else card.append(box);
    drawMermaid(box);
    return true;
  }

  function blockHtml(html) {
    if (html.includes("<iframe") || html.includes("<img")) {
      return `<div class="md-media">${html}</div>`;
    }
    return `<p>${html}</p>`;
  }

  function renderMarkdown(src, options) {
    const opts = optionsOf(options);
    const fences = [];
    let text = String(src || "").replace(/\r\n/g, "\n");
    text = text.replace(/```([^\n`]*)\n?([\s\S]*?)```/g, (_, info, code) => {
      const index = fences.length;
      fences.push(fenceHtml(info, code, opts));
      return `\n\n%%FENCE${index}%%\n\n`;
    });
    const open = text.match(/```([^\n`]*)\n?([\s\S]*)$/);
    if (open) {
      const index = fences.length;
      fences.push(fenceHtml(open[1], open[2], opts));
      text = text.slice(0, open.index) + `\n\n%%FENCE${index}%%\n\n`;
    }

    const lines = text.split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*$/.test(line)) { i += 1; continue; }
      const fence = line.trim().match(/^%%FENCE(\d+)%%$/);
      if (fence) { out.push(fences[Number(fence[1])]); i += 1; continue; }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${renderText(heading[2].trim(), opts)}</h${level}>`);
        i += 1;
        continue;
      }
      if (/^ {0,3}([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr>"); i += 1; continue; }
      if (/^>\s?/.test(line)) {
        const quote = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^>\s?/, ""));
          i += 1;
        }
        out.push(`<blockquote>${renderMarkdown(quote.join("\n"), opts)}</blockquote>`);
        continue;
      }
      if (line.includes("|") && isTableSep(lines[i + 1] || "")) {
        const header = splitTableRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
          rows.push(splitTableRow(lines[i]));
          i += 1;
        }
        let html = "<table><thead><tr>" +
          header.map((cell) => `<th>${renderText(cell, opts)}</th>`).join("") +
          "</tr></thead><tbody>";
        for (const row of rows) {
          html += "<tr>" + row.map((cell) => `<td>${renderText(cell, opts)}</td>`).join("") + "</tr>";
        }
        html += "</tbody></table>";
        out.push(html);
        continue;
      }
      if (/^\s*[-*+]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          items.push(`<li>${renderText(lines[i].replace(/^\s*[-*+]\s+/, ""), opts)}</li>`);
          i += 1;
        }
        out.push(`<ul>${items.join("")}</ul>`);
        continue;
      }
      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(`<li>${renderText(lines[i].replace(/^\s*\d+\.\s+/, ""), opts)}</li>`);
          i += 1;
        }
        out.push(`<ol>${items.join("")}</ol>`);
        continue;
      }
      const para = [];
      while (i < lines.length) {
        const cur = lines[i];
        if (/^\s*$/.test(cur)) break;
        if (/^%%FENCE\d+%%$/.test(cur.trim())) break;
        if (/^(#{1,3})\s+/.test(cur)) break;
        if (/^ {0,3}([-*_])\1{2,}\s*$/.test(cur)) break;
        if (/^>\s?/.test(cur)) break;
        if (/^\s*[-*+]\s+/.test(cur)) break;
        if (/^\s*\d+\.\s+/.test(cur)) break;
        if (cur.includes("|") && isTableSep(lines[i + 1] || "")) break;
        para.push(cur);
        i += 1;
      }
      const html = renderText(para.join("\n"), opts).replace(/\n/g, "<br>");
      out.push(blockHtml(html));
    }
    return out.join("");
  }

  function mediaRefs(text, options) {
    const opts = optionsOf(options);
    const refs = [];
    const seen = new Set();
    const re = /!\[([^\]]*)\]\(([^)\s]+)\)|!\[\[([^\]]+)\]\]/g;
    let match;
    const source = String(text || "");
    while ((match = re.exec(source))) {
      let rel = "";
      let alt = "";
      if (match[3] != null) {
        rel = resolveWikiMedia(match[3], opts.names);
        alt = match[3];
      } else if (!/^https?:\/\//i.test(match[2])) {
        rel = resolveImagePath(match[2], opts.sourceFile);
        alt = match[1] || match[2];
      }
      const kind = suffixKind(rel);
      if (!rel || !kind || seen.has(rel)) continue;
      seen.add(rel);
      refs.push({ rel, alt, kind });
    }
    return refs;
  }

  function appendMedia(card, text, options) {
    const refs = mediaRefs(text, options);
    if (!refs.length || !card) return;
    const box = document.createElement("div");
    box.className = "excerpt-media";
    for (const ref of refs) {
      if (ref.kind === "image") {
        const img = document.createElement("img");
        img.src = "/api/media/" + encPath(ref.rel);
        img.alt = ref.alt || "";
        box.append(img);
      } else if (ref.kind === "pdf") {
        const frame = document.createElement("iframe");
        frame.className = "media-frame";
        frame.src = "/api/media/" + encPath(ref.rel);
        frame.title = ref.alt || ref.rel;
        box.append(frame);
      }
    }
    card.append(box);
  }

  function drawMermaid(root) {
    if (!root) return Promise.resolve();
    const nodes = root.querySelectorAll(".diagram[data-diagram]");
    if (!nodes.length) return Promise.resolve();
    const mermaid = window.mermaid;
    if (!mermaid || typeof mermaid.render !== "function") {
      nodes.forEach((node) => {
        node.textContent = node.getAttribute("data-diagram") || "";
      });
      return Promise.resolve();
    }
    if (!mermaidReady) {
      const theme = document.documentElement.getAttribute("data-theme") === "light" ? "default" : "dark";
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        htmlLabels: false,
        suppressErrorRendering: true,
        theme,
        flowchart: { htmlLabels: false },
      });
      mermaidReady = true;
    }
    const jobs = [];
    nodes.forEach((node) => {
      const source = node.getAttribute("data-diagram") || "";
      const id = "nw-diagram-" + (diagramSeq += 1);
      jobs.push((async () => {
        try {
          if (typeof mermaid.parse === "function") await mermaid.parse(source);
          const drawn = await mermaid.render(id, source);
          node.innerHTML = drawn && drawn.svg ? drawn.svg : "";
        } catch (err) {
          node.textContent = source;
        }
      })());
    });
    return Promise.all(jobs);
  }

  window.Northwind = {
    escapeHtml,
    encPath,
    suffixKind,
    renderMarkdown,
    mediaRefs,
    appendMedia,
    appendDiagrams,
    excerptText,
    drawMermaid,
  };
})();
