/* Onglet Fichiers — arborescence du dossier de travail.
 *
 * ⚠ Aucun `innerHTML` avec un nom de fichier. Les noms viennent du disque de
 * l'utilisateur : un fichier nommé `<img onerror=...>` est du contenu, pas du
 * balisage. Tout passe par `createElement` + `textContent`.
 *
 * ⚠ L'API ne parle qu'en chemins RELATIFS à la racine. Le front n'en construit
 * jamais un autrement qu'en concaténant `parent + '/' + nom` d'entrées que le
 * serveur lui a rendues : il ne peut pas désigner l'extérieur.
 */
(function () {
'use strict';

const WS = {
  root: '',            // chemin absolu de la racine (affichage seulement)
  sel: null,           // { rel, is_dir, name, project }
  open: new Set(),     // rel des dossiers dépliés
  hidden: false,       // afficher les entrées cachées
  cache: new Map(),    // rel → entrées (évite de re-fetcher au repli/dépli)
  order: [],           // rel visibles, dans l'ordre — pour les flèches
};

const OPEN_KEY = 'amcx-ws-open';

/* ---------------------------------------------------------------- utils */

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function api(url, opts) {
  return fetch(url, opts).then(async r => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  });
}

function post(url, body) {
  return api(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
}

function toast(msg, kind) {
  const b = document.getElementById('ws-status');
  if (!b) return;
  b.textContent = msg;
  b.className = 'ws-status' + (kind ? ' is-' + kind : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { b.textContent = ''; }, 5000);
}

function fail(e) { toast('✘ ' + (e && e.message ? e.message : e), 'err'); }

function humanSize(n) {
  if (!n) return '—';
  const u = ['o', 'ko', 'Mo', 'Go'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + u[i];
}

function humanDate(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('fr-FR',
    {year: 'numeric', month: '2-digit', day: '2-digit',
     hour: '2-digit', minute: '2-digit'});
}

const EXT_ICON = {
  pdf: '📕', tex: '📐', csv: '📊', xlsx: '📊', xlsm: '📊', xls: '📊',
  json: '🧾', png: '🖼', jpg: '🖼', jpeg: '🖼', txt: '📄', md: '📄',
  py: '🐍', zip: '🗜', xy: '📐',
};

function iconOf(e) {
  if (e.project) return '🎓';
  if (e.is_dir) return WS.open.has(e.rel) ? '📂' : '📁';
  return EXT_ICON[e.ext] || '📄';
}

function parentOf(rel) {
  const i = (rel || '').lastIndexOf('/');
  return i < 0 ? '' : rel.slice(0, i);
}

/* -------------------------------------------------------- état déplié */

function loadOpen() {
  try {
    const raw = localStorage.getItem(OPEN_KEY + ':' + WS.root);
    if (raw) JSON.parse(raw).forEach(r => WS.open.add(r));
  } catch (e) { /* stockage indisponible : l'arbre repart replié, sans casse */ }
}

function saveOpen() {
  try {
    localStorage.setItem(OPEN_KEY + ':' + WS.root,
                         JSON.stringify([...WS.open]));
  } catch (e) { /* idem */ }
}

/* ------------------------------------------------------------- rendu */

async function entriesOf(rel) {
  if (WS.cache.has(rel)) return WS.cache.get(rel);
  const j = await api('/api/workspace/list?path=' + encodeURIComponent(rel) +
                      (WS.hidden ? '&hidden=1' : ''));
  WS.cache.set(rel, j.entries);
  return j.entries;
}

function invalidate(rel) {
  WS.cache.delete(rel || '');
  // Le parent d'un déplacement comme sa destination sont à relire.
  WS.cache.delete(parentOf(rel || ''));
}

async function renderInto(host, rel, depth) {
  let entries;
  try {
    entries = await entriesOf(rel);
  } catch (e) {
    host.appendChild(el('div', 'ws-empty-child', '✘ ' + e.message));
    return;
  }
  if (!entries.length) {
    host.appendChild(el('div', 'ws-empty-child', 'vide'));
    return;
  }
  for (const e of entries) {
    host.appendChild(rowOf(e, depth));
    if (e.is_dir && WS.open.has(e.rel)) {
      const kids = el('div', 'ws-children');
      kids.style.setProperty('--d', depth + 1);
      kids.dataset.rel = e.rel;
      host.appendChild(kids);
      await renderInto(kids, e.rel, depth + 1);
    }
  }
}

function rowOf(e, depth) {
  const row = el('div', 'ws-row');
  row.dataset.rel = e.rel;
  row.dataset.dir = e.is_dir ? '1' : '';
  row.tabIndex = -1;
  row.style.paddingLeft = (6 + depth * 14) + 'px';
  if (e.name.startsWith('.')) row.classList.add('is-hidden-entry');
  if (WS.sel && WS.sel.rel === e.rel) row.classList.add('is-selected');

  const caret = el('span', 'ws-caret' + (e.is_dir ? '' : ' is-leaf'),
                   e.is_dir ? (WS.open.has(e.rel) ? '▾' : '▸') : '▸');
  caret.addEventListener('click', ev => { ev.stopPropagation(); toggle(e); });
  row.appendChild(caret);

  row.appendChild(el('span', 'ws-icon', iconOf(e)));
  row.appendChild(el('span', 'ws-label', e.name));

  if (e.project) {
    // ⚠ Le projet ACTIF doit se distinguer des autres : c'est celui que les
    // autres onglets montrent. Sans la pastille, on croit corriger l'examen
    // qu'on a sous les yeux ici. `WS.active` pointe le dossier qui porte
    // `sujet/exam.tex`, donc le projet lui-même OU son `auto_grading/`.
    const abs = WS.root + '/' + e.rel;
    const isActive = !!WS.active && (WS.active === abs ||
                                     WS.active === abs + '/auto_grading');
    row.appendChild(el('span', 'ws-badge' + (isActive ? ' is-active' : ''),
                       isActive ? 'projet actif' : 'projet'));
  }

  const menu = el('button', 'ws-menu-btn', '⋯');
  menu.type = 'button';
  menu.title = 'Actions';
  menu.addEventListener('click', ev => {
    ev.stopPropagation();
    select(e);
    const r = menu.getBoundingClientRect();
    openMenu(r.left, r.bottom + 2, e);
  });
  row.appendChild(menu);

  row.addEventListener('click', () => select(e));
  row.addEventListener('dblclick', () => { if (e.is_dir) toggle(e); });
  row.addEventListener('contextmenu', ev => {
    ev.preventDefault();
    select(e);
    openMenu(ev.clientX, ev.clientY, e);
  });

  // Déplacement par glisser-déposer.
  row.draggable = true;
  row.addEventListener('dragstart', ev => {
    ev.dataTransfer.setData('application/x-amcx-ws', e.rel);
    ev.dataTransfer.effectAllowed = 'move';
    row.classList.add('is-dragging');
  });
  row.addEventListener('dragend', () => row.classList.remove('is-dragging'));
  if (e.is_dir) {
    row.addEventListener('dragover', ev => {
      // ⚠ `getData` est interdit pendant `dragover` : seuls les TYPES sont
      // lisibles. C'est la seule façon de savoir si le survol nous concerne
      // avant d'accepter le dépôt.
      const t = [...ev.dataTransfer.types];
      if (!t.includes('application/x-amcx-ws') && !t.includes('Files')) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = t.includes('Files') ? 'copy' : 'move';
      row.classList.add('is-drop');
    });
    row.addEventListener('dragleave', () => row.classList.remove('is-drop'));
    row.addEventListener('drop', ev => {
      row.classList.remove('is-drop');
      const src = ev.dataTransfer.getData('application/x-amcx-ws');
      if (src) { ev.preventDefault(); doMove(src, e.rel); return; }
      if (ev.dataTransfer.files && ev.dataTransfer.files.length) {
        ev.preventDefault();
        doUpload(e.rel, ev.dataTransfer.files);
      }
    });
  }
  return row;
}

async function refresh(keepSel) {
  const host = document.getElementById('ws-tree');
  if (!host) return;
  host.innerHTML = '';
  await renderInto(host, '', 0);
  WS.order = [...host.querySelectorAll('.ws-row')].map(r => r.dataset.rel);
  if (keepSel !== false) renderDetail();
}

function toggle(e) {
  if (!e.is_dir) return;
  if (WS.open.has(e.rel)) WS.open.delete(e.rel); else WS.open.add(e.rel);
  saveOpen();
  refresh();
}

function select(e) {
  WS.sel = e;
  document.querySelectorAll('.ws-row.is-selected')
          .forEach(r => r.classList.remove('is-selected'));
  const row = document.querySelector('.ws-row[data-rel="' + cssEscape(e.rel) + '"]');
  if (row) { row.classList.add('is-selected'); row.focus({preventScroll: true}); }
  renderDetail();
}

function cssEscape(s) {
  return (window.CSS && CSS.escape) ? CSS.escape(s)
                                    : String(s).replace(/["\\]/g, '\\$&');
}

/* ------------------------------------------------- panneau de détail */

async function renderDetail() {
  const host = document.getElementById('ws-detail');
  if (!host) return;
  host.innerHTML = '';
  if (!WS.sel) {
    const p = el('p', 'banque-empty',
      'Sélectionne un dossier ou un fichier à gauche pour le voir ici. '
      + 'Toutes les actions sont au clic droit dans l’arbre.');
    host.appendChild(p);
    return;
  }
  let info;
  try {
    info = (await api('/api/workspace/info?path=' +
                      encodeURIComponent(WS.sel.rel))).entry;
  } catch (e) { fail(e); return; }

  // Fil d'Ariane : remonter d'un cran est le geste le plus fréquent.
  const crumb = el('div', 'ws-crumb');
  const parts = info.rel ? info.rel.split('/') : [];
  const rootBtn = el('button', null, WS.name || 'racine');
  rootBtn.addEventListener('click', () => selectRel(''));
  crumb.appendChild(rootBtn);
  parts.forEach((seg, i) => {
    crumb.appendChild(document.createTextNode(' › '));
    if (i === parts.length - 1) { crumb.appendChild(document.createTextNode(seg)); return; }
    const b = el('button', null, seg);
    const target = parts.slice(0, i + 1).join('/');
    b.addEventListener('click', () => selectRel(target));
    crumb.appendChild(b);
  });
  host.appendChild(crumb);

  const h = el('h2');
  h.appendChild(el('span', null, iconOf(info)));
  h.appendChild(el('span', null, info.name));
  host.appendChild(h);

  const meta = el('div', 'ws-meta');
  const put = (k, v) => {
    const s = el('span');
    s.appendChild(document.createTextNode(k + ' '));
    s.appendChild(el('b', null, v));
    meta.appendChild(s);
  };
  put('Type', info.is_dir ? (info.project ? 'projet AMCx' : 'dossier') :
                            (info.ext ? '.' + info.ext : 'fichier'));
  if (info.is_dir) put('Contenu', (info.n_items || 0) + ' élément(s)');
  else put('Taille', humanSize(info.size));
  put('Modifié', humanDate(info.mtime));
  if (info.is_link) put('Lien', 'symbolique');
  host.appendChild(meta);

  // ⚠ Seule commande conservée à droite : ouvrir le projet n'est pas une
  // opération de fichier, c'est ce que l'application fait. Les actions de
  // gestion (créer, renommer, déplacer, supprimer) vivent au clic droit, sur
  // l'élément qu'elles visent — pas à l'autre bout de l'écran.
  if (info.project_root) {
    const card = el('div', 'ws-proj-card');
    card.appendChild(el('span', null, '🎓'));
    card.appendChild(el('span', 'ws-proj-txt',
      'Projet AMCx complet. L’ouvrir bascule l’application dessus : '
      + 'le serveur redémarre, les autres onglets changent d’examen.'));
    const b = el('button', 'btn btn-primary', 'Ouvrir ce projet');
    b.type = 'button';
    b.addEventListener('click', () => openProject(info.project_root, info.name));
    card.appendChild(b);
    host.appendChild(card);
  }

  if (!info.is_dir) renderPreview(host, info);
}

/* Aperçu — un panneau de détail vide n'aide personne, et l'usage courant est
 * de vérifier un notes.csv ou une page scannée sans quitter l'onglet. */
async function renderPreview(host, info) {
  const box = el('div', 'ws-preview');
  box.appendChild(el('div', 'ws-preview-load', '⏳ aperçu…'));
  host.appendChild(box);
  let j;
  try {
    j = await api('/api/workspace/preview?path=' + encodeURIComponent(info.rel));
  } catch (e) { box.remove(); return; }
  box.innerHTML = '';
  if (j.kind === 'text') {
    const pre = el('pre', 'ws-preview-text');
    pre.textContent = j.text;             // contenu du disque : jamais en HTML
    box.appendChild(pre);
    if (j.truncated) {
      box.appendChild(el('div', 'ws-preview-note',
        'Aperçu tronqué à ' + humanSize(200000) + ' — télécharge le fichier '
        + 'pour le voir en entier.'));
    }
  } else if (j.kind === 'inline') {
    const src = '/api/workspace/view?path=' + encodeURIComponent(info.rel);
    if (j.mime === 'application/pdf') {
      const f = el('iframe', 'ws-preview-pdf');
      f.src = src;
      f.title = info.name;
      box.appendChild(f);
    } else {
      const img = el('img', 'ws-preview-img');
      img.src = src;
      img.alt = info.name;
      box.appendChild(img);
    }
  } else {
    box.appendChild(el('div', 'ws-preview-note',
      'Pas d’aperçu pour ce type de fichier (' + humanSize(j.size) + ').'));
  }
}

async function selectRel(rel) {
  if (!rel) { WS.sel = null; renderDetail(); return; }
  try {
    const info = (await api('/api/workspace/info?path=' +
                            encodeURIComponent(rel))).entry;
    select(info);
  } catch (e) { fail(e); }
}

/* ------------------------------------------------------------ actions */

async function askMkdir(parentRel) {
  const name = prompt('Nom du nouveau dossier :', '');
  if (name == null) return;
  try {
    const j = await post('/api/workspace/mkdir', {parent: parentRel, name: name});
    invalidate(j.path);
    WS.open.add(parentRel); saveOpen();
    await refresh();
    selectRel(j.path);
    toast('✓ Dossier créé');
  } catch (e) { fail(e); }
}

async function askRename(rel, current) {
  const name = prompt('Nouveau nom :', current);
  if (name == null || name === current) return;
  try {
    const j = await post('/api/workspace/rename', {path: rel, name: name});
    WS.cache.clear();
    // Le dépli suit le renommage, sinon la branche se replie sous l'utilisateur.
    if (WS.open.delete(rel)) WS.open.add(j.path);
    saveOpen();
    await refresh();
    selectRel(j.path);
    toast('✓ Renommé');
  } catch (e) { fail(e); }
}

async function doMove(src, dest) {
  if (src === dest) return;
  try {
    const j = await post('/api/workspace/move', {path: src, dest: dest});
    WS.cache.clear();
    WS.open.add(dest); saveOpen();
    await refresh();
    selectRel(j.path);
    toast('✓ Déplacé dans « ' + (dest || WS.name) + ' »');
  } catch (e) { fail(e); }
}

async function askDelete(info) {
  // ⚠ La confirmation NOMME ce qu'on va perdre, et dit que c'est réversible :
  // un « Confirmer ? » nu ne protège personne, et cacher la corbeille ferait
  // hésiter là où il n'y a pas de risque.
  const what = info.is_dir
    ? (info.project ? 'le projet AMCx « ' + info.name + ' » et tout son contenu '
                      + '(sujet, scans, corrections, notes)'
                    : 'le dossier « ' + info.name + ' » et ses '
                      + (info.n_items || 0) + ' élément(s)')
    : 'le fichier « ' + info.name + ' »';
  if (!confirm('Mettre à la corbeille ' + what + ' ?\n\n'
               + 'Rien n’est supprimé : le contenu part dans la corbeille du '
               + 'dossier de travail, d’où il peut être restauré.')) return;
  try {
    const j = await post('/api/workspace/delete', {path: info.rel});
    if (j.failed && j.failed.length) { fail(j.failed[0].error); return; }
    WS.cache.clear();
    WS.sel = null;
    await refresh();
    setTrashCount(j.n_trash);
    toast('✓ « ' + info.name + ' » est à la corbeille');
  } catch (e) { fail(e); }
}

async function askProject(parentRel) {
  const name = prompt('Nom du nouveau projet AMC :', '');
  if (name == null || !name.trim()) return;
  const abs = WS.root + (parentRel ? '/' + parentRel : '');
  if (!confirm('Créer le projet « ' + name + ' » dans :\n' + abs
               + '\n\nL’application basculera dessus (le serveur redémarre).')) return;
  toast('⏳ Création du projet…');
  try {
    await post('/api/projects/create',
               {name: name.trim(), template: 'examen_minimal', parent: abs});
  } catch (e) {
    // ⚠ La création se termine par un suicide du serveur : l'échec du
    // transport ne dit rien du résultat. On attend le retour et on vérifie.
    return waitForProject(name.trim());
  }
  waitForProject(name.trim());
}

async function openProject(absPath, name) {
  if (!confirm('Ouvrir le projet « ' + name + ' » ?\n\n'
               + 'Le serveur redémarre et tous les onglets basculent sur cet examen.')) return;
  toast('⏳ Basculement…');
  try { await post('/api/projects/open', {path: absPath}); } catch (e) { /* cf. ci-dessous */ }
  waitForProject(name);
}

function waitForProject(name) {
  // Le serveur se relance : on sonde jusqu'à ce qu'il réponde, puis on
  // recharge. Conclure à l'échec sur une erreur réseau serait faux.
  let n = 0;
  const tick = () => {
    n++;
    fetch('/api/projects').then(r => r.json()).then(() => {
      window.location.reload();
    }).catch(() => {
      if (n > 60) { toast('✘ Le serveur ne répond pas.', 'err'); return; }
      setTimeout(tick, 400);
    });
  };
  setTimeout(tick, 900);
}

function pickUpload(destRel) {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.multiple = true;
  inp.addEventListener('change', () => {
    if (inp.files && inp.files.length) doUpload(destRel, inp.files);
  });
  inp.click();
}

async function doUpload(destRel, files) {
  const fd = new FormData();
  fd.append('dest', destRel);
  for (const f of files) fd.append('files', f);
  toast('⏳ Dépôt de ' + files.length + ' fichier(s)…');
  try {
    const j = await api('/api/workspace/upload', {method: 'POST', body: fd});
    WS.cache.clear();
    WS.open.add(destRel); saveOpen();
    await refresh();
    if (j.failed && j.failed.length) {
      toast('⚠ ' + j.saved.length + ' déposé(s), ' + j.failed.length
            + ' refusé(s) : ' + j.failed[0].error, 'err');
    } else {
      toast('✓ ' + j.saved.length + ' fichier(s) déposé(s)');
    }
  } catch (e) { fail(e); }
}

/* ------------------------------------------------------ menu contextuel */

let ctxNode = null;

function closeMenu() {
  if (ctxNode) { ctxNode.remove(); ctxNode = null; }
}

function openMenu(x, y, e) {
  closeMenu();
  const m = el('div', 'ws-ctx');
  const item = (label, fn, cls) => {
    const b = el('button', cls || null, label);
    b.type = 'button';
    b.addEventListener('click', () => { closeMenu(); fn(); });
    m.appendChild(b);
  };
  const sep = () => m.appendChild(el('div', 'ws-ctx-sep'));

  // `e === null` : clic droit sur le vide du panneau. Sans ce cas, on ne
  // pourrait plus rien créer à la racine dès qu'un dossier est sélectionné.
  const rel = e ? e.rel : '';
  const isDir = e ? e.is_dir : true;

  if (!e) m.appendChild(el('div', 'ws-ctx-head', WS.name || 'racine'));

  if (e && e.project) {
    item('🎓 Ouvrir ce projet', () => openProject(WS.root + '/' + e.rel, e.name));
    sep();
  }
  if (isDir) {
    // « sous-dossier » n'a de sens que sous quelque chose : sur le vide du
    // panneau, la cible est la racine.
    item(e ? '＋ Nouveau sous-dossier' : '＋ Nouveau dossier', () => askMkdir(rel));
    item('🎓 Nouveau projet AMC ici', () => askProject(rel));
    item('⤒ Déposer des fichiers…', () => pickUpload(rel));
  } else {
    item('⤓ Télécharger', () => {
      window.location = '/api/workspace/download?path=' + encodeURIComponent(rel);
    });
  }
  if (e) {
    sep();
    item('✎ Renommer…', () => askRename(e.rel, e.name));
    if (parentOf(e.rel)) item('↗ Déplacer vers la racine', () => doMove(e.rel, ''));
    sep();
    item('🗑 Supprimer…', async () => {
      try {
        const info = (await api('/api/workspace/info?path=' +
                                encodeURIComponent(e.rel))).entry;
        askDelete(info);
      } catch (err) { fail(err); }
    }, 'is-danger');
  }

  m.style.visibility = 'hidden';
  document.body.appendChild(m);
  const r = m.getBoundingClientRect();
  // Ramené dans la fenêtre : ancré au clic, un menu sort de l'écran dès qu'on
  // clique en bas à droite.
  m.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 6)) + 'px';
  m.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 6)) + 'px';
  m.style.visibility = '';
  ctxNode = m;
}

document.addEventListener('click', closeMenu);
document.addEventListener('scroll', closeMenu, true);
window.addEventListener('resize', closeMenu);

/* ------------------------------------------------------------ corbeille */

function setTrashCount(n) {
  const b = document.getElementById('ws-trash-btn');
  if (!b) return;
  b.textContent = '🗑 Corbeille' + (n ? ' (' + n + ')' : '');
  b.classList.toggle('has-items', !!n);
}

async function showTrash() {
  const host = document.getElementById('ws-detail');
  let j;
  try { j = await api('/api/workspace/trash'); } catch (e) { return fail(e); }
  WS.sel = null;
  document.querySelectorAll('.ws-row.is-selected')
          .forEach(r => r.classList.remove('is-selected'));
  host.innerHTML = '';
  host.appendChild(el('h2', null, '🗑 Corbeille'));
  if (!j.entries.length) {
    host.appendChild(el('p', 'banque-empty', 'La corbeille est vide.'));
    return;
  }
  host.appendChild(el('p', 'ws-trash-warn',
    j.entries.length + ' élément(s) · ' + j.size.n_files + ' fichier(s) · '
    + humanSize(j.size.n_bytes)
    + ' — restaurés à leur emplacement d’origine quand il est libre.'));
  const list = el('div', 'ws-trash-list');
  j.entries.forEach(t => {
    const row = el('div', 'ws-trash-item');
    row.appendChild(el('span', null, t.is_dir ? '📁' : '📄'));
    row.appendChild(el('span', null, t.name));
    row.appendChild(el('span', 'ws-trash-origin',
                       (t.origin ? 'depuis ' + t.origin + ' · ' : '')
                       + t.deleted_at.replace('T', ' ')));
    const b = el('button', 'btn btn-small', '↩ Restaurer');
    b.type = 'button';
    b.addEventListener('click', async () => {
      try {
        const r = await post('/api/workspace/trash/restore', {slot: t.slot});
        WS.cache.clear();
        await refresh(false);
        setTrashCount(r.n_trash);
        showTrash();
        toast('✓ Restauré dans « ' + (r.path || WS.name) + ' »');
      } catch (e) { fail(e); }
    });
    row.appendChild(b);
    list.appendChild(row);
  });
  host.appendChild(list);

  const act = el('div', 'ws-detail-actions');
  const empty = el('button', 'btn bq-danger', '🔥 Vider la corbeille');
  empty.type = 'button';
  empty.addEventListener('click', async () => {
    // ⚠ La seule action irréversible de l'onglet : elle annonce exactement
    // ce qu'elle détruit, et le mot « définitivement » y est.
    if (!confirm('Supprimer DÉFINITIVEMENT ' + j.size.n_files + ' fichier(s) ('
                 + humanSize(j.size.n_bytes) + ') ?\n\n'
                 + 'Cette action ne peut pas être annulée.')) return;
    try {
      await post('/api/workspace/trash/empty', {});
      setTrashCount(0);
      showTrash();
      toast('✓ Corbeille vidée');
    } catch (e) { fail(e); }
  });
  act.appendChild(empty);
  host.appendChild(act);
}

/* -------------------------------------------------------------- clavier */

function onKey(ev) {
  if (ctxNode && ev.key === 'Escape') { closeMenu(); return; }
  const tag = (ev.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
  if (!WS.sel) return;
  const i = WS.order.indexOf(WS.sel.rel);
  if (ev.key === 'ArrowDown' && i >= 0 && i + 1 < WS.order.length) {
    ev.preventDefault(); selectRel(WS.order[i + 1]);
  } else if (ev.key === 'ArrowUp' && i > 0) {
    ev.preventDefault(); selectRel(WS.order[i - 1]);
  } else if (ev.key === 'ArrowRight' && WS.sel.is_dir && !WS.open.has(WS.sel.rel)) {
    ev.preventDefault(); toggle(WS.sel);
  } else if (ev.key === 'ArrowLeft') {
    ev.preventDefault();
    if (WS.sel.is_dir && WS.open.has(WS.sel.rel)) toggle(WS.sel);
    else selectRel(parentOf(WS.sel.rel));
  } else if (ev.key === 'F2') {
    ev.preventDefault(); askRename(WS.sel.rel, WS.sel.name);
  } else if (ev.key === 'Delete') {
    ev.preventDefault();
    api('/api/workspace/info?path=' + encodeURIComponent(WS.sel.rel))
      .then(j => askDelete(j.entry)).catch(fail);
  }
}

/* ------------------------------------------------------------- démarrage */

function boot(state) {
  WS.root = state.root || '';
  WS.name = state.name || '';
  WS.active = state.active || '';
  if (!WS.root) return;                       // l'écran d'amorçage prend la main
  loadOpen();
  setTrashCount(state.n_trash || 0);
  refresh();

  document.getElementById('ws-new-folder')
    .addEventListener('click', () => askMkdir(currentDir()));
  document.getElementById('ws-new-project')
    .addEventListener('click', () => askProject(currentDir()));
  document.getElementById('ws-upload')
    .addEventListener('click', () => pickUpload(currentDir()));
  document.getElementById('ws-trash-btn')
    .addEventListener('click', showTrash);
  const hid = document.getElementById('ws-hidden');
  if (hid) hid.addEventListener('change', () => {
    WS.hidden = hid.checked;
    WS.cache.clear();
    refresh();
  });
  document.addEventListener('keydown', onKey);

  // Dépôt de fichiers n'importe où dans le panneau = dépôt dans le dossier
  // courant. Viser une ligne précise reste possible, mais ne doit pas être
  // obligatoire.
  const panel = document.getElementById('ws-tree-panel');
  panel.addEventListener('contextmenu', ev => {
    if (ev.target.closest('.ws-row')) return;      // la ligne s'en charge
    ev.preventDefault();
    openMenu(ev.clientX, ev.clientY, null);
  });
  panel.addEventListener('dragover', ev => {
    if (![...ev.dataTransfer.types].includes('Files')) return;
    ev.preventDefault();
    panel.classList.add('is-dropping');
  });
  panel.addEventListener('dragleave', ev => {
    if (ev.target === panel) panel.classList.remove('is-dropping');
  });
  panel.addEventListener('drop', ev => {
    panel.classList.remove('is-dropping');
    if (ev.target.closest('.ws-row[data-dir="1"]')) return;   // déjà traité
    if (!ev.dataTransfer.files || !ev.dataTransfer.files.length) return;
    ev.preventDefault();
    doUpload(currentDir(), ev.dataTransfer.files);
  });
}

function currentDir() {
  if (!WS.sel) return '';
  return WS.sel.is_dir ? WS.sel.rel : parentOf(WS.sel.rel);
}

window.AMCxWorkspace = {boot: boot};
})();
