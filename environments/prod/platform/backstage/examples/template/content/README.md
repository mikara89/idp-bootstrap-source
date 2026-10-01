# ${{ values.name }}

A Node.js service created by the internal developer platform.

## Develop and publish

Run locally with `npm start`; run tests with `npm test`; build with
`buildah bud -t ${{ values.name }} .`. The service honors `PORT`, `HEALTH_PATH`,
and `READINESS_PATH`; the defaults are 3000, `/healthz`, and `/readyz`.

The pipeline validates, tests, builds an immutable Registry digest, then requests
GitOps promotion only from the default branch. Record the pipeline's source
commit and `image.digest` artifact when requesting deployment verification.
Promotion success records a GitOps update; it does not prove Swarm deployment.
The service repository holds no host or GitOps write credentials.

Use the catalog's CI link for pipeline failures and its monitoring link for
logs and dashboards. Include the service name, source revision, image digest,
pipeline URL, and failure time when asking a platform maintainer for help.

## Runtime settings and secrets

The service contract supports `small` and `medium` resource profiles and
non-secret environment settings. Ask a platform maintainer to review changes
to the accepted definition at `environments/prod/apps/definitions/${{ values.name }}.yaml`
in the platform runtime repository. Subsequent image promotions preserve those
approved settings.

Never commit secret values or put them in scaffolder inputs or promotion
variables. Operators provision approved service-specific secret references
through Ansible and mount them under `/run/secrets/<reference>`. Rotate by
provisioning a new versioned reference, updating the approved definition, and
retiring the old secret only after no tasks use it.

`internal` services have no developer-facing route or platform readiness gate.
`authenticated-web` services use Traefik readiness checks and the platform login.
Both retain the image's Docker health check. Rolling updates are sequential and
pause on a failed update.

## Operator deployment verification

A platform maintainer runs this from the platform **source checkout** on an
operator host with existing Docker manager and node SSH access. The verifier is
not supplied in this service repository. No developer host credentials are needed.

```sh
python scripts/verify-app-deployment.py ${{ values.name }} \
  <expected-source-commit> sha256:<expected-image-digest> \
  --timeout 600 --poll-interval 10 --ssh-user developer
```

The verifier checks desired configuration, running task digests, Docker health,
and each task's readiness using a temporary overlay probe. Save its JSON evidence
with the release record. Success is exit 0; failure is 1; timeout is 2; a
superseded deployment is 3. It does not update services or push Git changes.

## Failed release and recovery

Provide the failed pipeline and verifier evidence to a platform maintainer.
The maintainer restores the previous accepted image digest and source revision
in the service definition on the configured platform GitOps branch, runs
`scripts/render-apps-stack.py --registry <trusted-registry>` from that checkout,
and reviews and commits the definition, aggregate stack, and metrics discovery
changes. SwarmCD reconciles that Git change; do not patch the running service.
Run the verifier again for the restored revision and digest before declaring
recovery complete.

## Offboard

Ask a platform maintainer to remove the service definition, regenerate the
aggregate stack and metrics discovery, and commit the reviewed result to the
configured platform GitOps branch. Confirm the service has stopped and its
route and metrics target are gone before retiring unused secrets. Remove the
catalog registration and archive the service repository according to the team's
data-retention decision.
