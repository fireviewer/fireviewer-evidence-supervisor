# fireviewer-evidence-supervisor

Evidence assessment, contradictions and accept/reject/abstain decisions.

Python package: `fireviewer_evidence_supervisor`. Version: `0.1.0`.

## Installation

Install the versioned release wheels (including private FireViewer dependencies) from the release bundle. No sibling source checkout is required.

```sh
python -m pip install --find-links /path/to/release/wheels fireviewer-evidence-supervisor==0.1.0
python -m pytest tests -q
```

Optional model/provider environments are separate extras and retain their existing upstream constraints. Model weights, credentials, datasets and local evidence are external inputs.

## Ownership and compatibility

Target account: `fireviewer`. Target stewardship: Association FIRE-VIEWER. Historical authorship and AGPL-3.0-or-later notices are retained. This technical extraction is not a signed assignment of rights.

Source correspondence and hashes are recorded in the migration dossier. Existing schema IDs, algorithm revisions and evidence/publication gates are preserved. The former `firewarning_worker` or backend module paths are compatibility adapters in their original repository.

## Delivery boundary

Docker, image ownership, registries and production deployment are deferred by the project owner. CPU/schema tests do not qualify GPU, visual or scientific performance.

## Sources et commandes propres au composant

Commande Python : `fireviewer-supervisor`. Eve se construit séparément dans `apps/eve` avec `npm ci --ignore-scripts` puis `npm run build -- --skip-sandbox-prewarm`. Une compilation n’invoque pas le modèle. La validation humaine, les seuils et l’abstention ne sont pas modifiés.

Les dépendances de base sont verrouillées avec hashes dans `requirements.lock.txt` (Python 3.13). Installer les wheels privés du même bundle via `--find-links`. Les extras lourds restent liés à leurs versions existantes et ne qualifient aucun GPU. Les tests de composant et leurs dépendances de test sont recensés dans le dossier unique de migration.
