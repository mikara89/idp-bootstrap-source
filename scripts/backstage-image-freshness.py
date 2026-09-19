#!/usr/bin/env python3
"""Decide whether the pinned Backstage image matches the authoritative source revision.

The GitLab image-supply pipeline builds ``backstage:<source revision>`` from the
repository Backstage build context and pins the digest that tag resolved to. That
means commissioning must answer a freshness question, not an availability
question:

    Does the pinned Backstage digest belong to the current authoritative
    Backstage source revision?

Registry existence of an older image proves availability, not freshness. A stale
pin whose image is still present in the Registry must still trigger a rebuild.

The authoritative source revision
---------------------------------

The source revision is the newest commit that changed the Backstage image *build
context*, excluding the runtime-only SwarmCD stack pin::

    environments/prod/platform/backstage              (build context)
    :(exclude)environments/prod/platform/backstage/stack.yml

``stack.yml`` is excluded because the ``update-backstage-digest`` job rewrites
only that file. Without the exclusion every digest-only GitOps commit would look
like a new source revision and commissioning would rebuild Backstage forever.

Freshness decision
------------------

A rebuild is required unless *both* halves hold:

* a Registry image exists for the source revision tag;
* that image's manifest digest equals the digest pinned in the authoritative
  SwarmCD stack.

Registry authentication
-----------------------

The bundled GitLab Container Registry does not accept Basic credentials on
manifest requests. It answers with ``401`` and a ``WWW-Authenticate: Bearer``
challenge naming its token realm, so this helper follows the same flow as the
Docker client: the challenge is parsed, the credentials are exchanged at the
realm for a short-lived Bearer token, and the manifest request is retried with
that token. A Basic challenge is honoured as well. Tokens, credentials,
authenticated URLs, and Authorization headers are never printed.

Fail-closed contract
--------------------

This helper never guesses. It exits ``1`` and prints a single
``backstage-freshness-failed=<category>`` token on stderr when it cannot prove
freshness: an unreadable or shallow repository, an undeterminable source
revision, a malformed or absent pin, an invalid Registry endpoint, an unreadable
authentication challenge, an unusable token response, Registry
authentication/network/API failure, or a source image whose digest cannot be
resolved. Only a ``404`` on the authenticated manifest request for the source
revision tag means "missing".
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: Backstage image build context. This is exactly the directory the image-supply
#: pipeline hands to Buildah, so every file that can change the produced image
#: lives under it.
SOURCE_CONTEXT = "environments/prod/platform/backstage"

#: Runtime-only SwarmCD stack holding the immutable image pin. Mutated by the
#: digest-update job and never part of the built image.
RUNTIME_STACK = "environments/prod/platform/backstage/stack.yml"

#: Canonical ``git log`` pathspec for the authoritative source revision.
SOURCE_PATHSPEC = (SOURCE_CONTEXT, f":(exclude){RUNTIME_STACK}")

#: Image name below the platform image prefix.
IMAGE_NAME = "backstage"

REVISION_RE = re.compile(r"^[a-f0-9]{40,64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_PREFIX_RE = re.compile(r"^[a-z0-9]+(?:(?:[._-]|/)[a-z0-9]+)*$")

#: ``http://`` Registry endpoints are refused unless the host is a loopback
#: address, so a misconfigured template cannot downgrade Registry credentials to
#: cleartext. Loopback exemptions keep local fixtures testable.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

ACCEPT_MANIFEST_TYPES = (
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
)

#: ``WWW-Authenticate`` challenge grammar (RFC 7235): an auth scheme followed by
#: comma-separated ``key=value`` parameters whose values are tokens or quoted
#: strings.
AUTH_SCHEME_RE = re.compile(
    r"^\s*(?P<scheme>[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]*)\s*(?P<parameters>.*)$",
    re.DOTALL,
)
AUTH_PARAM_RE = re.compile(
    r'(?P<key>[A-Za-z][A-Za-z0-9_-]*)\s*=\s*'
    r'(?:"(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<token>[^\s,]+))'
)

#: Token response fields, in preference order, and the largest token document this
#: helper accepts.
TOKEN_FIELDS = ("token", "access_token")
MAX_TOKEN_BYTES = 64 * 1024

#: Token printed instead of a digest when the source revision has no image.
ABSENT = "absent"

#: Every fail-closed category this helper can emit.
CATEGORIES = frozenset(
    {
        "credentials-missing",
        "pinned-digest-invalid",
        "registry-authentication-failed",
        "registry-challenge-invalid",
        "registry-endpoint-insecure",
        "registry-endpoint-invalid",
        "registry-token-invalid",
        "registry-unavailable",
        "source-image-digest-unresolved",
        "source-revision-unavailable",
    }
)

USERNAME_ENV = "BACKSTAGE_REGISTRY_USERNAME"
PASSWORD_ENV = "BACKSTAGE_REGISTRY_PASSWORD"


class FreshnessError(RuntimeError):
    """A fail-closed condition carrying a single non-secret category token."""

    def __init__(self, category: str) -> None:
        if category not in CATEGORIES:
            raise AssertionError(f"unknown freshness category: {category}")
        super().__init__(category)
        self.category = category


def source_revision(repository: Path, rev: str = "HEAD") -> str:
    """Return the authoritative Backstage source revision of ``repository``.

    The repository must carry full history: a shallow checkout cannot resolve the
    newest commit that changed the build context, and answering with the shallow
    tip would silently rebuild or skip the wrong source.
    """
    git = shutil.which("git")
    if not git:
        raise FreshnessError("source-revision-unavailable")
    if not repository.is_dir():
        raise FreshnessError("source-revision-unavailable")

    shallow = _run_git(git, repository, ["rev-parse", "--is-shallow-repository"])
    if shallow is None or shallow.strip() != "false":
        raise FreshnessError("source-revision-unavailable")

    revision = _run_git(
        git,
        repository,
        ["log", "-1", "--format=%H", rev, "--", *SOURCE_PATHSPEC],
    )
    if revision is None:
        raise FreshnessError("source-revision-unavailable")
    revision = revision.strip()
    if not REVISION_RE.fullmatch(revision):
        raise FreshnessError("source-revision-unavailable")
    return revision


def _run_git(git: str, repository: Path, arguments: list[str]) -> str | None:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            [git, "-C", str(repository), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def validate_pinned_digest(raw: str) -> str:
    """Return the authoritative stack pin, or fail closed."""
    if not isinstance(raw, str) or not DIGEST_RE.fullmatch(raw.strip()):
        raise FreshnessError("pinned-digest-invalid")
    return raw.strip()


def validate_image_prefix(raw: str) -> str:
    """Validate the platform image prefix used to build the Registry path."""
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        raise FreshnessError("registry-endpoint-invalid")
    if not IMAGE_PREFIX_RE.fullmatch(raw) or ".." in raw:
        raise FreshnessError("registry-endpoint-invalid")
    return raw


def normalize_registry_url(raw: str) -> str:
    """Return a credential-free Registry base URL, or fail closed."""
    return _normalize_endpoint(raw, allow_path=False)


def normalize_token_realm(raw: str) -> str:
    """Return a credential-free token realm URL, or fail closed.

    The realm arrives from the Registry challenge, so a malformed, downgraded, or
    credential-bearing realm is a challenge failure rather than an endpoint
    failure: ``http://`` is refused unless the host is loopback, which keeps
    Registry credentials off cleartext transports.
    """
    try:
        return _normalize_endpoint(raw, allow_path=True)
    except FreshnessError:
        raise FreshnessError("registry-challenge-invalid") from None


def _normalize_endpoint(raw: str, *, allow_path: bool) -> str:
    if not isinstance(raw, str) or not raw:
        raise FreshnessError("registry-endpoint-invalid")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise FreshnessError("registry-endpoint-invalid") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FreshnessError("registry-endpoint-invalid")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise FreshnessError("registry-endpoint-invalid")
    if not allow_path and parsed.path not in {"", "/"}:
        raise FreshnessError("registry-endpoint-invalid")
    if allow_path and not parsed.path.startswith("/"):
        raise FreshnessError("registry-endpoint-invalid")
    if port is not None and not 1 <= port <= 65535:
        raise FreshnessError("registry-endpoint-invalid")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS:
        raise FreshnessError("registry-endpoint-insecure")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    authority = f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "")
    if allow_path and parsed.path not in {"", "/"}:
        return authority + parsed.path
    return authority


def parse_authenticate_challenge(header: str) -> tuple[str, dict[str, str]]:
    """Return ``(scheme, parameters)`` for a Bearer or Basic challenge.

    GitLab's bundled Container Registry answers an unauthenticated manifest
    request with ``401`` and
    ``WWW-Authenticate: Bearer realm="https://gitlab.<domain>/jwt/auth",service="container_registry",scope="repository:<repo>:pull"``.
    The client exchanges its Registry credentials for a short-lived token at that
    realm and retries with ``Authorization: Bearer <token>``. Anything that cannot
    be read as a Bearer or Basic challenge fails closed, and the first value wins
    so a trailing second challenge cannot override the parsed parameters.
    """
    if not isinstance(header, str) or not header.strip():
        raise FreshnessError("registry-challenge-invalid")
    match = AUTH_SCHEME_RE.match(header)
    if not match:
        raise FreshnessError("registry-challenge-invalid")
    scheme = match.group("scheme").lower()
    if scheme not in {"bearer", "basic"}:
        raise FreshnessError("registry-challenge-invalid")
    parameters: dict[str, str] = {}
    for parameter in AUTH_PARAM_RE.finditer(match.group("parameters")):
        value = (
            parameter.group("quoted")
            if parameter.group("quoted") is not None
            else parameter.group("token")
        )
        parameters.setdefault(
            parameter.group("key").lower(), _unescape_quoted(value)
        )
    if scheme == "bearer" and not parameters.get("realm"):
        raise FreshnessError("registry-challenge-invalid")
    return scheme, parameters


def _unescape_quoted(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _repository_scope(scope: str, image_repository: str) -> str:
    """Return the pull scope to request, or fail closed.

    The Registry dictates the scope. It is echoed when it targets exactly this
    repository, which also keeps the requested scope from silently widening.
    """
    expected_suffix = f":{image_repository}:"
    if not scope:
        return f"repository:{image_repository}:pull"
    for entry in scope.split():
        if entry.startswith("repository") and expected_suffix in entry:
            return scope
    raise FreshnessError("registry-challenge-invalid")


def _manifest_request(
    url: str, authorization: str | None, timeout: float
) -> tuple[int, str | None, str | None]:
    """Return ``(status, Docker-Content-Digest, WWW-Authenticate)``.

    Transport failures are reported as Registry unavailability, so they can never
    be mistaken for a missing image.
    """
    headers = {"Accept": ", ".join(ACCEPT_MANIFEST_TYPES)}
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                int(response.status),
                response.headers.get("Docker-Content-Digest"),
                response.headers.get("WWW-Authenticate"),
            )
    except urllib.error.HTTPError as error:
        return (
            int(error.code),
            error.headers.get("Docker-Content-Digest") if error.headers else None,
            error.headers.get("WWW-Authenticate") if error.headers else None,
        )
    except urllib.error.URLError:
        raise FreshnessError("registry-unavailable") from None
    except (OSError, ValueError):
        raise FreshnessError("registry-unavailable") from None


def fetch_registry_token(
    realm: str,
    service: str,
    scope: str,
    username: str,
    password: str,
    timeout: float,
) -> str:
    """Exchange Registry credentials for a short-lived Bearer token.

    The token and the credentials behind it are never printed, and no usable token
    response is a fail-closed condition rather than a missing image.
    """
    parameters = {"scope": scope}
    if service:
        parameters["service"] = service
    request = urllib.request.Request(
        f"{realm}?{urllib.parse.urlencode(parameters)}",
        method="GET",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{username}:{password}".encode()).decode(),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if int(response.status) != 200:
                raise FreshnessError("registry-unavailable")
            payload = response.read(MAX_TOKEN_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise FreshnessError("registry-authentication-failed") from None
        raise FreshnessError("registry-unavailable") from None
    except urllib.error.URLError:
        raise FreshnessError("registry-unavailable") from None
    except (OSError, ValueError):
        raise FreshnessError("registry-unavailable") from None

    if len(payload) > MAX_TOKEN_BYTES:
        raise FreshnessError("registry-token-invalid")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FreshnessError("registry-token-invalid") from None
    if not isinstance(document, dict):
        raise FreshnessError("registry-token-invalid")
    for field in TOKEN_FIELDS:
        token = document.get(field)
        if isinstance(token, str) and token.strip():
            return token.strip()
    raise FreshnessError("registry-token-invalid")


def probe_source_image(
    registry_url: str,
    image_repository: str,
    tag: str,
    username: str,
    password: str,
    timeout: float,
) -> str | None:
    """Return the source revision image digest, or ``None`` when it is missing.

    Authentication follows the Registry v2 flow the Docker client uses, because
    the bundled GitLab Container Registry does not accept Basic credentials on
    manifest requests: the ``401`` ``WWW-Authenticate`` challenge selects the
    token realm, the credentials are exchanged for a short-lived Bearer token, and
    the manifest request is retried with that token. A Basic challenge is honoured
    too, for a Registry that authenticates manifest requests directly.

    Only a ``404`` on the authenticated manifest request means the source revision
    has no image. Every other failure - challenge, token, authentication, network,
    TLS, or an unexpected status - fails closed instead of being reinterpreted as
    "missing".
    """
    url = f"{registry_url}/v2/{image_repository}/manifests/{tag}"
    status, digest, challenge = _manifest_request(url, None, timeout)

    if status == 401:
        scheme, parameters = parse_authenticate_challenge(challenge)
        if scheme == "bearer":
            realm = normalize_token_realm(parameters.get("realm", ""))
            scope = _repository_scope(parameters.get("scope", ""), image_repository)
            token = fetch_registry_token(
                realm,
                parameters.get("service", ""),
                scope,
                username,
                password,
                timeout,
            )
            authorization = f"Bearer {token}"
        else:
            authorization = "Basic " + base64.b64encode(
                f"{username}:{password}".encode()
            ).decode()
        status, digest, _ = _manifest_request(url, authorization, timeout)

    if status == 404:
        return None
    if status == 200:
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest.strip()):
            raise FreshnessError("source-image-digest-unresolved")
        return digest.strip()
    if status in {401, 403}:
        raise FreshnessError("registry-authentication-failed")
    raise FreshnessError("registry-unavailable")


def freshness_required(source_digest: str | None, pinned_digest: str) -> bool:
    """Return whether a rebuild is required.

    Skipping requires both a source image for the current source revision and an
    exact digest match with the authoritative stack pin.
    """
    validate_pinned_digest(pinned_digest)
    if source_digest is None:
        return True
    if not DIGEST_RE.fullmatch(source_digest):
        raise FreshnessError("source-image-digest-unresolved")
    return source_digest != pinned_digest


def evaluate(
    *,
    repository: Path,
    rev: str,
    registry_url: str,
    image_prefix: str,
    pinned_digest: str,
    username: str,
    password: str,
    timeout: float,
) -> dict[str, str]:
    """Return the freshness payload, or raise :class:`FreshnessError`."""
    if not username or not password:
        raise FreshnessError("credentials-missing")
    pin = validate_pinned_digest(pinned_digest)
    endpoint = normalize_registry_url(registry_url)
    prefix = validate_image_prefix(image_prefix)
    revision = source_revision(repository, rev)
    source_digest = probe_source_image(
        endpoint,
        f"{prefix}/{IMAGE_NAME}",
        revision,
        username,
        password,
        timeout,
    )
    return {
        "source_revision": revision,
        "pinned_digest": pin,
        "source_image_digest": source_digest if source_digest else ABSENT,
        "required": "true" if freshness_required(source_digest, pin) else "false",
    }


def render_payload(payload: dict[str, str]) -> str:
    return "\n".join(
        [
            f"backstage-source-revision={payload['source_revision']}",
            f"backstage-pinned-digest={payload['pinned_digest']}",
            f"backstage-source-image-digest={payload['source_image_digest']}",
            f"backstage-required={payload['required']}",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="checkout with full history of the authoritative GitLab branch",
    )
    parser.add_argument("--rev", default="HEAD", help="revision to inspect")
    parser.add_argument(
        "--registry-url",
        required=True,
        help="Registry base URL, for example https://registry.example.internal",
    )
    parser.add_argument(
        "--image-prefix",
        required=True,
        help="platform image prefix, for example platform/idp-docker-stack",
    )
    parser.add_argument(
        "--pinned-digest",
        required=True,
        help="authoritative backstage@sha256:<64 hex> digest from the SwarmCD stack",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Registry request timeout in seconds",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = evaluate(
            repository=args.repository,
            rev=args.rev,
            registry_url=args.registry_url,
            image_prefix=args.image_prefix,
            pinned_digest=args.pinned_digest,
            username=os.environ.get(USERNAME_ENV, ""),
            password=os.environ.get(PASSWORD_ENV, ""),
            timeout=args.timeout,
        )
    except FreshnessError as error:
        print(f"backstage-freshness-failed={error.category}", file=sys.stderr)
        return 1
    print(render_payload(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
