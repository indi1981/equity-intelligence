# Equity Intelligence — déploiement Render gratuit

## 1. Déployer sur Render
- Mettre **tous les fichiers de ce dossier** dans le dépôt GitHub relié à Render.
- Dans Render : **New → Web Service**.
- Runtime : **Python 3**.
- Build Command : `pip install -r requirements.txt`
- Start Command : `uvicorn equity_backend_sec:app --host 0.0.0.0 --port $PORT`
- Plan : **Free**.

## 2. Variables d’environnement
Dans **Environment**, créer :
- `SEC_USER_AGENT` = `Equity Intelligence/1.0 (guets2000@hotmail.com)`
- Optionnel : `TWELVEDATA_API_KEY` pour le cours de marché fourni par Twelve Data. Ne jamais mettre cette clé dans `index.html` ni dans GitHub.

## 3. Données trimestrielles
Le backend reconstruit les trimestres à partir des faits SEC/XBRL :
- trimestre direct lorsque le filing fournit une période d'environ 3 mois ;
- Q2 = cumul 6 mois − Q1 lorsque seul le cumul est publié ;
- Q3 = cumul 9 mois − cumul 6 mois ;
- Q4 = exercice annuel − Q1 − Q2 − Q3.

Le FCF trimestriel = CFO − |Capex|.
Les marges et le FCF conversion sont recalculés à partir de ces données. Le LTM correspond à la somme des 4 derniers trimestres disponibles. Le P/E affiché est basé sur l'EPS dilué LTM observé/reconstruit ; aucun P/E NTM de consensus n'est inventé.

## 4. Vérification
- `/health` : vérifie que le backend répond.
- `/health/sec` : vérifie que le serveur peut joindre l'API publique SEC avec le User-Agent configuré.
- L'interface appelle ensuite `/api/company?symbol=NVDA` (ou un autre ticker).

## 5. Correctif important de cette version
Une erreur Python bloquait l'extraction trimestrielle : `dict.get()` était appelé avec un argument nommé `key=` dans la reconstruction du cumul 6 mois. En Python, `dict.get()` accepte la clé et la valeur par défaut en arguments positionnels, pas cet argument nommé. Cette version corrige ce point.

## 6. Render
Après remplacement des fichiers dans GitHub :
1. Commit + Push.
2. Render redéploie automatiquement si l'auto-deploy est activé.
3. Attendre la fin du déploiement.
4. Recharger l'URL Render avec un rechargement forcé.

Le service gratuit peut être mis en veille après une période d'inactivité. Le premier appel après veille peut donc être plus lent.
