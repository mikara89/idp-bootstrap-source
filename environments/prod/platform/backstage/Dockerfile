FROM node:24-trixie-slim AS packages

WORKDIR /app

COPY backstage.json package.json yarn.lock ./
COPY .yarn ./.yarn
COPY .yarnrc.yml ./
COPY packages packages
COPY plugins plugins

RUN find packages \! -name package.json -mindepth 2 -maxdepth 2 -exec rm -rf {} +
RUN find plugins \! -name package.json -mindepth 2 -maxdepth 2 -exec rm -rf {} +

FROM node:24-trixie-slim AS build

ENV PYTHON=/usr/bin/python3
ENV COREPACK_HOME=/home/node/.cache/node/corepack

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends python3 g++ build-essential libsqlite3-dev && \
    rm -rf /var/lib/apt/lists/*

RUN corepack enable
RUN mkdir -p /home/node/.cache/node/corepack && chown -R node:node /home/node/.cache

USER node
WORKDIR /app

COPY --from=packages --chown=node:node /app ./

RUN --mount=type=cache,target=/home/node/.cache/yarn,sharing=locked,uid=1000,gid=1000 \
    yarn install --immutable

COPY --chown=node:node . .

RUN yarn tsc
RUN yarn build:backend

RUN mkdir packages/backend/dist/skeleton packages/backend/dist/bundle \
    && tar xzf packages/backend/dist/skeleton.tar.gz -C packages/backend/dist/skeleton \
    && tar xzf packages/backend/dist/bundle.tar.gz -C packages/backend/dist/bundle

FROM node:24-trixie-slim

LABEL org.opencontainers.title="IDP Backstage"
LABEL org.opencontainers.description="Repo-owned Backstage image for the IDP platform deployment"
LABEL org.opencontainers.source="https://github.com/example/idp-docker-stack"

ENV PYTHON=/usr/bin/python3
ENV COREPACK_HOME=/home/node/.cache/node/corepack

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends python3 g++ build-essential libsqlite3-dev && \
    rm -rf /var/lib/apt/lists/*

RUN corepack enable
RUN mkdir -p /home/node/.cache/node/corepack && chown -R node:node /home/node/.cache

USER node
WORKDIR /app

COPY --from=build --chown=node:node /app/.yarn ./.yarn
COPY --from=build --chown=node:node /app/.yarnrc.yml ./
COPY --from=build --chown=node:node /app/backstage.json ./
COPY --from=build --chown=node:node /app/yarn.lock /app/package.json /app/packages/backend/dist/skeleton/ ./

RUN --mount=type=cache,target=/home/node/.cache/yarn,sharing=locked,uid=1000,gid=1000 \
    yarn workspaces focus --all --production && rm -rf "$(yarn cache clean)"

COPY --from=build --chown=node:node /app/packages/backend/dist/bundle/ ./
COPY --chown=node:node app-config*.yaml ./
COPY --chown=node:node examples ./examples

ENV NODE_ENV=production
ENV NODE_OPTIONS="--no-node-snapshot"

CMD ["node", "packages/backend", "--config", "app-config.yaml", "--config", "app-config.production.yaml"]
