import express from 'express';
import { createProxyMiddleware } from 'http-proxy-middleware';
import http from 'http';
import fs from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';
import httpModule from 'http';
import httpsModule from 'https';
import cors from 'cors';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();

// Add request logging middleware
app.use((req, res, next) => {
  const start = Date.now();
  console.log(`[${new Date().toISOString()}] ${req.method} ${req.url}`);
  
  res.on('finish', () => {
    const duration = Date.now() - start;
    console.log(`[${new Date().toISOString()}] ${req.method} ${req.url} - ${res.statusCode} (${duration}ms)`);
  });
  
  next();
});

// Enable CORS for all routes
app.use(cors());

app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true }));

// Health check endpoint
app.get('/health', (req, res) => {
  res.status(200).json({
    status: 'ok',
    timestamp: new Date().toISOString(),
    llmEndpointConfigured: !!currentLLMEndpoint,
    llmEndpoint: currentLLMEndpoint || 'Not configured'
  });
});

// Initialize with endpoint from environment or empty - will be set by the frontend
let currentLLMEndpoint = '';

// Create proxy middleware with dynamic target
const createLLMProxy = () => {
  try {
    if (!currentLLMEndpoint) {
      throw new Error('No LLM endpoint configured');
    }
    
    const targetUrl = normalizeEndpointUrl(currentLLMEndpoint);
    console.log(`[${new Date().toISOString()}] Creating proxy for target: ${targetUrl}`);
    
    // Create agent based on protocol
    const isHttps = targetUrl.startsWith('https');
    const agent = isHttps 
      ? new httpsModule.Agent({
          keepAlive: true,
          timeout: 300000,
          keepAliveMsecs: 300000,
          rejectUnauthorized: false // Only for development, consider proper certs in production
        })
      : new http.Agent({
          keepAlive: true,
          timeout: 300000,
          keepAliveMsecs: 300000
        });

    return createProxyMiddleware({
      target: targetUrl,
      changeOrigin: true,
      timeout: 300000, // 5 minute timeout (300,000ms)
      proxyTimeout: 300000, // 5 minute proxy timeout (300,000ms)
      xfwd: true, // Add x-forwarded headers
      ws: true, // Proxy WebSockets
      logLevel: 'debug',
      secure: false, // Disable SSL verification
      agent, // Use the appropriate agent based on protocol
      pathRewrite: {
        '^/api/chat': '/v1/chat/completions', // Map /api/chat to /v1/chat/completions
        '^/api/llm-endpoint': ''
      },
      onError: (err, req, res) => {
        console.error(`[${new Date().toISOString()}] Proxy error:`, err);
        if (!res.headersSent) {
          res.status(500).json({
            success: false,
            error: 'Proxy error',
            details: err.message,
            currentEndpoint: currentLLMEndpoint,
            targetUrl: targetUrl,
            timestamp: new Date().toISOString()
          });
        }
      },
      onProxyReq: (proxyReq, req) => {
        console.log(`[${new Date().toISOString()}] Proxying request to: ${proxyReq.getHeader('host')}${proxyReq.path}`);
        console.log('Request Headers:', JSON.stringify(proxyReq.getHeaders(), null, 2));
        
        // Add any required headers here
        proxyReq.setHeader('x-forwarded-proto', 'https');
        
        // If we have a body, log it and ensure proper headers
        if (req.body) {
          console.log('Request Body:', JSON.stringify(req.body, null, 2));
          if (!proxyReq.getHeader('Content-Type')) {
            proxyReq.setHeader('Content-Type', 'application/json');
          }
          if (req.body && !Buffer.isBuffer(req.body) && typeof req.body === 'object') {
            const bodyData = JSON.stringify(req.body);
            proxyReq.setHeader('Content-Length', Buffer.byteLength(bodyData));
            proxyReq.write(bodyData);
          }
        }
      },
      onProxyRes: (proxyRes, req, res) => {
        console.log(`[${new Date().toISOString()}] Received response with status: ${proxyRes.statusCode}`);
        console.log('Response Headers:', JSON.stringify(proxyRes.headers, null, 2));
        
        // Forward headers
        Object.keys(proxyRes.headers).forEach(key => {
          res.setHeader(key, proxyRes.headers[key]);
        });
      }
    });
  } catch (error) {
    console.error(`[${new Date().toISOString()}] Error creating proxy:`, error);
    throw error; // Re-throw to be handled by the route handler
  }
};

