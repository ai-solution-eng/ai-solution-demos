FROM --platform=linux/amd64 node:24-slim

WORKDIR /app

# Install dependencies
COPY app-ui/package*.json ./
RUN npm install

# Copy application code
COPY app-ui/ .
COPY USAGE.md DIAGRAM.md ./

# Build argument for domain
ARG DOMAIN
ENV DOMAIN=${DOMAIN}

# Environment variables for stability and Next.js
ENV NEXT_TELEMETRY_DISABLED=1
ENV NODE_OPTIONS="--max-old-space-size=4096"

# Build the application for production
RUN npm run build

EXPOSE 3000

# Run dev if ENV=development, otherwise run production
CMD if [ "$ENV" = "development" ]; then \
      npm run dev; \
    else \
      npm start; \
    fi

