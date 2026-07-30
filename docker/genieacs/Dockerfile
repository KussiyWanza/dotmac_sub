FROM node:20-bookworm-slim

RUN npm install -g genieacs@1.2.13 \
    && useradd --system --no-create-home --user-group genieacs \
    && mkdir -p /opt/genieacs/ext /var/log/genieacs \
    && chown -R genieacs:genieacs /opt/genieacs /var/log/genieacs

# Build context is this directory (docker/genieacs), not the repo root — the
# image needs nothing from the application tree, and a repo-root context made
# every app commit invalidate this image's build cache.
COPY entrypoint.sh /usr/local/bin/genieacs-entrypoint

RUN chmod +x /usr/local/bin/genieacs-entrypoint

USER genieacs
WORKDIR /opt/genieacs

ENV GENIEACS_EXT_DIR=/opt/genieacs/ext

ENTRYPOINT ["/usr/local/bin/genieacs-entrypoint"]
