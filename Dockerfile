FROM oven/bun:1-slim AS builder

WORKDIR /src/corner

COPY package.json bun.lock ./
RUN bun install --frozen-lockfile

COPY . .

ARG NUXT_PUBLIC_LIFE_API_URL=/api/life
ARG NUXT_PUBLIC_CONTENT_HOST=
ARG LIFE_PREVIEW_POSTS_URL=

ENV NUXT_PUBLIC_LIFE_API_URL=${NUXT_PUBLIC_LIFE_API_URL} \
    NUXT_PUBLIC_CONTENT_HOST=${NUXT_PUBLIC_CONTENT_HOST} \
    LIFE_PREVIEW_POSTS_URL=${LIFE_PREVIEW_POSTS_URL}

RUN bun run build

FROM nginx:1.27-alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /src/corner/.output/public /usr/share/nginx/html

EXPOSE 80
