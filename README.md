# VPN Web (vShop) - Multi-Tenant Proxy Billing Stack

A Django-based billing and proxy management system built to handle subscription management, transaction tracking, and asynchronous task processing for VPN services.

## Features

- **Multi-tenant Architecture**: Support for multiple users and proxy configurations
- **Billing Engine**: Automated billing and subscription management
- **REST API**: Django REST Framework for programmatic access
- **Async Task Processing**: Celery + Redis for background job execution
- **Periodic Scheduling**: Celery Beat for scheduled billing cycles and maintenance
- **Redis Cache**: High-performance caching and message broker
- **Docker Support**: Full containerization with Docker Compose
- **Production Ready**: Gunicorn WSGI server with proper scaling

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Web Framework | Django 5.0+ |
| Task Queue | Celery 5.3+ |
| Message Broker & Cache | Redis 5.0+ |
| Web Server | Gunicorn 22.0+ |
| REST API | Django REST Framework 3.15+ |
| Image Processing | Pillow 10.0+ |
| Database | SQLite (dev) / PostgreSQL (prod) |
| Container Orchestration | Docker Compose 3.9+ |

## Project Structure

```
vpn_web/
├── vShop/                  # Main Django application
│   ├── settings.py         # Django configuration
│   └── wsgi.py            # WSGI entry point
├── billing_engine/         # Billing logic and models
├── manage.py              # Django management CLI
├── docker-compose.yml     # Multi-service container setup
├── requirements.txt       # Python dependencies
└── README.md             # This file
```

## Quick Start

### Prerequisites

- Python 3.8+
- Docker & Docker Compose (optional, but recommended)
- Redis (included in Docker setup)

### Option 1: Docker Compose (Recommended)

```bash
# Clone the repository
git clone https://github.com/mostafa-html/vpn_web.git
cd vpn_web

# Create a .env file with required settings
cp .env.example .env

# Start all services (web, Redis, Celery, Celery Beat)
docker-compose up -d

# Run migrations
docker-compose exec web python manage.py migrate

# Create a superuser
docker-compose exec web python manage.py createsuperuser

# Access the application
# Web: http://localhost:8000
# Admin: http://localhost:8000/admin
```

### Option 2: Local Development

```bash
# Clone the repository
git clone https://github.com/mostafa-html/vpn_web.git
cd vpn_web

# Create a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create a .env file
cp .env.example .env

# Run migrations
python manage.py migrate

# Create a superuser
python manage.py createsuperuser

# Start the development server
python manage.py runserver

# In another terminal, start Celery worker
celery -A vShop worker --loglevel=info

# In another terminal, start Celery Beat
celery -A vShop beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler

# Access the application at http://localhost:8000
```

## Configuration

### Environment Variables

Create a `.env` file in the project root with the following variables:

```bash
# Django Settings
SECRET_KEY=your-secret-key-here
DEBUG=False
ALLOWED_HOSTS=localhost,127.0.0.1,yourdomain.com

# Database (default: SQLite)
# For PostgreSQL: psycopg2-binary must be installed
# DATABASE_URL=postgresql://user:password@localhost:5432/vpn_web

# Redis
REDIS_URL=redis://redis:6379/0

# Celery
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0
```

## Services Overview

The Docker Compose stack includes:

### 1. **Redis** (Cache & Message Broker)
- Alpine Linux image for minimal footprint
- AOF persistence enabled
- Used by Celery for task queuing
- Used by Django for session/cache storage

### 2. **Web** (Gunicorn WSGI Application)
- Runs Django application
- 3 worker processes for handling concurrent requests
- 120-second timeout for long-running operations
- Serves static/media files through configured storage

### 3. **Celery Worker** (Background Task Processing)
- 4 concurrent processes
- Fair scheduling for task distribution
- Processes billing, notifications, and scheduled jobs

### 4. **Celery Beat** (Task Scheduler)
- Database-backed scheduler for reliability
- Manages periodic billing cycles
- Handles cleanup and maintenance tasks

## Development

### Running Tests

```bash
# Run all tests with coverage
coverage run --source='.' manage.py test
coverage report

# Run specific test module
python manage.py test billing_engine.tests
```

### Database Migrations

```bash
# Create migrations for model changes
python manage.py makemigrations

# Apply pending migrations
python manage.py migrate

# Show migration status
python manage.py showmigrations
```

### Creating an Admin User

```bash
python manage.py createsuperuser
```

Access the admin panel at `/admin/` after logging in.

## API Documentation

The REST API endpoints are available at:
- Base URL: `http://localhost:8000/api/`
- API Schema/Browsable API documentation available through DRF interface

## Production Deployment

### Key Considerations

1. **Secret Key**: Generate a new secret key and store it securely
2. **Database**: Switch from SQLite to PostgreSQL for production
3. **Allowed Hosts**: Update `ALLOWED_HOSTS` with your domain
4. **Static Files**: Configure proper static file serving (S3, CDN, etc.)
5. **Media Files**: Use persistent storage for uploaded files
6. **SSL/TLS**: Use HTTPS with proper certificates
7. **Logging**: Configure proper logging to files or external services
8. **Monitoring**: Set up monitoring for Celery and application health

### Scaling

- Increase `--workers` in Gunicorn config
- Increase `--concurrency` in Celery Worker config
- Use load balancing (Nginx, HAProxy) for multiple web instances
- Consider using Kubernetes for container orchestration

## Troubleshooting

### Issue: Celery tasks not running
```bash
# Check Redis connectivity
redis-cli ping  # Should return PONG

# Restart Celery services
docker-compose restart celery_worker celery_beat
```

### Issue: Database locked (SQLite)
```bash
# Switch to PostgreSQL (recommended for production)
# Update .env with DATABASE_URL and install psycopg2-binary
```

### Issue: Media files not persisting
```bash
# Ensure the protected_media volume is properly mounted
docker volume ls | grep protected_media
docker volume inspect vpn_web_protected_media
```

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is currently unlicensed. Please contact the author for licensing information.

## Author

[mostafa-html](https://github.com/mostafa-html)

## Support

For issues, questions, or suggestions, please [open an issue](https://github.com/mostafa-html/vpn_web/issues) on GitHub.
