# idp-bootstrap-source

This repository is a generated bootstrap snapshot and credential-free bootstrap distribution.

- The private `mikara89/idp-docker-stack` repository is the development source of truth.
- This public repository exists to distribute verified bootstrap snapshots and bootstrap self-hosted GitLab.
- It is installation media, not a development or runtime repository, and must not become a Git submodule.
- It must contain no credentials.
- Commissioning clones `main` anonymously, waits for the expected snapshot fingerprint, and promotes it into GitLab `platform/idp-docker-stack` on `sovereign-idp-vertical-slices`.
- GitLab becomes authoritative for runtime GitOps after initial promotion and remains authoritative; newer verified public snapshots may be consumed by a later commissioning run.
- Promotion uses conflict-detecting three-way merge semantics, preserves GitLab-only work, and never force-rewrites the target branch.
- SwarmCD, Backstage, and CI use GitLab only. This public repository is never the runtime SwarmCD repository.
- Direct edits to this generated public repository are unsupported and may be overwritten by the next generated snapshot.
