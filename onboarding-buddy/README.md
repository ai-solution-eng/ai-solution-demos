# Onboarding Buddy AI Assistant

## Overview & Purpose

**Onboarding Buddy** is an AI-powered application designed to help organizations streamline onboarding for new hires. It empowers hiring managers and admins to create, assign, and manage onboarding tasks, while providing new employees with an embedded AI Q&A Assistant to guide them through their journey. The platform is demo-oriented: all data except the AI configuration is in-memory, making it easy to reset for demos or testing.

## ✨ Features
- 🚀 AI-powered onboarding tasks generation
- 🔒 Secure API key management with Kubernetes Secrets
- 🔄 Two-layer proxy architecture for secure API communication
- 🛡️ Built-in CORS and security headers
- 💬 Natural language Q&A AI Assistant
- 📊 New hire onboarding progress tracking
- 🎨 Modern, responsive UI built with shadcn/ui
- ☁️ Cloud-native design with Helm charts for easy deployment

## 📚 What You Can Do in the Application

### All Users
- Edit your avatar in "My Profile" by uploading a new image.

### Admin Users
- Access the Admin Dashboard: view all users, tasks, role distribution, and recent activity.
- Create/edit/delete/reset users of all roles (Admins, Managers, Mentors, New Hires).
- Create and manage all task templates.
- Access the Onboarding Tasks page: view/search/filter all Personalized Tasks, view task details, and create new Personalized Tasks using the AI-powered "Generate Tasks" feature.
- Impersonate Mentor and New Hire users to view their dashboards/progress.
- Change AI Configuration settings (LLM endpoint, model, API key) stored in localStorage.

### Manager Users
- Access the Manager Dashboard: see reporting New Hires/Mentors, task stats, progress, and quick actions.
- Create/edit/delete/reset New Hire and Mentor users reporting to them.
- Create/manage their own task templates; use (but not edit/delete) admin templates.
- Access the Onboarding Tasks page: view/search/filter all Personalized Tasks, create new ones via AI-powered generation.
- Review Personalized Tasks completed by their New Hires (score 1-10, review notes), volunteer to review, start/continue reviews.
- Impersonate Mentor and New Hire users reporting to them.

### Mentor Users
- Access the Mentor Dashboard: review stats, available tasks to review, assignments (move from assigned to in-progress), and continue reviews.
- Access the Onboarding Tasks page: view/search/filter all Personalized Tasks.
- Review Personalized Tasks completed by New Hires under the same manager (score 1-10, review notes), volunteer to review, start/continue reviews.

### New Hire Users
- Access the New Hire Dashboard: view task stats and onboarding schedule (timeline of assigned tasks).
- Access the Onboarding Tasks page: view/search/filter all Personalized Tasks, start/complete tasks, add completion notes.
- From the Personalized Task details page: start task, add completion notes, mark complete, and use the embedded Q&A AI Onboarding Assistant for task-specific help.

---

## 🔄 Proxy Architecture & CORS Handling

The application implements a sophisticated proxy architecture to handle CORS (Cross-Origin Resource Sharing) challenges when communicating with external LLM services. This architecture ensures secure and flexible API communication while maintaining compatibility with various deployment environments.

### Why CORS is a Challenge
**CORS** (Cross-Origin Resource Sharing) is a browser security feature that restricts web applications from making requests to a different origin (domain/protocol/port) than the one that served the web page. Even when running on the same PCAI cluster and domain, browser-enforced CORS policies can block requests to LLM endpoints.

### Architecture Overview

1. **Frontend Layer (Vite Dev Server / Nginx)**
   - Serves the React application to end users
   - In development, Vite's built-in proxy handles API requests
   - In production, Nginx serves static files and proxies API requests

2. **API Proxy Layer**
   - **Development**: Vite proxies requests from `/api` to a local Node.js proxy server (port 3001)
   - **Production**: Nginx serves static files and routes API requests to the Node.js middleware service
   - Handles CORS headers and request/response transformation

