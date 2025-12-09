# Justitia & Associates - Law Firm Case Management System

A modern, full-stack case management system for law firms with AI-powered chat assistance.

![Dashboard](./docs/images/Dashboard.png)

More screenshots can be found in the [docs/images](./docs/images) directory.

## 🏗️ Architecture

- **Frontend**: Next.js 15 with TypeScript, Tailwind CSS
- **Backend**: FastAPI (Python 3.12)
- **Database**: PostgreSQL
- **Deployment**: HPE Private Cloud AI (HPE PCAI) "Import Framework" compatible (auth-proxy integration not implemented)
- **Development**: Kubernetes with Tilt for local development

## ✨ Features

### Case Management
- Create and manage legal cases
- Track case details (defendant, type, status, lead attorney)
- Document upload and management
- Evidence logging with categorization
- Document viewer with download and print capabilities

### AI-Powered Chat
- LLM integration for case analysis
- Multi-model support (OpenAI compatible API)
- Automatic context building from case data
- Readable document content inclusion
- Video Q&A support
- Automated "dramatis personae" generation
- Debug panel for transparency

### Admin Panel
- Database browser with pagination
- LLM configuration management
- Record detail viewer

## 🚀 Quick Start

### Prerequisites
- HPE Private Cloud AI (HPE PCAI) admin access

### Development Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd lawfirm-co
   ```

2. **Start the development environment**
   ```bash
   tilt up
   ```

3. **Access the applications**
   - Frontend: http://localhost:3000
   - Backend API: http://localhost:8000
   - API Docs: http://localhost:8000/docs

### Environment Variables

Preferred method is to configure during HPE PCAI "Import Framework" setup.

Create `.env` files in both frontend and backend directories:

**Backend (.env)**
```env
DATABASE_URL=postgresql://user:password@postgres:5432/lawfirm
```

**Frontend (.env.local)**
```env
BACKEND_URL=http://localhost:8000
NEXT_PUBLIC_API_URL=http://localhost:8000
```

## 📦 Production Deployment

### Building Production Images

**Backend:**
```bash
cd backend
docker build -f Dockerfile.prod -t lawfirm-backend:latest .
```

**Frontend:**
```bash
cd frontend
docker build -f Dockerfile.prod -t lawfirm-frontend:latest .
```

## 🛠️ Development

### Backend Development

```bash
cd backend
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### Frontend Development

```bash
cd frontend
npm install
npm run dev
```

## 📚 API Documentation

Once the backend is running, visit:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

### Key Endpoints

- `GET /cases` - List all cases
- `POST /cases` - Create a new case
- `GET /cases/{id}` - Get case details
- `POST /cases/{id}/documents` - Upload document
- `POST /cases/{id}/evidence` - Add evidence
- `POST /chat/cases/{id}` - Chat with AI about a case
- `GET /admin/tables` - List database tables
- `GET /health` - Health check

## 🔒 Security

- Non-root user in Docker containers
- Environment variable management for secrets
- API key masking in UI
- Input validation and sanitization
- CORS configuration

## 🧪 Testing

**Backend:**
```bash
cd backend
pytest
```

**Frontend:**
```bash
cd frontend
npm test
```

## 📊 Monitoring

Health check endpoints are available:
- Backend: `GET /health`
- Frontend: `GET /api/health`

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## 📝 License

Copyright © 2025 Justitia & Associates

## 🆘 Support

For issues and questions, please open an issue on GitHub.

## 🎯 TODO

- [x] Advanced search and filtering
- [x] RAG from shared documents
- [x] Video Q & A