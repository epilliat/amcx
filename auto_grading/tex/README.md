# `automultiplechoice.sty` — style LaTeX AMC, vendorisé

**Version : 2025/04/17 v1.7.0 r:ec7413fa** (paquet Debian
`auto-multiple-choice-common` 1.7.0). Copyright © 2008-2025 Alexis Bienvenüe,
**GPL v2 ou ultérieure** — l'en-tête de licence du fichier est intact et doit
le rester.

## Pourquoi ce fichier est ici

Ce style **n'est pas sur CTAN** (`ctan.org/pkg/automultiplechoice` → 404) : il
est distribué avec le logiciel AMC, donc empaqueté pour Debian/Ubuntu seulement.
Ni MiKTeX (Windows) ni MacTeX (macOS) ne peuvent l'installer. Sans lui,
`pdflatex` échoue sur `File 'automultiplechoice.sty' not found` → pas de PDF,
pas de calage `.xy`, donc **aucun sujet créable** hors Linux/Debian.

Le vendoriser donne deux choses :

1. **Portabilité** — n'importe quelle distribution TeX suffit. Toutes les *autres*
   dépendances du style (`tikz`, `hyperref`, `fancybox`, `csvsimple`, `environ`,
   `storebox`, `rotating`, `xkeyval`…) sont des paquets CTAN standards que
   MiKTeX installe automatiquement à la demande.
2. **Reproductibilité du calage** — tout le monde compile avec le *même* style.
   Deux versions différentes peuvent produire des positions de cases
   différentes, donc un `.xy` différent : les copies imprimées avec l'une ne
   sont plus alignées avec le calage de l'autre. Panne silencieuse et coûteuse,
   découverte en corrigeant les copies.

## Comment il est utilisé

`sujet_store.compile_pdf()` le copie dans son dossier temporaire de compilation,
à côté d'`exam.tex`. `pdflatex` cherche le répertoire courant en premier : cette
copie a donc la priorité sur une éventuelle installation AMC du système, ce qui
est voulu (déterminisme).

## Mise à jour

Remplacer le fichier, puis **vérifier que le calage n'a pas bougé** avant de
committer : recompiler un sujet de référence et comparer le SHA256 de `exam.xy`.
S'il change, les copies déjà imprimées avec l'ancienne version ne sont plus
alignées.
