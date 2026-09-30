FROM node:20-alpine

WORKDIR /app

# Copy dependency files
COPY package.json package-lock.json ./
RUN npm ci

# Copy application code
COPY . .

# Set dynamic build/run environment
# Note: we include everything so that development (npm run dev)
# and production (npm run build && npm start) both work.
RUN npm run build

EXPOSE 3000

ENV PORT 3000
ENV HOSTNAME "0.0.0.0"

# Default behavior: run development if ENV is set to development
# otherwise run production server
CMD ["sh", "-c", "if [ \"$ENV\" = \"development\" ]; then npm run dev; else npm run start; fi"]
