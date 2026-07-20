# Stagehand AI sidecar. No browser download needed — it attaches to the existing Chrome over CDP.
FROM node:22-slim
WORKDIR /svc
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund
COPY server.ts ./
ENV PORT=8081
EXPOSE 8081
CMD ["node","--experimental-strip-types","server.ts"]
