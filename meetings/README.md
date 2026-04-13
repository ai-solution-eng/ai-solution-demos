# Agentic Meetings

Using HPE Private Cloud AI, you can use Agentic AI workflows to build your organisation's core needs, all secure, on your own private cloud. This is one example of how you can use it to simulate company meetings with AI agents.

| Owner                 | Name              | Email                              |
| ----------------------|-------------------|------------------------------------|
| Use Case Owner        | Erdinc Kaya       | kaya@hpe.com                       |
| PCAI Deployment Owner | Erdinc Kaya       | kaya@hpe.com                       |

![Demo](demo.mp4)

## Overview
Agentic Meetings is an advanced application that simulates company meetings using agentic AI workflows. By defining scenarios, objectives, and attendee profiles, the application orchestrates AI agents to engage in collaborative discussions, solve problems, and generate comprehensive meeting outcomes.

## Application Structure
The platform is organised into two primary components:

*   **Frontend**: A modern web interface built with **Next.js, React, Tailwind CSS, Zustand, and React Query**. It provides a rich UI for creating meeting scenarios (such as the `SAMPLE_SCENARIO.md`), configuring agents, and tracking the live multi-agent simulation.
*   **Backend**: A high-performance REST API powered by **FastAPI** and **Python**. It serves as the brain for the agentic workflows, featuring:
    *   **Orchestration (`app/orchestration`)**: Utilises **LangGraph** and **LangChain** to implement a supervisor-agent architecture. A central supervisor coordinates tasks among specialised worker agents.
    *   **Services (`app/services`)**: Implements Retrieval-Augmented Generation (RAG) by managing document processing, embeddings, and vector similarity search (via **pgvector**) to ensure agents have accurate, context-aware business knowledge.

## How It Is Used
1.  **Define a Scenario**: Users create a meeting brief with an agenda, expectations, and desired outcomes (e.g. closing fiscal year reviews, strategic planning).
2.  **Provide Context**: Relevant documents and histories are processed and embedded into the vector database.
3.  **Simulate**: The backend orchestration provisions the necessary AI attendees. The supervisor agent guides the flow of the meeting based on the agenda, calling upon specialised agents to contribute, debate, and draw conclusions.
4.  **Review Outcomes**: The frontend tracks the live "conversation", providing users with a comprehensive meeting transcript, actionable insights, and summaries.

## Capabilities & HPE PCAI Platform Integration
Agentic Meetings leverages the **HPE Private Cloud AI (PCAI)** platform to unlock enterprise-grade, resilient agentic workflows:
*   **LLM & Model Serving**: Designed to interface natively with models hosted within PCAI's secure boundary for both inference and embedding generation.
*   **Agentic Orchestration**: Showcases advanced AI patterns beyond simple chat. It utilises stateful graphs, checkpointing (PostgreSQL), and complex multi-agent reasoning, fully supported by the PCAI compute environment.
*   **RAG & Vector Search**: Harnesses high-performance PostgreSQL with pgvector within PCAI to perform semantic search over company documents during agent reasoning.
*   **Secure Networking**: Integrated natively with PCAI's Istio Gateway to securely expose the application's Virtual Service endpoints.

## Deploying on HPE PCAI via Import Framework
*Note: Manual installation instructions are omitted in favour of the PCAI-native deployment strategy.*

The application is packaged and prepared for deployment using the **HPE PCAI Import Framework**. 

1. Ensure your container images (`erdincka/meetings-frontend:latest` and `erdincka/meetings-backend:latest`) are accessible from the PCAI environment (or published via `publish_images.sh`).
2. Navigate to the **Import App** framework within the HPE PCAI AI Essentials UI.
3. Import the application using the provided **`meetings-0.1.0.tgz`** chart and `meetings-icon.png` as its icon.
4. The Import Framework will automatically:
    *   Provision the `meetings` namespace.
    *   Deploy the backend and frontend workloads.
    *   Configure the persistence layer (`nfs-csi`).
    *   Set up the Istio virtual service to route traffic to `meetings.<DOMAIN_NAME>`.
5. Once the deployment transitions to `Installed`, navigate to the generated endpoint to access the Agentic Meetings interface.
