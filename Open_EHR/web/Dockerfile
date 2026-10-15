# OpenCare web client.
# Build: Vite/React/TypeScript on Node 24 (Alpine).
# Serve: nginx (open source) via the unprivileged image — it runs as a
# non-root user and listens on 8080. Chosen over a Node static server so the
# production image carries no Node runtime.
FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# VITE_* values are baked in at build time; override with --build-arg.
ARG VITE_API_BASE=http://localhost:8000
ARG VITE_AUTH_MODE=dev
ENV VITE_API_BASE=$VITE_API_BASE VITE_AUTH_MODE=$VITE_AUTH_MODE
RUN npm run build

FROM nginxinc/nginx-unprivileged:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
