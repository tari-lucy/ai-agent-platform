FROM alpine:3.24.3@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a
RUN apk add --no-cache openssh-client \
    && addgroup -S -g 65532 tunnel \
    && adduser -S -u 65532 -G tunnel -h /run/tunnel -s /sbin/nologin tunnel
COPY --chmod=755 entrypoint.sh /entrypoint.sh
USER 65532:65532
ENV HOME=/run/tunnel
EXPOSE 1080
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ssh -S /run/tunnel/control -O check unused >/dev/null 2>&1 && nc -z 127.0.0.1 1080
ENTRYPOINT ["/entrypoint.sh"]
