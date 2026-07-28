## v0.10.6 (2026-07-21)

### Fix

- **jobs**: abort jobs left mid-flight by a restart instead of leaving them "in progress"

## v0.10.5 (2026-07-21)

### Fix

- **registry**: repair stale registry entries instead of refusing uploads

## v0.10.4 (2026-07-21)

### Fix

- changed secret forDVD_ADMIN_PASSWORD
- **deploy**: pass the admin password as DVD_ADMIN_PASSWORD

## v0.10.3 (2026-07-21)

### Fix

- added IDU_DVD_PASSWORD for admin panel in build_and_deploy.yml

## v0.10.2 (2026-07-21)

### Fix

- **config**: accept a scheme in DVD_MINIO_ENDPOINT

## v0.10.1 (2026-07-21)

### Fix

- added vars and secrets to build_and_deploy.yml

## v0.10.0 (2026-07-13)

### Feat

- add weighted document upload progress (#32)

### Fix

- **user-index**: emit DocumentDeleted events on user index deletion (#31)

## v0.9.0 (2026-07-10)

### Feat

- (#27) (#28)
- (#27)

## v0.8.0 (2026-07-08)

### Feat

- **pipeline**: auto-detect vector size and merge structure/tagging LLM passes (#24)
- - script

## v0.7.0 (2026-07-08)

### Feat

- **progress**: per-phase request counters and a single progress bar (#22)
- **vectorizer**: - add giga embeddings provider (#18)
- **dvd_service**: - add GET /tags endpoint and document_names search filter
- **autofill**: - updated autofill
- **autofill**: - added autofill actions
- **release**: - added release action
- **release**: - added release action
- **dvd_service**: (#3)
- **toml**: - updated dependencies
- (#1)

### Fix

- **release**: - fixed release action on master
- **release**: - fixed release action

### Refactor

- **scripts**: drop hardcoded base url from upload script

## v0.6.0 (2026-07-07)

### Feat

- **vectorizer**: - add giga embeddings provider (#18)

## v0.5.0 (2026-07-03)

### Feat

- **dvd_service**: - add GET /tags endpoint and document_names search filter

## v0.4.2 (2026-06-30)

### Fix

- **release**: - fixed release action on master

## v0.4.1 (2026-06-30)

### Fix

- **release**: - fixed release action

## v0.4.0 (2026-06-28)

### Feat

- **dvd_service**: (#3)
- **toml**: - updated dependencies
- (#1)
- **release.yml**: - added version bump pipeline
- - updated tests.yml - updated ignore
- **v0.1.0a1**: - added first alpha version
- **v0.1.0a1**: - added first alpha version
