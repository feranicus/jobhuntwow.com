# Frontend is prebuilt (dist/). We only serve it — no node/npm/esbuild in the image,
# so the build can't fail. (For CI later, switch back to a node:20-slim build stage.)
FROM nginx:alpine
COPY dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
