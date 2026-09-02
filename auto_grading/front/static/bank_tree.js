/* AMCxBankTree — arbre de catégories d'une banque, partagé entre les pages.
 *
 * Deux modes :
 *   'filter'  navigation (une sélection à la fois) + édition de l'arbre
 *   'pick'    sélection multiple par cases à cocher (classer une question)
 *
 * ⚠ Les noms de catégories viennent d'une banque potentiellement partagée :
 * c'est de l'entrée NON FIABLE. Tout le DOM est construit par createElement +
 * textContent — aucun innerHTML, nulle part dans ce fichier.
 *
 * Le serveur renvoie déjà `depth`, `path`, `n_direct` et `n_total` : ce
 * composant ne recalcule aucune structure d'arbre, il ne fait que replier et
 * indenter.
 */
(function () {
  'use strict';

  const MAX_LABEL = 80;

  // Type MIME du glisser-déposer « question → catégorie ». Un type dédié plutôt
  // que text/plain : pendant `dragover`, `getData` est interdit et seuls les
  // TYPES sont lisibles — c'est la seule façon de savoir si le survol nous
  // concerne avant d'accepter le dépôt.
  const DND_TYPE = 'application/x-amcx-bank-id';

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  async function api(url, opts) {
    const r = await fetch(url, opts);
    let j = {};
    try { j = await r.json(); } catch (e) { /* corps vide */ }
    if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }

  function Tree(host, opts) {
    this.host = host;
    this.opts = Object.assign({
      mode: 'filter',
      canEdit: false,
      storageKey: 'amcx-bank-tree',
      onFilterChange: null,   // ({category, descendants, uncategorized})
      onPickChange: null,     // ([ids])
      onDropQuestion: null,   // (bankId, catId) — glisser-déposer d'une question
      onStatus: null,         // (msg, 'ok'|'err')
    }, opts || {});
    this.nodes = [];
    this.byId = {};
    this.collapsed = this._loadCollapsed();
    this.picked = new Set();
    this.selected = null;       // id, '' = toutes, '__none__' = sans catégorie
    this.descendants = true;
    this.editing = null;        // id du nœud en cours de renommage
    this.adding = null;         // parent_id du nœud en cours de création
  }

  // -- persistance du repli (par banque) ------------------------------------
  Tree.prototype._loadCollapsed = function () {
    try {
      const raw = localStorage.getItem(this.opts.storageKey);
      return new Set(raw ? JSON.parse(raw) : []);
    } catch (e) { return new Set(); }
  };
  Tree.prototype._saveCollapsed = function () {
    try {
      localStorage.setItem(this.opts.storageKey, JSON.stringify([...this.collapsed]));
    } catch (e) { /* mode privé : le repli ne survit pas, sans gravité */ }
  };

  Tree.prototype._status = function (msg, cls) {
    if (this.opts.onStatus) this.opts.onStatus(msg, cls || 'ok');
  };

  // -- chargement -----------------------------------------------------------

  // Alimente l'arbre depuis une charge utile déjà récupérée — `/api/bank/facets`
  // renvoie `nodes` ET `all_tags` en une requête, ce qui évite d'aller chercher
  // l'arbre séparément juste après.
  Tree.prototype.load = function (j) {
    this.nodes = (j && j.nodes) || [];
    this.maxDepth = (j && j.max_depth) || 4;
    this.canEdit = this.opts.canEdit && !!(j && j.can_edit);
    this.byId = {};
    this.nodes.forEach(n => { this.byId[n.id] = n; });
    this.render();
  };

  Tree.prototype.refresh = async function () {
    try {
      const j = await api('/api/bank/categories');
      this.nodes = j.nodes || [];
      this.maxDepth = j.max_depth || 4;
      this.canEdit = this.opts.canEdit && !!j.can_edit;
      this.byId = {};
      this.nodes.forEach(n => { this.byId[n.id] = n; });
      this.render();
    } catch (e) {
      this.host.textContent = '';
      // 501 = backend sans catégories : on masque au lieu d'alarmer.
      if (!/501|pas encore/i.test(String(e))) {
        this.host.appendChild(el('p', 'bt-err', '✘ ' + e.message));
      }
      this.nodes = [];
    }
  };

  Tree.prototype._hidden = function (node) {
    let p = node.parent_id;
    while (p) {
      if (this.collapsed.has(p)) return true;
      p = (this.byId[p] || {}).parent_id;
    }
    return false;
  };

  Tree.prototype._hasKids = function (id) {
    return this.nodes.some(n => n.parent_id === id);
  };

  // -- rendu ----------------------------------------------------------------
  Tree.prototype.render = function () {
    const filter = this.opts.mode === 'filter';
    this.host.textContent = '';
    this.host.classList.add('bank-tree');
    this.host.classList.toggle('bank-tree-pick', !filter);

    // Arbre vide : « Toutes », « Sans catégorie » et le bouton de portée ne
    // peuvent rien filtrer. On ne montre que de quoi démarrer.
    if (filter && !this.nodes.length && this.adding === null) {
      this.host.appendChild(el('p', 'bt-empty',
        'Aucune catégorie. Crée un chapitre pour structurer la banque.'));
      if (this.canEdit) this.host.appendChild(this._addRootBtn());
      return;
    }

    if (filter) {
      this.host.appendChild(this._pseudoRow('', 'Toutes les questions', 'bt-all'));
    }

    this.nodes.forEach(n => {
      if (this._hidden(n)) return;
      this.host.appendChild(this._row(n));
      if (this.adding === n.id) this.host.appendChild(this._newRow(n.id, n.depth + 1));
    });

    if (this.adding === '') this.host.appendChild(this._newRow(null, 1));

    if (filter) {
      this.host.appendChild(this._pseudoRow('__none__', 'Sans catégorie', 'bt-none'));
      const opt = el('label', 'bt-desc-toggle');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = this.descendants;
      cb.addEventListener('change', () => {
        this.descendants = cb.checked;
        this._emitFilter();
      });
      opt.appendChild(cb);
      opt.appendChild(el('span', null, ' inclure les sous-catégories'));
      this.host.appendChild(opt);
    }

    if (this.canEdit) this.host.appendChild(this._addRootBtn());
  };

  Tree.prototype._addRootBtn = function () {
    const add = el('button', 'btn btn-tiny bt-add-root', '+ chapitre');
    add.type = 'button';
    add.addEventListener('click', () => { this.adding = ''; this.render(); });
    return add;
  };

  Tree.prototype._pseudoRow = function (id, label, cls) {
    const row = el('div', 'bt-row bt-pseudo ' + cls);
    if (this.selected === id) row.classList.add('bt-selected');
    row.appendChild(el('span', 'bt-caret'));
    row.appendChild(el('span', 'bt-label', label));
    row.addEventListener('click', () => {
      this.selected = id;
      this._emitFilter();
      this.render();
    });
    return row;
  };

  Tree.prototype._row = function (n) {
    const filter = this.opts.mode === 'filter';
    const row = el('div', 'bt-row');
    row.dataset.id = n.id;
    row.style.paddingLeft = (6 + (n.depth - 1) * 14) + 'px';
    if (filter && this.selected === n.id) row.classList.add('bt-selected');

    // caret : plier/déplier, séparé du label (clic distinct)
    const caret = el('span', 'bt-caret');
    if (this._hasKids(n.id)) {
      caret.textContent = this.collapsed.has(n.id) ? '▸' : '▾';
      caret.classList.add('bt-caret-on');
      caret.addEventListener('click', (e) => {
        e.stopPropagation();
        if (this.collapsed.has(n.id)) this.collapsed.delete(n.id);
        else this.collapsed.add(n.id);
        this._saveCollapsed();
        this.render();
      });
    }
    row.appendChild(caret);

    if (!filter) {
      const cb = el('input', 'bt-check');
      cb.type = 'checkbox';
      cb.checked = this.picked.has(n.id);
      cb.addEventListener('change', () => {
        if (cb.checked) this.picked.add(n.id); else this.picked.delete(n.id);
        if (this.opts.onPickChange) this.opts.onPickChange([...this.picked]);
      });
      row.appendChild(cb);
    }

    if (this.editing === n.id) {
      row.appendChild(this._nameInput(n.name, async (v) => {
        await api('/api/bank/categories/' + encodeURIComponent(n.id),
                  {method: 'PATCH', headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify({name: v})});
        this._status('Renommée ✓');
      }));
      return row;
    }

    const label = el('span', 'bt-label', n.name);
    label.title = n.path.join(' › ');
    if (filter) {
      label.addEventListener('click', () => {
        this.selected = n.id;
        this._emitFilter();
        this.render();
      });
    }
    if (this.canEdit) {
      label.addEventListener('dblclick', (e) => {
        e.preventDefault();
        this.editing = n.id;
        this.render();
      });
    }
    row.appendChild(label);

    // n_direct (n_total) — le total n'est affiché que s'il diffère.
    const cnt = el('span', 'bt-count',
                   n.n_total > n.n_direct ? n.n_direct + ' (' + n.n_total + ')'
                                          : String(n.n_direct));
    cnt.title = n.n_direct + ' directement · ' + n.n_total + ' avec les sous-catégories';
    row.appendChild(cnt);

    if (this.canEdit) row.appendChild(this._tools(n));
    if (filter && this.opts.onDropQuestion) this._dropTarget(row, n);
    return row;
  };

  // Déposer une question sur un nœud l'AJOUTE à cette catégorie ; ça ne la
  // retire d'aucune autre. C'est le modèle : une question appartient à
  // plusieurs catégories. Le retrait se fait par le ✕ d'une chip sur la fiche.
  Tree.prototype._dropTarget = function (row, n) {
    const accepts = (e) => e.dataTransfer &&
                           Array.prototype.includes.call(e.dataTransfer.types, DND_TYPE);
    row.addEventListener('dragover', (e) => {
      if (!accepts(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
      row.classList.add('bt-drop');
    });
    row.addEventListener('dragleave', () => row.classList.remove('bt-drop'));
    row.addEventListener('drop', (e) => {
      row.classList.remove('bt-drop');
      if (!accepts(e)) return;
      e.preventDefault();
      const id = e.dataTransfer.getData(DND_TYPE);
      if (id) this.opts.onDropQuestion(id, n.id);
    });
  };

  Tree.prototype._tools = function (n) {
    const box = el('span', 'bt-tools');
    const mk = (label, title, fn, cls) => {
      const b = el('button', 'bt-btn' + (cls ? ' ' + cls : ''), label);
      b.type = 'button';
      b.title = title;
      b.addEventListener('click', async (e) => {
        e.stopPropagation();
        try { await fn(); } catch (err) { this._status('✘ ' + err.message, 'err'); }
      });
      return b;
    };
    if (n.depth < this.maxDepth) {
      box.appendChild(mk('+', 'ajouter une sous-catégorie', () => {
        this.collapsed.delete(n.id);
        this.adding = n.id;
        this.render();
      }));
    }
    box.appendChild(mk('▲', 'monter', () => this._move(n, -1)));
    box.appendChild(mk('▼', 'descendre', () => this._move(n, +1)));
    box.appendChild(mk('✕', 'supprimer', () => this._delete(n), 'bt-btn-del'));
    return box;
  };

  Tree.prototype._nameInput = function (value, commit) {
    const inp = el('input', 'bt-name-input');
    inp.type = 'text';
    inp.value = value || '';
    inp.maxLength = MAX_LABEL;
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const v = inp.value.trim();
      this.editing = null;
      this.adding = null;
      if (save && v && v !== value) {
        try { await commit(v); }
        catch (e) { this._status('✘ ' + e.message, 'err'); }
      }
      await this.refresh();
      this._emitFilter(true);
    };
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    inp.addEventListener('blur', () => finish(true));
    setTimeout(() => { inp.focus(); inp.select(); }, 0);
    return inp;
  };

  Tree.prototype._newRow = function (parentId, depth) {
    const row = el('div', 'bt-row bt-row-new');
    row.style.paddingLeft = (6 + (depth - 1) * 14) + 'px';
    row.appendChild(el('span', 'bt-caret'));
    row.appendChild(this._nameInput('', async (v) => {
      await api('/api/bank/categories',
                {method: 'POST', headers: {'Content-Type': 'application/json'},
                 body: JSON.stringify({name: v, parent_id: parentId})});
      this._status('Catégorie créée ✓');
    }));
    return row;
  };

  Tree.prototype._move = async function (n, dir) {
    const sibs = this.nodes.filter(x => x.parent_id === n.parent_id);
    const i = sibs.findIndex(x => x.id === n.id);
    const j = i + dir;
    if (j < 0 || j >= sibs.length) return;
    const a = sibs[i], b = sibs[j];
    const pa = a.position, pb = b.position;
    // Positions égales (créations concurrentes) : on les réécrit toutes.
    const send = (id, pos) => api('/api/bank/categories/' + encodeURIComponent(id),
      {method: 'PATCH', headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({position: pos})});
    if (pa === pb) {
      await Promise.all(sibs.map((s, k) => send(s.id, k)));
      await send(a.id, j);
      await send(b.id, i);
    } else {
      await send(a.id, pb);
      await send(b.id, pa);
    }
    await this.refresh();
  };

  Tree.prototype._delete = async function (n) {
    const url = '/api/bank/categories/' + encodeURIComponent(n.id);
    try {
      await api(url, {method: 'DELETE'});
    } catch (e) {
      // 409 : le nœud n'est pas vide. On dit CE qu'il contient et ce que
      // « remonter » implique — jamais de suppression de question.
      const ok = window.confirm(
        e.message + '\n\n' +
        'Remonter son contenu vers la catégorie parente ?\n' +
        'Aucune question ne sera supprimée. Sur une banque en ligne, les '
        + 'classements posés par d\'autres personnes sur ce nœud seront perdus.');
      if (!ok) return;
      await api(url + '?mode=reparent', {method: 'DELETE'});
    }
    if (this.selected === n.id) this.selected = '';
    this.picked.delete(n.id);
    this._status('Catégorie supprimée ✓');
    await this.refresh();
    this._emitFilter(true);
  };

  Tree.prototype._emitFilter = function () {
    if (this.opts.mode !== 'filter' || !this.opts.onFilterChange) return;
    this.opts.onFilterChange(this.getFilter());
  };

  // -- API publique ---------------------------------------------------------
  Tree.prototype.getFilter = function () {
    return {
      category: (this.selected && this.selected !== '__none__') ? this.selected : '',
      uncategorized: this.selected === '__none__',
      descendants: this.descendants,
    };
  };
  Tree.prototype.applyTo = function (params) {
    const f = this.getFilter();
    if (f.category) {
      params.set('category', f.category);
      if (!f.descendants) params.set('descendants', '0');
    }
    if (f.uncategorized) params.set('uncategorized', '1');
    return params;
  };
  // La clé de repli dépend de la banque active, connue seulement après
  // /api/bank/auth-status : on la repose après coup plutôt que d'attendre.
  Tree.prototype.setStorageKey = function (k) {
    if (!k || k === this.opts.storageKey) return;
    this.opts.storageKey = k;
    this.collapsed = this._loadCollapsed();
    this.render();
  };
  Tree.prototype.getPicked = function () { return [...this.picked]; };
  Tree.prototype.setPicked = function (ids) {
    // ⚠ Filtre sur l'arbre courant. Les ids peuvent venir d'un `localStorage`
    // écrit avant qu'une catégorie ne soit supprimée : les garder ferait
    // échouer l'enregistrement en 404 sur un identifiant inconnu.
    this.picked = new Set((ids || []).filter(id => this.byId[id]));
    // Déplie les branches des nœuds cochés, sinon la sélection est invisible.
    this.picked.forEach(id => {
      let p = (this.byId[id] || {}).parent_id;
      while (p) { this.collapsed.delete(p); p = (this.byId[p] || {}).parent_id; }
    });
    this.render();
  };
  Tree.prototype.pathOf = function (id) {
    const n = this.byId[id];
    return n ? n.path : null;
  };
  Tree.prototype.isEmpty = function () { return !this.nodes.length; };

  window.AMCxBankTree = {
    create: function (host, opts) { return new Tree(host, opts); },
    DND_TYPE: DND_TYPE,
  };
})();