// Normalize the endpoint URL if it exists
if (currentLLMEndpoint) {
  try {
    currentLLMEndpoint = normalizeEndpointUrl(currentLLMEndpoint);
    console.log(`[${new Date().toISOString()}] Initialized with endpoint from environment: ${currentLLMEndpoint}`);
  } catch (error) {
    console.error(`[${new Date().toISOString()}] Invalid DEFAULT_LLM_ENDPOINT:`, error.message);
    currentLLMEndpoint = '';
  }
} else {
  console.log(`[${new Date().toISOString()}] No initial endpoint configured - waiting for frontend configuration`);
}

// Health check endpoint
app.get('/api/health', (req, res) => {
  res.json({
    status: 'ok',
    timestamp: new Date().toISOString(),
    proxy: 'running',
    llmEndpointConfigured: !!currentLLMEndpoint,
    llmEndpoint: currentLLMEndpoint || 'Not configured'
  });
});

// Endpoint to configure the LLM endpoint
app.post('/api/configure', express.json(), (req, res) => {
  const { endpoint } = req.body;
  
  if (!endpoint) {
    console.log(`[${new Date().toISOString()}] [ERROR] No endpoint provided in request`);
    return res.status(400).json({ 
      success: false,
      error: 'Endpoint is required',
      timestamp: new Date().toISOString()
    });
  }
  
  console.log(`[${new Date().toISOString()}] Received request to update LLM endpoint to:`, endpoint);
  
  try {
    // Validate the URL
    const url = new URL(endpoint);
    
    // Ensure the URL has a protocol
    if (!url.protocol || !url.hostname) {
      throw new Error('Invalid URL format. Must include protocol (http:// or https://) and hostname');
    }
    
    // Update the current endpoint
    currentLLMEndpoint = endpoint;
    
    // Remove existing proxy middleware
    app._router.stack = app._router.stack.filter(
      layer => !layer?.handle?.name?.includes('handle')
    );
    
    // Re-add the routes with the new proxy
    app.use('/api/chat', createLLMProxy());
    app.use('/api/llm-endpoint', createLLMProxy());
    
    console.log(`[${new Date().toISOString()}] Successfully updated LLM endpoint to: ${currentLLMEndpoint}`);
    
    res.json({ 
      success: true, 
      message: 'LLM endpoint configured successfully',
      endpoint: currentLLMEndpoint,
      normalizedUrl: normalizeEndpointUrl(currentLLMEndpoint),
      timestamp: new Date().toISOString()
    });
  } catch (error) {
    console.error(`[${new Date().toISOString()}] Error configuring LLM endpoint:`, error);
    res.status(400).json({ 
      success: false, 
      error: 'Invalid endpoint URL',
      details: error.message,
      timestamp: new Date().toISOString()
    });
  }
});

// Alias for backward compatibility
app.post('/api/llm-endpoint', express.json(), (req, res) => {
  console.log(`[${new Date().toISOString()}] Using deprecated /api/llm-endpoint endpoint, please use /api/configure instead`);
  req.url = '/api/configure';
  app.handle(req, res);
});

// Endpoint to get the current LLM endpoint
app.get('/api/llm-endpoint', (req, res) => {
  res.json({ endpoint: currentLLMEndpoint });
});

// Helper function to normalize the endpoint URL
const normalizeEndpointUrl = (url) => {
  try {
    const parsed = new URL(url);
    // Remove any trailing slashes and /v1/chat/completions if present
    parsed.pathname = parsed.pathname
      .replace(/\/+$/, '')  // Remove trailing slashes
      .replace(/\/v1\/chat\/completions$/, '')  // Remove /v1/chat/completions if present
      .replace(/\/v1$/, '');  // Remove /v1 if present
    
    // Add just the base path
    parsed.pathname = parsed.pathname || '/';
    return parsed.toString();
  } catch (e) {
    console.error('Invalid URL:', url, e);
    return 'http://localhost:11434';
  }
};

// Global error handler
app.use((err, req, res, next) => {
  console.error('Global error handler:', err);
  if (!res.headersSent) {
    res.status(500).json({
      error: 'Internal Server Error',
      message: err.message || 'An unexpected error occurred',
      timestamp: new Date().toISOString()
    });
  }
});

