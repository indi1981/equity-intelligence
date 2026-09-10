# Equity Intelligence V7 — déploiement Render

## Architecture

- `index.html` : dashboard responsive PC + smartphone.
- `equity_backend_sec.py` : FastAPI, SEC EDGAR/XBRL côté serveur + Twelve Data pour le cours.
- `requirements.txt` : dépendances Python.
- `render.yaml` : service Render.

## Variables Render

Dans **Render → ton service → Environment**, ajoute :

- `SEC_USER_AGENT` = `Equity Intelligence (ton-email@example.com)`
- `TWELVEDATA_API_KEY` = ta clé Twelve Data

**Ne mets jamais la clé Twelve Data dans `index.html`, GitHub, `render.yaml` ou dans un message public.** Render recommande les variables d'environnement pour les secrets : https://render.com/docs/configure-environment-variables

## Twelve Data

Le backend appelle Twelve Data uniquement côté serveur. Le frontend ne voit jamais la clé.

Endpoint utilisé : `/quote?symbol=...`.

Le dashboard récupère notamment : cours, précédente clôture, variation %, devise, marché et timestamp. Si la clé est absente ou si Twelve Data renvoie une erreur, le dashboard affiche clairement l'état au lieu d'inventer un cours.

Le plan gratuit actuel de Twelve Data inclut 8 crédits API/minute et 800/jour, avec les actions US en temps réel ; vérifie les conditions de licence/d'affichage correspondant à ton usage avant de rendre le dashboard public : https://twelvedata.com/pricing

## SEC / XBRL

Le backend récupère :

1. registre ticker → CIK ;
2. submissions SEC → 10-Q/10-K/20-F/40-F ;
3. companyfacts XBRL ;
4. normalisation des métriques ;
5. reconstruction trimestrielle ;
6. LTM et ratios ;
7. provenance / accession / lien SEC.

## Reconstruction trimestrielle — correction V7

Pour les mesures de flux :

- Les périodes sont regroupées par **année fiscale (`fy`) et `fp`**, et non par année civile.
- Q1/Q2/Q3 privilégient un fait XBRL de **3 mois** issu du 10-Q.
- Si le 3 mois n’existe pas, Q2 = cumul 6 mois − Q1.
- Si le 3 mois n’existe pas, Q3 = cumul 9 mois − cumul 6 mois.
- Q4 = exercice annuel FY − Q1 − Q2 − Q3.
- Les amendements sont arbitrés par la date de dépôt la plus récente.
- Le tableau conserve pour chaque trimestre la méthode et le fait XBRL source dans `provenance`.

Capex est normalisé en sortie positive : `FCF = CFO − |Capex|`.

### EPS et actions

Les EPS trimestriels ne sont **jamais** fabriqués en faisant `EPS annuel − EPS Q1 − EPS Q2 − EPS Q3`.

- EPS trimestriel rapporté : conservé tel quel.
- À défaut, Q1/Q2/Q3 peuvent utiliser résultat net trimestriel / actions diluées moyennes du même `fy`/`fp`.
- Q4 n’est jamais obtenu par soustraction d’EPS : il reste vide si aucun EPS trimestriel fiable n’est disponible.
- Si Q4 ne dispose pas d'un EPS trimestriel exploitable, le dashboard laisse Q4 EPS vide plutôt que de fabriquer une valeur.
- L'EPS LTM peut utiliser le résultat net LTM / moyenne annuelle diluée lorsqu'il n'est pas possible de sommer quatre EPS trimestriels fiables.
- Les actions pondérées ne sont jamais soustraites comme un flux YTD.

## LTM

LTM = somme des quatre derniers trimestres exploitables pour les flux : CA, gross profit, operating income, net income, CFO, Capex et FCF.

EBITDA LTM = Operating income LTM + D&A LTM uniquement si la donnée D&A est disponible.

## Valorisation

Calculée uniquement à partir des données disponibles :

- P/E LTM = cours / EPS LTM
- P/FCF = cours / FCF par action LTM
- P/OCF = cours / OCF par action LTM

Le dashboard **ne fabrique pas de P/E NTM** sans consensus/estimations identifiés.

## Analyse des nouveaux filings

L'analyse des nouveaux 10-Q/10-K n'est **pas automatique**.

Le bouton **« Analyser nouveaux 10-Q/10-K »** recharge les submissions SEC et compare les accession numbers avec la dernière analyse conservée dans le `localStorage` du navigateur. Seuls les nouveaux filings sont signalés.

La première analyse sert de référence ; les clics suivants détectent les nouveaux documents.

## Projection investisseur 5 ans

Les projections sont des hypothèses utilisateur :

- croissance EPS ;
- croissance FCF/action ;
- croissance OCF/action ;
- variation annuelle du nombre d'actions ;
- P/E, P/FCF et P/OCF cibles.

Les projections par action tiennent compte de la dilution/du rachat via :

`métrique par action suivante = métrique par action actuelle × (1 + croissance métrique) / (1 + croissance du nombre d'actions)`.

## Déploiement

1. Remplacer les fichiers du dépôt GitHub par ceux de cette version.
2. Commit + Push.
3. Render redéploie le service.
4. Dans Render → Environment, ajouter `TWELVEDATA_API_KEY`.
5. Sauvegarder puis redéployer.
6. Tester `/health` et ensuite analyser un ticker comme `MSFT` ou `NVDA`.

Le secret Twelve Data doit rester côté Render ; ne pas l'inclure dans le dépôt.

## Graphiques V7

Les graphiques CA, bénéfice net et FCF sont désormais des **bar charts purs** : aucune polyline ni aucun point ne sont superposés. Les valeurs sont affichées au-dessus des barres. Les graphiques de marges/croissance restent linéaires.

## Tests effectués

Le backend a été compilé après modification et la reconstruction trimestrielle a été testée sur des cas synthétiques :
- FY 100, Q1 20, Q2 25, Q3 30 → Q4 25 ;
- FY 100, Q1 20, H1 45, M9 75 → Q2 25, Q3 30, Q4 25.

Ces tests vérifient les formules sans dépendre d’un jeu de données SEC particulier.