### Implementation Details

#### Development Mode (Vite Proxy)
- The Vite dev server proxies `/api` requests to an LLM endpoint defined by the `VITE_PROXY_URL` environment variable
- The proxy rewrites the `Origin` header to match the target LLM's origin, making requests appear same-origin
- Configured in `vite.config.ts`

#### Production Mode (Nginx + Node.js Middleware)
- Nginx serves static files and proxies `/api` requests to the Node.js middleware
- The Node.js middleware (`scripts/proxy.mjs`) handles:
  - Dynamic extraction of target LLM hostname/path
  - Header rewriting for CORS compliance
  - Request/response transformation
  - Preflight `OPTIONS` request handling
  - API key authentication
  - Request logging

### Why Node.js Middleware is Essential
- Acts as a server-side proxy to avoid browser CORS restrictions
- Enables dynamic LLM endpoint configuration at runtime
- Provides request/response transformation between frontend and LLM APIs
- Handles authentication and security headers
- Implements rate limiting and request validation
- Provides detailed logging for debugging

### Frontend Integration
- The `callLLM` utility in `src/utils/aiUtils.ts` constructs the correct proxy URL based on environment
- Automatically detects development vs. production mode
- Handles both direct and proxied API calls seamlessly

---

## 🖥️ Installation Methods

### 🏠 Local Development

1. Clone the repository:
   ```sh
   git clone https://github.com/your-org/onboarding-buddy.git
   cd onboarding-buddy
   ```

2. Install dependencies:
   ```sh
   npm install
   ```

3. Configure environment:
   - Copy `.env.example` to `.env`
   - Configure the proxy URL (only needed for development):
     ```env
     # Development Proxy Configuration
     VITE_PROXY_URL=http://localhost:3001
     ```

4. Start the development servers:
   ```sh
   # Start the frontend
   npm run dev
   
   # In a separate terminal, start the proxy server
   node scripts/proxy.mjs
   ```

