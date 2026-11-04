FROM --platform=linux/amd64 node:24-slim

RUN groupadd -g 10001 app && useradd -u 10001 -g app -m app
WORKDIR /app
RUN chown app:app /app
# Everything below runs as the app user, which owns the code so that Tilt's live update works
USER 10001

# Install dependencies exactly as locked
COPY --chown=10001:10001 app-ui/package*.json ./
RUN npm ci

# Copy application code
COPY --chown=10001:10001 app-ui/ .
COPY --chown=10001:10001 USAGE.md DIAGRAM.md ./

ENV NEXT_TELEMETRY_DISABLED=1

# Build the application for production. The larger heap is for the build only: at run time it
# would let Node grow past the container's memory limit.
RUN NODE_OPTIONS="--max-old-space-size=4096" npm run build

EXPOSE 3000

# Run dev if ENV=development, otherwise run production
CMD if [ "$ENV" = "development" ]; then \
      npm run dev; \
    else \
      npm start; \
    fi
