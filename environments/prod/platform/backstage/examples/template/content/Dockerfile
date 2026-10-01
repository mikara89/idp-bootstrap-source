FROM node:24-alpine
WORKDIR /app
ARG APP_PORT=3000
ENV PORT=$APP_PORT
COPY package.json package-lock.json index.js ./
RUN addgroup -S app && adduser -S app -G app && chown -R app:app /app
USER app
EXPOSE $APP_PORT
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s CMD node -e "const p=process.env.HEALTH_PATH||'/healthz'; fetch('http://127.0.0.1:'+(process.env.PORT||3000)+p).then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "index.js"]
