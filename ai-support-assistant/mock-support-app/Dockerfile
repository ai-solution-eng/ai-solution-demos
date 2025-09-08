# ---- Stage 1: Build frontend ----
FROM node:20 AS frontend-builder

WORKDIR /app/frontend

# Copy frontend files
COPY frontend/package*.json ./
RUN npm install

COPY frontend/ ./
RUN npm run build   # outputs dist/

# ---- Stage 2: Build backend + copy frontend dist ----
FROM node:20

WORKDIR /app/backend

# Copy backend package.json and install deps
COPY backend/package*.json ./
RUN npm install --production

# Copy backend source code
COPY backend/ ./

# Copy built frontend from stage 1
COPY --from=frontend-builder /app/frontend/dist ../frontend/dist

# Expose backend port
EXPOSE 80

# Start the backend server
CMD ["node", "server.js"]