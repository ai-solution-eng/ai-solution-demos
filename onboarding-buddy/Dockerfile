# ---------- Build Stage ----------
FROM node:22.16.0-alpine AS builder
WORKDIR /app

# Copy package files and install all dependencies (including dev dependencies needed for proxy)
COPY package*.json ./
RUN npm ci --legacy-peer-deps --force

# Copy the rest of the source
COPY . .

# Build the application
RUN npm run build

# ---------- Production Stage ----------
FROM nginx:alpine

# Install Node.js for the proxy
RUN apk update && apk add --no-cache --update nodejs=22.15.1-r0

# Copy built assets from builder
COPY --from=builder /app/dist /usr/share/nginx/html
COPY --from=builder /app/node_modules /app/node_modules
COPY --from=builder /app/scripts /app/scripts
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy and set up entrypoint
COPY --from=builder /app/scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create necessary directories
RUN mkdir -p /run/nginx /var/log/nginx /var/cache/nginx /var/run/nginx /usr/share/nginx/html/config

# Clean up
RUN rm -rf /var/cache/apk/* /tmp/* /var/tmp/* /root/.npm /root/.node-gyp


# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost/health || exit 1

EXPOSE 80

# Set working directory
WORKDIR /app

ENTRYPOINT ["/entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]