FROM nginx:1.27-alpine

# openssl CLI is needed by docker-entrypoint-wrapper.sh to generate a
# throwaway self-signed cert on first boot (nginx:alpine links libssl but
# doesn't ship the standalone `openssl` binary).
RUN apk add --no-cache openssl

COPY docker-entrypoint-wrapper.sh /docker-entrypoint-wrapper.sh
RUN chmod +x /docker-entrypoint-wrapper.sh

ENTRYPOINT ["/docker-entrypoint-wrapper.sh"]
