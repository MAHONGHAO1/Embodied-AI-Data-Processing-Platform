# syntax=docker/dockerfile:1.7
# Production Web image — pnpm workspace (root lockfile), vinext start.
FROM node:22.22.0-bookworm-slim AS build
WORKDIR /opt/quictrain
RUN corepack enable && corepack prepare pnpm@11.7.0 --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
RUN pnpm --dir apps/web install --frozen-lockfile
COPY apps/web apps/web
ARG NEXT_PUBLIC_API_BASE_URL=
ARG NEXT_PUBLIC_ALLOW_DEMO_MODE=false
ARG NEXT_PUBLIC_AUTH_TOKEN=
ARG NEXT_PUBLIC_QUIC_PROJECT=
ARG NEXT_PUBLIC_QUIC_ACTOR=
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}
ENV NEXT_PUBLIC_ALLOW_DEMO_MODE=${NEXT_PUBLIC_ALLOW_DEMO_MODE}
ENV NEXT_PUBLIC_AUTH_TOKEN=${NEXT_PUBLIC_AUTH_TOKEN}
ENV NEXT_PUBLIC_QUIC_PROJECT=${NEXT_PUBLIC_QUIC_PROJECT}
ENV NEXT_PUBLIC_QUIC_ACTOR=${NEXT_PUBLIC_QUIC_ACTOR}
RUN pnpm --dir apps/web build

FROM node:22.22.0-bookworm-slim AS runtime
ENV NODE_ENV=production
WORKDIR /opt/quictrain
RUN corepack enable && corepack prepare pnpm@11.7.0 --activate
# pnpm links workspace packages via the monorepo root; keep the build tree intact.
COPY --from=build /opt/quictrain ./
WORKDIR /opt/quictrain/apps/web
EXPOSE 3000
CMD ["pnpm", "run", "start"]