// Apply the proxy middleware to /api/chat
app.use('/api/chat', (req, res, next) => {
  const requestId = Date.now();
  const startTime = Date.now();
  
  console.log(`[${new Date().toISOString()}] [${requestId}] New request to /api/chat`);
  console.log(`[${new Date().toISOString()}] [${requestId}] Headers:`, JSON.stringify(req.headers, null, 2));
  
  // Log request body if present
  if (req.body && Object.keys(req.body).length > 0) {
    console.log(`[${new Date().toISOString()}] [${requestId}] Body:`, JSON.stringify(req.body, null, 2));
  }
  
  // Create a new proxy instance for this request
  const proxy = createLLMProxy();
  
  // Handle the request with the proxy
  proxy(req, res, next);
});

// Apply the proxy middleware to /api/llm-endpoint
app.use('/api/llm-endpoint', (req, res, next) => {
  const requestId = Date.now();
  
  console.log(`[${new Date().toISOString()}] [${requestId}] New request to /api/llm-endpoint`);
  console.log(`[${new Date().toISOString()}] [${requestId}] Headers:`, JSON.stringify(req.headers, null, 2));
  
  // Log request body if present
  if (req.body && Object.keys(req.body).length > 0) {
    console.log(`[${new Date().toISOString()}] [${requestId}] Body:`, JSON.stringify(req.body, null, 2));
  }
  
  // Create a new proxy instance for this request
  const proxy = createLLMProxy();
  
  // Handle the request with the proxy
  proxy(req, res, next);
});

// Start the server
const startServer = () => {
  const PORT = 3001;
  const server = app.listen(PORT, '0.0.0.0', () => {
    console.log(`[${new Date().toISOString()}] ==========================================`);
    console.log(`[${new Date().toISOString()}] Proxy server started successfully`);
    console.log(`[${new Date().toISOString()}] Listening on port: 3001`);
    console.log(`[${new Date().toISOString()}] Current LLM endpoint: ${currentLLMEndpoint || 'Not configured'}`);
    console.log(`[${new Date().toISOString()}] ==========================================`);
  });

  // Handle server errors
  server.on('error', (error) => {
    if (error.syscall !== 'listen') {
      throw error;
    }

    switch (error.code) {
      case 'EACCES':
        console.error(`[${new Date().toISOString()}] Port 3001 requires elevated privileges`);
        process.exit(1);
        break;
      case 'EADDRINUSE':
        console.error(`[${new Date().toISOString()}] Port 3001 is already in use`);
        process.exit(1);
        break;
      default:
        throw error;
    }
  });

  // Handle process termination
  process.on('SIGTERM', () => {
    console.log(`[${new Date().toISOString()}] SIGTERM received, shutting down gracefully`);
    server.close(() => {
      console.log(`[${new Date().toISOString()}] Server closed`);
      process.exit(0);
    });

    // Force close after 10 seconds
    setTimeout(() => {
      console.error(`[${new Date().toISOString()}] Forcing server close`);
      process.exit(1);
    }, 10000);
  });

  return server;
};

// Start the server if this file is run directly
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  startServer();
}

// Handle process termination
process.on('SIGINT', () => {
  const timestamp = new Date().toISOString();
  console.log(`\n[${timestamp}] Shutting down proxy server...`);
  console.log(`[${timestamp}] Current LLM endpoint: ${currentLLMEndpoint || 'Not configured'}`);
  console.log(`[${timestamp}] Gracefully shutting down...`);
  process.exit(0);
});

// Handle uncaught exceptions
process.on('uncaughtException', (error) => {
  console.error(`[${new Date().toISOString()}] Uncaught Exception:`, error);
  // Don't exit if the error is from the proxy
  if (!error.message.includes('ECONNREFUSED')) {
    process.exit(1);
  }
});

// Handle 404 for other routes
app.use((req, res) => {
  res.status(404).json({ 
    error: 'Not Found',
    message: `The requested resource ${req.url} was not found`,
    timestamp: new Date().toISOString()
  });
});

// Handle unhandled promise rejections
process.on('unhandledRejection', (reason, promise) => {
  console.error(`[${new Date().toISOString()}] Unhandled Rejection at:`, promise, 'reason:', reason);
  // Don't exit if the error is from the proxy
  if (!reason.message?.includes('ECONNREFUSED')) {
    process.exit(1);
  }
});
