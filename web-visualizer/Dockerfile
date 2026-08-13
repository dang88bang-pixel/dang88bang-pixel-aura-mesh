FROM node:20-alpine

WORKDIR /app

COPY package*.json ./
RUN npm ci --omit=dev || npm install --omit=dev

COPY server.js ./
COPY src/ ./src/
COPY public/ ./public/

RUN addgroup -g 10001 aura && adduser -D -u 10001 -G aura aura
USER aura

ENV PORT=3000 HOST=0.0.0.0 NODE_ENV=production
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