5. Access the application at [http://localhost:8080](http://localhost:8080)

### 🏗️ Building for Production

To build and preview the application in a production-like environment:

1. Build the production assets:
   ```sh
   npm run build
   ```
   This creates an optimized production build in the `dist` directory.

2. Preview the production build locally:
   ```sh
   npm run preview
   ```
   This serves the built application on [http://localhost:4173](http://localhost:4173) by default.

3. (Optional) To test with the proxy in production mode:
   - First, build the application as shown above
   - Then run the proxy server in a separate terminal:
     ```sh
     node scripts/proxy.mjs
     ```
   - Access the application at [http://localhost:4173](http://localhost:4173)

   The production build uses the same proxy configuration but serves the optimized static files instead of running the development server.

Note: The production build is what gets deployed to Docker and Kubernetes environments.

### 🐳 Docker Deployment

#### Quick Start with Pre-built Docker Image (Recommended)
If you want to try the application immediately, use the public image:
```sh
docker run -p 8080:80 claudiodeterminedai/onboarding-buddy:v0.0.7-alpha
```
Then access [http://localhost:8080](http://localhost:8080).

#### Build and Run Your Own Image
1. Clone the repository:
   ```sh
   git clone https://github.com/your-org/onboarding-buddy.git
   cd onboarding-buddy
   ```

2. Install dependencies:
   ```sh
   npm install
   ```

3. (Optional) Build the frontend manually (advanced):
   ```sh
   npm run build        # Production build
   npm run build:dev    # Development build
   ```

4. Build the Docker image:
   ```sh
   docker build -t your-registry/onboarding-buddy:<tag> .
   ```

5. Push to container registry (optional):
   ```sh
   docker push your-registry/onboarding-buddy:<tag>
   ```

6. Run the container:
   ```sh
   docker run -p 8080:80 your-registry/onboarding-buddy:<tag>
   ```

7. Access the application at [http://localhost:8080](http://localhost:8080)

### ☸️ HPE PCAI Deployment

The application is designed to be deployed on HPE's Private Cloud AI (PCAI) using AIE's Import Framework feature. The provided Helm chart simplifies the deployment process and includes all necessary Kubernetes resources.

1. **(Optional) Create a Kubernetes secret** containing your API key:
   For enhanced security, you can store your API key in a Kubernetes secret instead of including it directly in your values file. Here's how to create and use a secret:

   ```sh
   # Create a generic secret with your API key
   kubectl create secret generic onboarding-buddy-secrets \
     --from-literal=apiKey='your-actual-api-key-here' \
     -n <target-namespace>
   ```
   
   Replace:
   - `onboarding-buddy-secrets` with your desired secret name (must match the name in values.yaml)
   - `apiKey` with the key name that matches your configuration
   - `<target-namespace>` with the namespace where you'll deploy the application

   Verify the secret was created:
   ```sh
   kubectl get secret onboarding-buddy-secrets -n <target-namespace> -o yaml
   ```

   **Security Best Practices**
   - Never commit API keys to version control
   - Use Kubernetes RBAC to restrict access to secrets
   - Consider using a secret management solution like HashiCorp Vault for production environments
   - Rotate API keys regularly and update the corresponding secrets

2. **Prepare your configuration**:
   Create a `values.yaml` file with your configuration:
   ```yaml
   # values.yaml
   
   # Image configuration
   image:
     repository: claudiodeterminedai/onboarding-buddy
     tag: v0.0.7-alpha
     pullPolicy: IfNotPresent
   
   # Replica count for high availability
   replicaCount: 1
   
   # Resource requests and limits
   resources:
     requests:
       cpu: "100m"
       memory: "256Mi"
     limits:
       cpu: "500m"
       memory: "1Gi"
   
   # AI Configuration
   ai:
     # AI endpoint URL (e.g., https://your-llm-endpoint.com)
     endpoint: "https://your-llm-endpoint.com"
     # AI model to use
     model: "Meta/Llama-3.1-8B-Instruct"
     # Additional AI configuration parameters
     config:
       temperature: 0.7
       maxTokens: 2000
     # API Key configuration
     apiKey:
      # Option 1: Directly specify the API key (not recommended for production)
      # value: "your-api-key-here"
     
      # Option 2: Reference a Kubernetes secret (recommended)
      secret:
        name: "onboarding-buddy-secrets"  # Name of the Kubernetes secret
        key: "apiKey"  # Key within the secret that contains the API key
   
   # Istio Virtual Service Configuration
   ezua:
     virtualService:
       endpoint: "onboarding-buddy.${DOMAIN_NAME}"
       istioGateway: "istio-system/ezaf-gateway"
   ```

3. **Package the Helm chart**:
   ```sh
   helm package charts/onboarding-buddy
   ```

4. **Import into PCAI using AIE**:
   - Log in to your HPE PCAI environment
   - Navigate to AI Essentials (AIE)
   - Select "Import Framework"
   - Fill in the framework details:
     - Framework Name: A short name for the framework
     - Description: Brief description
     - Category: Select appropriate category (e.g., "Data Science")
     - Framework Icon: Upload an icon (recommended)
     - Helm Chart: Upload the packaged chart (.tar.gz)
     - Namespace: Target namespace for deployment
     - (Optional) Release Name: Custom release name
     - (Optional) Framework Values: Paste your `values.yaml` content or modify values directly
   - Click "Import" to deploy

5. **Deploy your application** using the Helm chart as described in the previous section. The application will automatically use the API key from the secret.

6. **Access the application**
   - Once deployed, access the application at the configured hostname (e.g., `https://onboarding-buddy.your-domain.com`)
   - Navigate to Admin > AI Configuration to verify or update LLM settings

### MLIS Integration

To use PCAI's Machine Learning Inference Service (MLIS):
1. Deploy your preferred LLM model through MLIS
2. Use the provided MLIS endpoint and API key in the application's AI Configuration
3. The application will automatically handle the integration with the MLIS-hosted model

### Managing Deployments

#### Updating an Existing Secret
To update an existing secret with a new API key:
```sh
kubectl create secret generic onboarding-buddy-secrets \
  --from-literal=apiKey='your-new-api-key' \
  -n <target-namespace> \
  --dry-run=client -o yaml | kubectl apply -f -
```

#### Updating the Application

To update the application code:
1. Build and push a new container image with an updated tag
2. In AIE, find your application and click the menu (⋮)
3. Select "Configure"
4. Update the `image.tag` value to the new version
5. Click "Configure" to apply changes

#### Upgrading the Helm Chart

To deploy a new version of the Helm chart:
1. Make your changes to the Helm chart
2. Update the chart version in `Chart.yaml`
3. Package the updated chart:
   ```sh
   helm package charts/onboarding-buddy
   ```
4. In AIE, find your application and click the menu (⋮)
5. Select "Update"
6. Upload the new chart package
7. Review and apply the changes

#### Uninstalling the Application

To completely remove the application and its resources:
1. In AIE, find your application and click the menu (⋮)
2. Select "Delete"
3. Confirm the deletion

> **Note:** This will remove all application resources but will not delete any persistent volumes or secrets. To completely clean up, manually delete any remaining resources in the namespace.

### Helm Chart Reference

The Helm chart includes the following configurable parameters:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `replicaCount` | Number of pod replicas | `1` |
| `image.repository` | Container image repository | `claudiodeterminedai/onboarding-buddy` |
| `image.tag` | Container image tag | `v0.0.7-alpha` |
| `image.pullPolicy` | Image pull policy | `IfNotPresent` |
| `resources.requests.cpu` | CPU request | `100m` |
| `resources.requests.memory` | Memory request | `256Mi` |
| `resources.limits.cpu` | CPU limit | `500m` |
| `resources.limits.memory` | Memory limit | `1Gi` |
| `ai.endpoint` | LLM API endpoint | `https://your-llm-endpoint.com` |
| `ai.model` | LLM model name | `Meta/Llama-3.1-8B-Instruct` |
| `ai.config.temperature` | Sampling temperature | `0.7` |
| `ai.config.maxTokens` | Maximum tokens to generate | `2000` |
| `ai.apiKey.secret.name` | Secret containing API key | `onboarding-buddy-secrets` |
| `ai.apiKey.secret.key` | Key in secret for API key | `apiKey` |
| `ezua.virtualService.endpoint` | Virtual service hostname | `onboarding-buddy.${DOMAIN_NAME}` |
| `ezua.virtualService.istioGateway` | Istio gateway reference | `istio-system/ezaf-gateway` |

#### Autoscaling (Optional)

| Parameter | Description | Default |
|-----------|-------------|---------|
| `autoscaling.enabled` | Enable HPA | `false` |
| `autoscaling.minReplicas` | Minimum replicas | `2` |
| `autoscaling.maxReplicas` | Maximum replicas | `5` |
| `autoscaling.targetCPUUtilizationPercentage` | CPU target for scaling | `80` |
| `autoscaling.targetMemoryUtilizationPercentage` | Memory target for scaling | `80` |

### Security Notes
- API keys should never be committed to version control
- For production deployments, always use Kubernetes Secrets to manage sensitive information
- The application implements proper CORS and security headers to protect against common web vulnerabilities

---

## 🙏 Acknowledgments

- [HPE](https://www.hpe.com/us/en.html) for the Private Cloud AI platform
- [Claudio Calderon](mailto:jose-claudio.calderon@hpe.com) for the Onboarding Buddy AI application.

## 📄 Documentation

For more detailed documentation, please refer to:

- [New Hire Onboarding Assistant](https://hpe-aiatscale.atlassian.net/wiki/spaces/CS/pages/2238808065/New+Hire+Onboarding+Assistant) Confluence page.

## 🤝 Contributing

This is just a PoC / demo, so no contributions are expected.

## 📊 Project Status

This is just a PoC / demo, so no further development is expected.
