/**
 * AMCxBlockEditor — édition d'un bloc .sujet-block (textareas LaTeX,
 * badges bonne/mauvaise, +/- réponse, dirty tracking, sauvegarde).
 *
 * Utilisé par /banque pour offrir EXACTEMENT la même UX que /sujet sur
 * une seule question, et à terme par /sujet lui-même (les fonctions
 * inline de sujet.html sont historiques et seront migrées ici).
 *
 * Dépendances :
 *   - window.renderMd, window.latexToMd (chargés par render.js)
 *
 * API :
 *   AMCxBlockEditor.initBlock(blk, ctx)
 *     blk : <section.sujet-block> dans le DOM
 *     ctx : {
 *       onSave: async (blk, data) => true|false  // POST côté appelant
 *       onDirtyChange: (isDirty, blk) => void    // état save button
 *       onTypeChange: async (blk) => void        // single↔mult → re-rendu
 *     }
 *
 *   AMCxBlockEditor.collectBlockData(blk) → data dict (selon kind)
 *   AMCxBlockEditor.renderPreview(ta)     → re-render md→html d'une textarea
 *   AMCxBlockEditor.refreshScores(blk)    → recalcule .ans-score / q-max-val
 */
(function () {
  'use strict';

  // === Renderer LaTeX → MD → KaTeX (fourni par render.js) ====================
  // Le rendu inline `.md-view` à côté de chaque textarea a été retiré (UX :
  // un seul bloc d'édition). On garde cette fonction pour rétrocompat : si
  // un `.md-view` traîne (ancien template), on le remplit ; sinon no-op.
  function renderPreview(ta) {
    const pair = ta.closest('.md-pair');
    if (!pair) return;
    const view = pair.querySelector('.md-view');
    if (view) view.innerHTML = window.renderMd(window.latexToMd(ta.value));
  }

  // === Barème (QCM uniquement) ===============================================
  function frac(s) {
    s = String(s == null ? '' : s).trim();
    if (!s) return 0;
    if (s.indexOf('/') !== -1) {
      const p = s.split('/');
      return parseFloat(p[0]) / parseFloat(p[1]);
    }
    return parseFloat(s) || 0;
  }
  function fmtScore(x) {
    const r = Math.round(x * 1000) / 1000;
    return (r > 0 ? '+' : '') + r;
  }
  function modeOf(arr) {
    const c = {}; let best = arr[0], bn = -1;
    arr.forEach(v => { c[v] = (c[v] || 0) + 1; if (c[v] > bn) { bn = c[v]; best = v; } });
    return best;
  }
  function qType(qEl) {
    const sel = qEl.querySelector('.q-type-select');
    return sel ? sel.value : qEl.dataset.type;
  }
  function deriveGlobals(qEl) {
    if (qType(qEl) !== 'mult') return;
    const bonnes = [].map.call(qEl.querySelectorAll('.md-answer.is-correct .ans-bareme'), i => i.value);
    const mauv   = [].map.call(qEl.querySelectorAll('.md-answer.is-wrong .ans-bareme'), i => i.value);
    const gb = qEl.querySelector('.bar-global-b');
    const gm = qEl.querySelector('.bar-global-m');
    if (gb && bonnes.length) gb.value = modeOf(bonnes);
    if (gm && mauv.length)   gm.value = modeOf(mauv);
  }
  function applyGlobal(qEl, cls, value) {
    qEl.querySelectorAll('.md-answer.' + cls + ' .ans-bareme')
       .forEach(i => { i.value = value; });
  }
  function refreshOverrides(qEl) {
    const gbEl = qEl.querySelector('.bar-global-b');
    const gmEl = qEl.querySelector('.bar-global-m');
    if (!gbEl || !gmEl) return;
    const gb = (gbEl.value || '').trim();
    const gm = (gmEl.value || '').trim();
    qEl.querySelectorAll('.md-answer').forEach(row => {
      const inp = row.querySelector('.ans-bareme'); if (!inp) return;
      const g = row.dataset.correct === '1' ? gb : gm;
      inp.classList.toggle('is-override', inp.value.trim() !== g);
    });
  }
  function refreshScores(qEl) {
    if (qType(qEl) === 'single') {
      const ve = qEl.querySelector('.bar-value');
      const v = ve ? frac(ve.value) : 1;
      qEl.querySelectorAll('.md-answer').forEach(row => {
        const s = row.dataset.correct === '1' ? v : 0;
        const sp = row.querySelector('.ans-score');
        if (sp) {
          sp.textContent = fmtScore(s);
          sp.className = 'ans-score ' + (s > 0 ? 'sc-pos' : (s < 0 ? 'sc-neg' : 'sc-zero'));
        }
      });
    } else {
      qEl.querySelectorAll('.ans-bareme').forEach(inp => {
        const s = frac(inp.value);
        inp.classList.remove('sc-pos', 'sc-neg', 'sc-zero');
        inp.classList.add(s > 0 ? 'sc-pos' : (s < 0 ? 'sc-neg' : 'sc-zero'));
      });
    }
    let mx = 0;
    if (qType(qEl) === 'single') {
      const ve = qEl.querySelector('.bar-value'); mx = ve ? frac(ve.value) : 1;
    } else {
      qEl.querySelectorAll('.md-answer.is-correct .ans-bareme')
         .forEach(i => { mx += frac(i.value); });
    }
    const el = qEl.querySelector('.q-max-val');
    if (el) el.textContent = (Math.round(mx * 100) / 100).toFixed(2);
  }

  // === Toggle bonne/mauvaise ================================================
  function toggleCorrect(blk, row) {
    const isC = row.dataset.correct === '1';
    if (qType(blk) === 'single' && !isC) {
      // single : une seule bonne — désactiver les autres
      blk.querySelectorAll('.md-answer').forEach(r => {
        r.dataset.correct = '0';
        r.classList.remove('is-correct'); r.classList.add('is-wrong');
        const b = r.querySelector('.ans-badge');
        if (b) b.textContent = '✗ mauvaise';
      });
    }
    row.dataset.correct = isC ? '0' : '1';
    row.classList.toggle('is-correct', !isC);
    row.classList.toggle('is-wrong', isC);
    const badge = row.querySelector('.ans-badge');
    if (badge) badge.textContent = isC ? '✗ mauvaise' : '✔ bonne';
  }

  // === Add / remove answer (mode canonique) =================================
  function addAnswer(blk) {
    const list = blk.querySelector('.sujet-answers');
    if (!list) return;
    const isMult = qType(blk) === 'mult';
    const row = document.createElement('div');
    row.className = 'md-answer is-wrong';
    row.dataset.correct = '0';
    row.innerHTML =
      '<div class="ans-mark">' +
        '<button type="button" class="ans-badge" title="cliquer pour basculer bonne / mauvaise">✗ mauvaise</button>' +
        (isMult
          ? '<input type="text" class="ans-bareme" spellcheck="false" title="barème de cette réponse" value="">'
          : '<span class="ans-score"></span>') +
        '<button type="button" class="btn-mini ans-remove" title="supprimer cette réponse">✕</button>' +
      '</div>' +
      '<textarea class="md-src ans-src" rows="2" spellcheck="false"></textarea>';
    list.appendChild(row);
    // Hooks
    row.querySelector('.ans-badge').addEventListener('click', () => {
      toggleCorrect(blk, row);
      _onDirtyMark(blk);
      refreshOverrides(blk); refreshScores(blk);
    });
    row.querySelector('.ans-remove').addEventListener('click', () => {
      removeAnswer(blk, row);
    });
    const ta = row.querySelector('.md-src');
    renderPreview(ta);
    let t;
    ta.addEventListener('input', () => {
      _onDirtyMark(blk);
      clearTimeout(t); t = setTimeout(() => renderPreview(ta), 200);
    });
    const bar = row.querySelector('.ans-bareme');
    if (bar) bar.addEventListener('input', () => {
      _onDirtyMark(blk); refreshOverrides(blk); refreshScores(blk);
    });
    _onDirtyMark(blk);
    refreshOverrides(blk); refreshScores(blk);
  }
  function removeAnswer(blk, row) {
    if (blk.querySelectorAll('.md-answer').length <= 2) {
      alert('Il faut au moins 2 réponses.');
      return;
    }
    row.remove();
    _onDirtyMark(blk);
    refreshOverrides(blk); refreshScores(blk);
  }

  // === Add / remove grading_case (question_open) ============================
  function addGradingCase(blk) {
    const list = blk.querySelector('.open-grading-cases');
    if (!list) return;
    const div = document.createElement('div');
    div.className = 'grading-case';
    div.innerHTML =
      '<label>label <input type="text" class="grading-label" value="" spellcheck="false"></label>' +
      '<label>valeur <input type="number" class="grading-value" step="0.5" value="0"></label>' +
      '<button type="button" class="btn-mini grading-remove" title="supprimer">✕</button>';
    list.appendChild(div);
    div.querySelectorAll('input').forEach(i => i.addEventListener('input', () => _onDirtyMark(blk)));
    div.querySelector('.grading-remove').addEventListener('click', () => {
      div.remove(); _onDirtyMark(blk);
    });
    _onDirtyMark(blk);
  }

  // === Collecte des données d'un bloc =======================================
  function collectQcmData(blk) {
    const envCb = blk.querySelector('.q-env-horiz');
    const valEl = blk.querySelector('.bar-value');
    const answers = [].map.call(blk.querySelectorAll('.md-answer'), row => {
      const bar = row.querySelector('.ans-bareme');
      return {text: row.querySelector('.ans-src').value,
              correct: row.dataset.correct === '1',
              bareme: bar ? bar.value : ''};
    });
    const fEl = blk.querySelector('.q-floor');
    const cEl = blk.querySelector('.q-ceiling');
    const fv = fEl && fEl.value.trim() !== '' ? parseFloat(fEl.value) : null;
    const cv = cEl && cEl.value.trim() !== '' ? parseFloat(cEl.value) : null;
    return {tag: (blk.querySelector('.q-tag-input')?.value || '').trim(),
            qtype: qType(blk),
            env: (envCb && envCb.checked) ? 'reponseshoriz' : 'reponses',
            statement: blk.querySelector('.stmt-src')?.value || '',
            answers: answers,
            value: valEl ? valEl.value : '1',
            floor: (fv !== null && !isNaN(fv)) ? fv : null,
            ceiling: (cv !== null && !isNaN(cv)) ? cv : null};
  }
  function collectOpenData(blk) {
    const cases = [].map.call(blk.querySelectorAll('.grading-case'), gc => ({
      label: gc.querySelector('.grading-label').value,
      value: parseFloat(gc.querySelector('.grading-value').value) || 0,
    }));
    return {tag: (blk.querySelector('.q-tag-input')?.value || '').trim(),
            statement: blk.querySelector('.stmt-src')?.value || '',
            lines: parseInt(blk.querySelector('.open-lines')?.value) || 4,
            points: parseFloat(blk.querySelector('.open-points')?.value) || 0,
            grading_cases: cases};
  }
  function collectTextData(blk) {
    return {tex: blk.querySelector('.text-src')?.value || ''};
  }
  function collectAnswerboxData(blk) {
    return {
      height: (blk.querySelector('.ab-height')?.value || '5cm').trim() || '5cm',
      placement: blk.querySelector('.ab-placement')?.value || 'inline',
      title: (blk.querySelector('.ab-title')?.value || '').trim(),
      instructions: blk.querySelector('.ab-instructions')?.value || '',
      bareme_max: parseFloat(blk.querySelector('.ab-bareme-max')?.value) || 0,
      bareme_step: parseFloat(blk.querySelector('.ab-bareme-step')?.value) || 1,
    };
  }
  function collectFreeformData(blk) {
    let exp = (blk.querySelector('.ff-expected')?.value || '').trim();
    if (exp.includes('|')) {
      exp = exp.split('|').map(s => s.trim()).filter(Boolean);
    }
    return {
      tag: (blk.querySelector('.q-tag-input')?.value || '').trim(),
      statement: blk.querySelector('.stmt-src')?.value || '',
      expected_answer: exp,
      match_mode: blk.querySelector('.ff-match-mode')?.value || 'exact',
      numeric_tol: parseFloat(blk.querySelector('.ff-numeric-tol')?.value) || 0.01,
      lines: parseInt(blk.querySelector('.ff-lines')?.value) || 2,
      points: parseFloat(blk.querySelector('.ff-points')?.value) || 1.0,
    };
  }
  function collectBlockData(blk) {
    const k = blk.dataset.kind;
    if (k === 'text') return collectTextData(blk);
    if (k === 'question_qcm') return collectQcmData(blk);
    if (k === 'question_open') return collectOpenData(blk);
    if (k === 'question_freeform') return collectFreeformData(blk);
    if (k === 'answerbox') return collectAnswerboxData(blk);
    return {};
  }

  // === Dirty tracking (per-block, fires ctx.onDirtyChange) ==================
  //
  // Le contexte est stocké PAR BLOC : un simple `let _onDirty` partagé faisait
  // que le dernier `initBlock` écrasait le callback de tous les précédents —
  // invisible tant qu'une page n'édite qu'un bloc à la fois (banque), fatal dès
  // que /sujet passera à cet éditeur (des dizaines de blocs).
  const _ctxOf = new WeakMap();

  function _ctxCb(blk, name) {
    const ctx = _ctxOf.get(blk);
    return ctx && ctx[name] ? ctx[name] : null;
  }
  function _onDirtyMark(blk) {
    blk.classList.add('dirty');
    const cb = _ctxCb(blk, 'onDirtyChange');
    if (cb) cb(true, blk);
  }
  function clearDirty(blk) {
    blk.classList.remove('dirty');
    const cb = _ctxCb(blk, 'onDirtyChange');
    if (cb) cb(false, blk);
  }

  // === init principal =======================================================
  function initBlock(blk, ctx) {
    ctx = ctx || {};
    _ctxOf.set(blk, ctx);

    // 1. Textareas avec preview
    blk.querySelectorAll('.md-src').forEach(ta => {
      renderPreview(ta);
      let t;
      ta.addEventListener('input', () => {
        _onDirtyMark(blk);
        clearTimeout(t); t = setTimeout(() => renderPreview(ta), 200);
      });
    });

    // 2. Dirty sur inputs/select
    blk.querySelectorAll('input, select').forEach(el => {
      el.addEventListener('input',  () => _onDirtyMark(blk));
      el.addEventListener('change', () => _onDirtyMark(blk));
    });

    // 3. QCM-specific
    if (blk.dataset.kind === 'question_qcm') {
      deriveGlobals(blk); refreshOverrides(blk); refreshScores(blk);
      blk.querySelectorAll('.bar-global-b').forEach(g => {
        g.addEventListener('input', () => {
          applyGlobal(blk, 'is-correct', g.value);
          _onDirtyMark(blk); refreshOverrides(blk); refreshScores(blk);
        });
      });
      blk.querySelectorAll('.bar-global-m').forEach(g => {
        g.addEventListener('input', () => {
          applyGlobal(blk, 'is-wrong', g.value);
          _onDirtyMark(blk); refreshOverrides(blk); refreshScores(blk);
        });
      });
      blk.querySelectorAll('.ans-bareme').forEach(inp => {
        inp.addEventListener('input', () => {
          _onDirtyMark(blk); refreshOverrides(blk); refreshScores(blk);
        });
      });
      blk.querySelectorAll('.bar-value').forEach(v => {
        v.addEventListener('input', () => { _onDirtyMark(blk); refreshScores(blk); });
      });
      blk.querySelectorAll('.ans-badge').forEach(b => {
        b.addEventListener('click', () => {
          toggleCorrect(blk, b.closest('.md-answer'));
          _onDirtyMark(blk); refreshOverrides(blk); refreshScores(blk);
        });
      });
      blk.querySelectorAll('.ans-add').forEach(b => {
        b.addEventListener('click', () => addAnswer(blk));
      });
      blk.querySelectorAll('.ans-remove').forEach(b => {
        b.addEventListener('click', () => removeAnswer(blk, b.closest('.md-answer')));
      });
    }

    // 4. Open-specific (grading cases)
    if (blk.dataset.kind === 'question_open') {
      blk.querySelectorAll('.grading-add').forEach(b => {
        b.addEventListener('click', () => addGradingCase(blk));
      });
      blk.querySelectorAll('.grading-remove').forEach(b => {
        b.addEventListener('click', () => {
          b.closest('.grading-case').remove(); _onDirtyMark(blk);
        });
      });
    }

    // 5. Raccourcis Enter/Ctrl+Enter → ctx.onSave
    if (ctx.onSave) {
      blk.querySelectorAll('input, textarea, select').forEach(field => {
        field.addEventListener('keydown', async (e) => {
          if (e.key !== 'Enter') return;
          if (e.ctrlKey || e.metaKey || field.tagName === 'INPUT') {
            e.preventDefault();
            const ok = await ctx.onSave(blk, collectBlockData(blk));
            if (ok) clearDirty(blk);
          }
        });
      });
    }

    // 6. Type change (single ↔ mult) — l'appelant veut souvent recharger le
    //    bloc. On lui passe le bloc et on attend sa promesse : il peut ainsi
    //    enregistrer l'édition en cours avant de tout re-rendre.
    const sel = blk.querySelector('.q-type-select');
    if (sel && ctx.onTypeChange) {
      sel.addEventListener('change', async () => {
        if (sel.value === blk.dataset.type) return;
        await ctx.onTypeChange(blk);
      });
    }
  }

  // === Export ===============================================================
  window.AMCxBlockEditor = {
    initBlock:        initBlock,
    collectBlockData: collectBlockData,
    renderPreview:    renderPreview,
    refreshScores:    refreshScores,
    refreshOverrides: refreshOverrides,
    clearDirty:       clearDirty,
  };
})();
