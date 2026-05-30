# Setup Supabase pour la banque en ligne AMCx

Démarche pour activer la banque de questions en ligne (Phase A : CRUD multi-user
+ stats par-user). 10 minutes en tout.

## 1. Créer un projet Supabase

1. Aller sur [supabase.com](https://supabase.com) → **Start your project** → se
   connecter (GitHub recommandé).
2. **New project** :
   - Name : `amcx-banque` (ou ce que tu veux)
   - Database password : générer un fort, le noter dans un gestionnaire
   - Region : `eu-west-1` (Ireland) ou la plus proche
   - Plan : **Free**
3. Attendre ~2 minutes que la base soit provisionnée.

## 2. Récupérer les credentials

Dashboard du projet → **Settings → API** :
- **Project URL** (ex. `https://abcdefgh.supabase.co`) → champ `bank_supabase_url`
- **anon / public key** (long JWT commençant par `eyJ…`) → champ `bank_supabase_anon_key`

⚠ Ne **JAMAIS** mettre la `service_role` key dans AMCx — elle bypass RLS. Seule
l'`anon` key est sûre côté client.

## 3. Appliquer le schéma

Dashboard → **SQL editor** → **+ New query** → coller le contenu de
[`schema.sql`](schema.sql) → **Run**.

→ Crée les tables `profiles`, `bank_questions`, `question_evals` + les policies
Row-Level Security. Idempotent : peut être re-run.

## 4. Configurer l'auth

Dashboard → **Authentication → Providers** :
- **Email** doit être activé par défaut (magic link / OTP OK).

Dashboard → **Authentication → URL Configuration** :
- **Site URL** : `http://localhost:5050`
- **Redirect URLs** : ajouter `http://localhost:5050/api/bank/auth/callback`
  (+ d'autres si tu prévois différents ports).

### ⚠ 4.0) Mode invite-only (FORTEMENT recommandé)

**Pourquoi** : par défaut, n'importe qui avec un email valide peut s'inscrire
— y compris tes étudiants — et lirait les questions `status='public'` AVEC
leurs réponses correctes (le champ `data` jsonb contient `{correct: true}`
sur chaque bonne réponse). Catastrophe pour la confidentialité des examens.

**Solution la plus simple** : désactiver le signup public et inviter les
profs un par un.

1. **Désactiver le signup public** :
   - Dashboard → Authentication → Providers → Email
   - **Décocher "Enable email signups"**
   - Save

2. **Inviter un collègue** (chaque prof) :
   - Dashboard → Authentication → Users
   - Bouton **Invite user** → entrer son email pro → Supabase envoie un mail
     avec un lien d'invitation
   - Le prof clique → arrive sur une page de set-up (Supabase hosted) →
     reçoit son magic link → peut se connecter à AMCx normalement

3. **Effet sur AMCx** : un étudiant qui essaie `Recevoir un code` reçoit un
   message d'erreur clair (`Signups not allowed for this instance`).

→ Coût : 30 sec par invitation. Aucun code AMCx à modifier. Tu peux ouvrir
le signup public plus tard si la communauté devient mature et que tu veux
laisser grandir.

### Alternatives (si invite-only ne convient pas)

### 4.a) Whitelist email académique (alternative à invite-only)

Si tu préfères ouvrir le signup à toute la communauté académique au lieu
d'inviter un par un, tu peux filtrer par domaine. **Attention** : ne filtre
pas si les profs ET les étudiants partagent le même domaine
(ex. tous les deux `@ensai.fr`) — dans ce cas seul invite-only protège.

Pour bloquer les signups Gmail/Yahoo/mailinator et garder une communauté
académique :
1. Ouvrir [schema.sql](schema.sql), section `0)`.
2. **Décommenter** le bloc `check_academic_email()` + trigger.
3. **Éditer la regex** pour ajouter/retirer des domaines selon ta cible
   (par défaut : `@ensai.fr`, `@univ-rennes*.fr`, `@inrae.fr`, `@cnrs.fr`,
   `@inria.fr`, `*.ac-XXX.fr`, `*.edu`).
4. Re-run le SQL dans le SQL editor → le trigger est activé.

Test : essaie de t'inscrire avec un email Gmail → tu reçois `Email académique
requis` dans AMCx. Avec un email autorisé → ça passe.

### 4.b) (Optionnel) OAuth Google — login 1-clic pour Google Workspace

Si ton institution est sous Google Workspace académique
(`prenom.nom@ton-institution.fr` hébergé chez Google), tu peux activer
OAuth Google :

1. **Google Cloud Console** ([console.cloud.google.com](https://console.cloud.google.com))
   → créer un projet (gratuit) → APIs & Services → Credentials
   → Create Credentials → OAuth client ID → Web application.
2. **Authorized redirect URIs** : copier l'URL fournie par Supabase
   (Dashboard → Authentication → Providers → Google → "Callback URL").
3. **Restrict by hosted domain** (HD parameter) : pour limiter aux comptes
   `@ton-institution.fr`, ajouter le `hd` param dans la config Supabase
   (ou laisser ouvert si tu acceptes tous les Google Workspaces).
4. **Supabase Dashboard** → Authentication → Providers → Google → activer
   → coller Client ID + Client Secret.

Côté AMCx (Phase A actuelle) : le bouton OAuth Google n'est pas encore dans
l'UI — pour l'instant les profs utilisent le magic link OTP. L'OAuth Google
sera ajouté en Phase B si demandé. **Ceci dit, dès maintenant ils peuvent
utiliser leur email Google Workspace avec le magic link OTP.**

> **OAuth Microsoft 365 / Azure AD** : même principe, suivre
> [supabase.com/docs/guides/auth/social-login/auth-azure](https://supabase.com/docs/guides/auth/social-login/auth-azure).

> **SSO RENATER / Shibboleth (ENT français)** : nécessite Supabase Pro
> ($25/mois pour SAML) + démarches RENATER. Hors-scope MVP.

## 5. Configurer AMCx

Lancer le serveur, aller dans **Dashboard → Réglages → Banque en ligne** :
- Coller l'URL Supabase + la clé anon
- Cliquer **Se connecter** → modal email → magic link → clic dans le mail →
  retour AMCx connecté.

## 6. Vérifier

- Onglet **Sujet** → modal **📚 Banque** → bandeau "online — connecté en tant
  que &lt;ton-email&gt;".
- Créer une question via right-click outline → 💾 Sauver dans la banque.
- Vérifier dans Supabase dashboard → **Table editor → bank_questions** que la
  ligne est apparue.

## Coûts

- **Free tier** : 500 MB DB + 2 GB egress/mois + 50k MAU. Couvre largement
  <1000 profs.
- **Pro** ($25/mois) si tu dépasses : 8 GB DB, 250 GB egress.

## Self-hosting (optionnel)

Supabase est OSS. Pour héberger ta propre instance :
```bash
npm install -g supabase
supabase start    # nécessite Docker
```
Et changer `bank_supabase_url` vers `http://localhost:54321`. Voir
[supabase.com/docs/guides/self-hosting](https://supabase.com/docs/guides/self-hosting).
