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
- **Email** doit être activé par défaut (magic link OK).
- Optionnel : activer GitHub / Google (clic + suivre la doc).

Dashboard → **Authentication → URL Configuration** :
- **Site URL** : `http://localhost:5050`
- **Redirect URLs** : ajouter `http://localhost:5050/api/bank/auth/callback`
  (+ d'autres si tu prévois différents ports).

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
