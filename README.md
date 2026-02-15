# Email Scraper

Outil CLI Python pour extraire des adresses email à partir d'URLs listées dans un fichier CSV.

## Fonctionnalités

- **Extraction robuste** : regex stricte qui évite les faux positifs (pas de `098775contact@...Découvrez`)
- **Détection intelligente** : analyse les liens de la homepage pour trouver automatiquement les pages contact, mentions légales, à propos
- **Emails obfusqués** : détecte `contact[at]domain[dot]com`, `contact (at) domain . fr`, etc.
- **Score de confiance** : chaque email reçoit un score (0.0-1.0) basé sur la source, le contexte et le type de page
- **Dédoublonnage** : garde le meilleur résultat par email
- **Rate limiting** : respect du `robots.txt` et délai entre requêtes
- **Cache** : évite le re-scraping avec un cache local
- **Export dual** : CSV (colonnes demandées) + JSON (métadonnées complètes)
- **Mode dry-run** : teste la détection de pages sans scraper
- **Parallélisation** : multi-thread avec `concurrent.futures`
- **Webhook** : notification optionnelle en fin de scraping

## Installation

```bash
pip install -r requirements.txt
```

Ou en mode développement :

```bash
pip install -e .
```

## Utilisation

### Format CSV d'entrée

Le fichier CSV doit contenir une colonne `url` (ou `urls`, `website`, `site`, `link`) :

```csv
url
https://example-company.fr
https://another-site.com
mon-site.fr
```

### Commandes

**Utilisation basique :**

```bash
python -m email_scraper -i urls.csv -o results.csv
```

**Avec parallélisation et logs détaillés :**

```bash
python -m email_scraper -i urls.csv -o results.csv -t 4 -vv
```

**Avec cache et export JSON :**

```bash
python -m email_scraper -i urls.csv -o results.csv --cache .cache --json results.json
```

**Mode dry-run (test sans extraction) :**

```bash
python -m email_scraper -i urls.csv -o results.csv --dry-run -v
```

**Toutes les options :**

```bash
python -m email_scraper -i urls.csv -o results.csv \
  -t 4 \
  --timeout 20 \
  --rate-limit 1.5 \
  --cache .cache \
  --json results.json \
  --webhook https://hooks.example.com/notify \
  --log-file scraper.log \
  -vv
```

Si installé via `pip install -e .` :

```bash
email-scraper -i urls.csv -o results.csv -t 4 -vv
```

### Options CLI

| Option | Description | Défaut |
|--------|-------------|--------|
| `-i, --input` | Fichier CSV d'entrée (requis) | - |
| `-o, --output` | Fichier CSV de sortie (requis) | - |
| `-t, --threads` | Nombre de threads parallèles | 1 |
| `--timeout` | Timeout par requête (secondes) | 15 |
| `--rate-limit` | Délai minimum entre requêtes (secondes) | 1.0 |
| `--cache` | Répertoire de cache | désactivé |
| `--json` | Export JSON additionnel | désactivé |
| `--webhook` | URL webhook de notification | désactivé |
| `--no-robots` | Ignorer robots.txt | False |
| `--dry-run` | Détecter les pages sans scraper | False |
| `--user-agent` | User-Agent personnalisé | défaut interne |
| `-v, -vv` | Verbosité (INFO, DEBUG) | WARNING |
| `--log-file` | Fichier de log | désactivé |

### Format CSV de sortie

```csv
url,email,source_page,confidence_score
https://example.fr,contact@example.fr,https://example.fr/contact,0.87
https://example.fr,info@example.fr,https://example.fr/mentions-legales,0.62
```

### Format JSON de sortie

```json
[
  {
    "url": "https://example.fr",
    "email": "contact@example.fr",
    "source_page": "https://example.fr/contact",
    "confidence_score": 0.87,
    "source_type": "mailto",
    "page_type": "contact"
  }
]
```

## Architecture

```
email_scraper/
├── __init__.py      # Version
├── __main__.py      # Point d'entrée python -m
├── cli.py           # Interface CLI, I/O, orchestration parallèle
├── scraper.py       # Logique de scraping, fetch, cache, robots.txt
├── extractor.py     # Extraction emails : regex, mailto, désobfuscation
├── detector.py      # Détection intelligente des pages contact/legal/about
└── scorer.py        # Score de confiance par email
```

## Score de confiance

Le score (0.0-1.0) est calculé à partir de 5 facteurs :

| Facteur | Poids | Détail |
|---------|-------|--------|
| Méthode source | 25% | `mailto` > obfusqué > regex |
| Domaine | 30% | Email sur le même domaine que le site |
| Type de page | 20% | Contact > mentions légales > about > homepage |
| Contexte sémantique | 15% | Mots-clés de contact à proximité |
| Qualité du local part | 10% | `contact@` > `prenom.nom@` > autre |
