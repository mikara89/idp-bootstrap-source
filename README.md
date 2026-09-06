# idp-bootstrap-source

This repository is a generated bootstrap snapshot.

- Source of truth is the private `mikara89/idp-docker-stack` repository.
- This public repository exists only to bootstrap self-hosted GitLab.
- It is installation media, not a second source of truth, and must not become a Git submodule.
- It must contain no credentials.
- First commissioning clones this snapshot anonymously from `main` and seeds GitLab `platform/idp-docker-stack` on `sovereign-idp-vertical-slices` only when that GitLab branch does not already exist.
- After GitLab is seeded, GitLab becomes authoritative.
- SwarmCD, Backstage, and CI use GitLab only. This public repository is never the runtime SwarmCD repository.
- Direct changes to this public repository may be overwritten by the next generated snapshot.
