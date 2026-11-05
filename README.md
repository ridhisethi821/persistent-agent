# Persistent Agent

A cloud-deployed AI agent application with persistent, session-isolated
workspaces. Upload documents, chat with an agent that can use Bash and
Python to work with files, and return later to continue where you left
off.

## Features

-   Independent conversations and workspaces
-   Persistent files and conversation context
-   PDF and DOCX upload
-   File analysis and creation using Bash and Python
-   Excel workbook creation and editing
-   File upload, listing, and download
-   Streaming assistant responses and tool activity

## Tech Stack

-   **Frontend:** React, TypeScript
-   **Backend:** Python, FastAPI
-   **Agent:** Claude Agent SDK (`claude-haiku-5-5`)
-   **Cloud:** Microsoft Azure
-   **Storage:** Azure Blob Storage and Azure Table Storage
-   **Execution:** Azure Container Apps Sandboxes

## Architecture

The React frontend communicates with a FastAPI backend over HTTP and
Server-Sent Events (SSE). The backend manages sessions, coordinates
isolated agent execution, and stores workspace files and conversation
data in Azure Storage. Execution environments can be stopped and
restored while persistent data remains available.

## Run Locally

### Backend

``` bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend

``` bash
cd frontend
npm install
npm run dev
```

Configure the required environment variables for your chosen storage,
execution, and model-provider setup before starting the backend. Keep
credentials in an untracked `.env` file or a secret manager.

## Deployment

Deployed on Azure Container Apps.

-   **Application:**
    https://pa-api-4sxahptpoltze.agreeablepond-02cec781.eastus.azurecontainerapps.io/
-   **Health check:**
    https://pa-api-4sxahptpoltze.agreeablepond-02cec781.eastus.azurecontainerapps.io/health

Reviewer access uses HTTP Basic Authentication. Username: `admin`. The
password is provided separately.

## Tests

Run from the project root:

``` bash
PYTHONPATH=backend pytest -v
```

Capacity reservation unit tests cover session limits, memory limits,
reservation release, fencing-token validation, and concurrent
reservations.
