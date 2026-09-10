# Equity Intelligence — déploiement Render gratuit

## 1. Déployer sur Render
- Créer/importer ce dossier dans un dépôt GitHub.
- Dans Render : **New → Web Service**.
- Choisir le dépôt.
- Runtime : **Python 3**.
- Build Command : `pip install -r requirements.txt`
- Start Command : `uvicorn equity_backend_sec:app --host 0.0.0.0 --port $PORT`
- Plan : **Free**.

## 2. Variable d’environnement SEC
Dans **Environment**, créer :
- Name : `SEC_USER_AGENT`
- Value : `Equity Intelligence/1.0 (guets2000@hotmail.com)`

## 3. Utilisation
Après déploiement, Render fournit une URL `https://...onrender.com`.
Ouvrir cette URL sur PC, tablette ou smartphone. Le même service sert l’interface HTML et le backend SEC.

## 4. Vérification
- `/health` : vérifie que le backend répond.
- `/health/sec` : vérifie que le serveur peut joindre l’API publique SEC avec le User-Agent configuré.

## 5. Important
Le service gratuit peut être mis en veille après une période d’inactivité. Le premier appel après veille peut donc être plus lent.
