/**
 * AMCxRender — shared question-rendering module.
 *
 * Used by:
 * - /sujet (édition de blocs) via .md-pair / .md-view (mode 'edit')
 * - /banque (visualisation de questions) en mode 'view'
 * - Bank modal preview dans /sujet (rendu d'une question depuis la banque)
 *
 * Exporte un objet global `window.AMCxRender` :
 *   - latexToMd(s)         : conversion LaTeX → Markdown best-effort
 *   - renderMd(src)        : Markdown + maths → HTML (marked.js + KaTeX)
 *   - escapeHtml(s)
 *   - mdToHtmlSafe(md)     : fallback si marked.js indispo
 *   - renderQuestion(host, data, opts)  : rend une question complète
 *
 * Dépendances : window.marked (vendor/marked.min.js) + window.katex
 * (vendor/katex/katex.min.js). Doivent être chargés avant render.js.
 *
 * Pour ne pas casser les pages existantes qui appellent latexToMd/renderMd/etc.
 * en tant que globales, le module les expose aussi en `window.<name>` (compat).
 */
(function () {
  'use strict';

  // === LaTeX → markdown (best-effort) =======================================

  function balancedJS(s, i) {
    let depth = 0;
    for (let j = i; j < s.length; j++) {
      if (s[j] === '{') depth++;
      else if (s[j] === '}') { depth--; if (depth === 0) return [s.slice(i + 1, j), j + 1]; }
    }
    return [s.slice(i + 1), s.length];
  }

  function replaceCmd(t, name, fn) {
    const needle = '\\' + name + '{';
    let k;
    while ((k = t.indexOf(needle)) !== -1) {
      const [inner, after] = balancedJS(t, k + needle.length - 1);
      t = t.slice(0, k) + fn(inner) + t.slice(after);
    }
    return t;
  }

  function textToMd(t) {
    t = t.replace(/(^|[^\\])%.*/g, '$1');
    t = replaceCmd(t, 'textbf', x => '**' + x + '**');
    t = replaceCmd(t, 'textsc', x => x);
    t = replaceCmd(t, 'emph', x => '*' + x + '*');
    t = replaceCmd(t, 'textit', x => '*' + x + '*');
    t = replaceCmd(t, 'text', x => x);
    t = t.replace(/\\\\/g, '\n');
    ['\\,', '\\;', '\\!', '\\ ', '\\quad', '\\qquad', '~'].forEach(sp => { t = t.split(sp).join(' '); });
    t = t.split('\\dots').join('…').split('\\ldots').join('…');
    t = t.split('\\%').join('%').split('\\&').join('&').split('\\_').join('_');
    ['\\noindent', '\\smallskip', '\\medskip', '\\bigskip'].forEach(c => { t = t.split(c).join(''); });
    return t;
  }

  function latexToMd(s) {
    let out = '', i = 0;
    while (i < s.length) {
      if (s[i] === '$') {
        const delim = s.substr(i, 2) === '$$' ? '$$' : '$';
        let j = s.indexOf(delim, i + delim.length);
        if (j === -1) { out += s.slice(i); break; }
        out += s.slice(i, j + delim.length);
        i = j + delim.length;
      } else {
        let j = s.indexOf('$', i);
        if (j === -1) j = s.length;
        out += textToMd(s.slice(i, j));
        i = j;
      }
    }
    return out.replace(/[ \t]+/g, ' ').replace(/ *\n */g, '\n')
              .replace(/\n{3,}/g, '\n\n').trim();
  }

  // === markdown + maths → HTML ===============================================

  function renderMd(src) {
    const math = [];
    const tok = i => '@@KX' + i + 'XK@@';
    let s = (src || '')
      .replace(/\$\$([\s\S]+?)\$\$/g, function (_, m) {
        math.push({tex: m, display: true});  return tok(math.length - 1); })
      .replace(/\$([^\$\n]+?)\$/g, function (_, m) {
        math.push({tex: m, display: false}); return tok(math.length - 1); });
    let html;
    try { html = window.marked ? window.marked.parse(s) : s; } catch (e) { html = s; }
    return html.replace(/@@KX(\d+)XK@@/g, function (_, i) {
      const it = math[+i];
      try { return window.katex.renderToString(it.tex, {displayMode: it.display, throwOnError: false}); }
      catch (e) { return '<code>$' + it.tex + '$</code>'; }
    });
  }

  // === Helpers ==============================================================

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function mdToHtmlSafe(md) {
    try { return window.marked ? window.marked.parse(md) : escapeHtml(md); }
    catch (e) { return escapeHtml(md); }
  }

  // === renderQuestion : rend une question dans un container =================
  //
  // Couvre les 4 kinds (question_qcm, question_open, answerbox, text).
  // Mode 'view' = read-only, visuellement proche du mode édition de Sujet.
  // Mode 'edit' n'est pas encore implémenté ici (Sujet a son propre rendu inline).
  //
  // Options :
  //   - kind : 'question_qcm' | 'question_open' | 'answerbox' | 'text'
  //   - mode : 'view' (par défaut) — readonly
  //   - size : 'normal' (défaut) | 'large' — pour panel preview agrandi
  //
  function renderQuestion(host, data, opts) {
    opts = opts || {};
    const kind = opts.kind || data._kind || 'text';
    const size = opts.size || 'normal';
    const d = data || {};

    host.innerHTML = '';
    host.classList.add('amcx-rendered');
    host.classList.toggle('amcx-rendered-large', size === 'large');
    host.classList.add('amcx-rendered-' + kind);

    if (kind === 'question_qcm') {
      _renderQcm(host, d);
    } else if (kind === 'question_open') {
      _renderOpen(host, d);
    } else if (kind === 'answerbox') {
      _renderAnswerbox(host, d);
    } else {
      _renderText(host, d);
    }
  }

  function _renderQcm(host, d) {
    // Énoncé
    if (d.statement) {
      const stmt = document.createElement('div');
      stmt.className = 'amcx-stmt';
      stmt.innerHTML = renderMd(latexToMd(d.statement));
      host.appendChild(stmt);
    }
    // Réponses
    if (Array.isArray(d.answers) && d.answers.length) {
      const ol = document.createElement('ol');
      ol.className = 'amcx-answers';
      d.answers.forEach((a, i) => {
        const li = document.createElement('li');
        li.className = 'amcx-answer ' + (a.correct ? 'is-correct' : 'is-wrong');
        const marker = document.createElement('span');
        marker.className = 'amcx-answer-marker';
        marker.textContent = a.correct ? '✓' : '·';
        const txt = document.createElement('span');
        txt.className = 'amcx-answer-text';
        txt.innerHTML = renderMd(latexToMd(a.text || ''));
        li.appendChild(marker);
        li.appendChild(txt);
        if (a.bareme) {
          const b = document.createElement('span');
          b.className = 'amcx-answer-bareme';
          b.textContent = a.bareme;
          li.appendChild(b);
        }
        ol.appendChild(li);
      });
      host.appendChild(ol);
    }
    // Méta-infos (qtype, env, value)
    const meta = [];
    if (d.qtype) meta.push(d.qtype === 'mult' ? 'choix multiple' : 'choix unique');
    if (d.env) meta.push('env: ' + d.env);
    if (d.value != null) meta.push('valeur: ' + d.value);
    if (meta.length) {
      const m = document.createElement('p');
      m.className = 'amcx-meta';
      m.textContent = meta.join(' · ');
      host.appendChild(m);
    }
  }

  function _renderOpen(host, d) {
    if (d.statement) {
      const stmt = document.createElement('div');
      stmt.className = 'amcx-stmt';
      stmt.innerHTML = renderMd(latexToMd(d.statement));
      host.appendChild(stmt);
    }
    const meta = document.createElement('p');
    meta.className = 'amcx-meta';
    meta.innerHTML = '<em>📝 Question ouverte · ' + (d.lines || 4) + ' lignes · ' +
                     (d.points != null ? d.points : '?') + ' pts</em>';
    host.appendChild(meta);
    if (Array.isArray(d.grading_cases) && d.grading_cases.length) {
      const ul = document.createElement('ul');
      ul.className = 'amcx-grading-cases';
      d.grading_cases.forEach(g => {
        const li = document.createElement('li');
        li.textContent = (g.label || '?') + ' : ' + (g.value || 0) + ' pts';
        ul.appendChild(li);
      });
      host.appendChild(ul);
    }
  }

  function _renderAnswerbox(host, d) {
    const wrap = document.createElement('div');
    wrap.className = 'amcx-answerbox';
    wrap.innerHTML =
      '<strong>' + escapeHtml(d.title || '') + '</strong>' +
      (d.instructions ? '<br><em>' + escapeHtml(d.instructions) + '</em>' : '');
    host.appendChild(wrap);
    const meta = document.createElement('p');
    meta.className = 'amcx-meta';
    meta.innerHTML = 'cadre <code>' + escapeHtml(d.height || '5cm') + '</code> · ' +
                     'placement <code>' + escapeHtml(d.placement || 'inline') + '</code>';
    host.appendChild(meta);
  }

  function _renderText(host, d) {
    const div = document.createElement('div');
    div.className = 'amcx-text';
    div.innerHTML = renderMd(latexToMd(d.tex || d.text || ''));
    host.appendChild(div);
  }

  // === Export global ========================================================

  window.AMCxRender = {
    latexToMd:      latexToMd,
    renderMd:       renderMd,
    escapeHtml:     escapeHtml,
    mdToHtmlSafe:   mdToHtmlSafe,
    renderQuestion: renderQuestion,
  };

  // Compat : exposer aussi en globales (pour les scripts inline existants)
  window.latexToMd    = latexToMd;
  window.renderMd     = renderMd;
  window.escapeHtml   = window.escapeHtml || escapeHtml;
  window.mdToHtmlSafe = mdToHtmlSafe;
})();
