# WebFolder Mirror (HTTP directory listing → local folder)

Ce script permet de **télécharger récursivement** tout le contenu (dossier + sous-dossiers) d’un répertoire exposé en HTTP (type “index of /…”) et de **synchroniser** ensuite le dossier local avec le serveur (téléchargement des fichiers manquants ou modifiés).

## Prérequis

- Python **3.9+**, je recommande d'utiliser un environnement virtuel (`venv`).
- Accès réseau vers l’URL cible

## Installation

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
# .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

## Configuration

Éditer en haut du fichier `webFolderMirror.py` :

- `TARGET_URL` : l’URL du dossier distant à répliquer 
- `DEST_DIR` : le dossier local de destination

## Utilisation

Lancer :

```bash
python webFolderMirror.py
```

Le script :
1. explore `TARGET_URL` et tous ses sous-dossiers (via le listing HTML),
2. compare avec le contenu déjà présent dans `DEST_DIR`,
3. télécharge :
   - les fichiers manquants,
   - les fichiers modifiés (détection via `ETag` / `Last-Modified` / `Content-Length` si disponibles).

Un fichier d’état est créé/maintenu dans :
- `DEST_DIR/.webFolderMirror_state.tsv`

## Options

Dans `webFolderMirror.py`, il est possible de configurer :

- `DRY_RUN = True` : ne télécharge rien, affiche seulement ce qui serait téléchargé
- `VERIFY_TLS` : validation TLS (laisser `True` sauf cas particulier)
- `TIMEOUT_SECS` : timeout réseau
- `USER_AGENT` : user-agent HTTP

## Limitations / remarques

- Le serveur doit exposer un **listing HTML** navigable (autoindex).  
- Le script ne supprime pas (pour l’instant) les fichiers locaux qui n’existent plus sur le serveur.

## Licence
MIT License