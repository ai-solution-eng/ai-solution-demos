# Predictive Maintenance Demo

A gen-AI powered predictive maintenance application built with Streamlit, featuring three core capabilities:

1. **Resolution Prediction**: Leverages LLaMA models to predict maintenance resolutions based on historical ticket data and embeddings
2. **Ticket Classification**: Uses BERT-based models to automatically classify maintenance tickets into appropriate categories
3. **Network Inspection OCR**: Employs Qwen VL models to extract and analyze text from network equipment photos for diagnostic purposes

## Helm Chart Deployment

This application is packaged as a Kubernetes Helm chart designed for deployment on HPE PCAI infrastructure with Istio service mesh integration.

### EZUA Integration

The Helm chart includes EZUA (HPE's application orchestration platform) integration through:

- **VirtualService Configuration**: Automatically configures Istio VirtualService for external access
- **Gateway Integration**: Routes traffic through `istio-system/ezaf-gateway`
- **Dynamic Endpoints**: Supports `${DOMAIN_NAME}` variable substitution for flexible domain configuration

### Key Features

- **ConfigMap Mounting**: Application configuration mounted as ConfigMap for runtime flexibility
- **Istio Service Mesh**: Native integration with Istio for traffic management and security
- **Resource Management**: Configured with appropriate CPU/memory limits for ML workloads
- **Health Checks**: Built-in liveness and readiness probes for Kubernetes orchestration
- **HPE PCAI Optimized**: Tailored for HPE Private Cloud AI infrastructure requirements

### Deployment

Two assets required for the app deployment on PCAI via "Import Framework" are:

1. The packaged helm chart: ./assets/predictive-maintenance-0.1.8.tgz
2. The app logo: ./assets/logo.png

### Testing: 

- For ticket resolution prediction & classification, refer to the notebooks in ./postgresql to set up a table data in PostgreSQL. You can deploy PostgreSQL on the same PCAI cluster using [this framework](https://github.com/ai-solution-eng/frameworks/tree/main/postgresql). 

- For demoing the OCR use case, use the test image under ./data/test-image.jpg. Hint: You might need to add this sentence to the existing template suggested prompts in case the decimals are not properly identified.

```yaml
Make sure a comma in between digits is treated as "." or decimal.
```

# Access via configured domain
# Application will be available at: predictive-maintenance.${DOMAIN_NAME}

The application supports dynamic endpoint configuration through the UI, allowing runtime updates to ML inference server URLs and authentication tokens without requiring redeployment.

## HPE PCAI Integration

### Bring Your Own Application (BYOA)

For detailed instructions on deploying custom applications to HPE PCAI using the import framework, refer to:
- [BYOA Tutorials](https://github.com/HPEEzmeral/byoa-tutorials) - Step-by-step guides for bringing your own applications to PCAI

### Model Deployment

To deploy and manage ML models on HPE PCAI infrastructure:
- [HPE MLIS Documentation](https://docs.ai-solutions.ext.hpe.com/products/mlis/latest/) - Complete guide for ML inference services on PCAI

### Acknowledgement:
- HPE GSE Team - Original use case development.
- [Roh Geun Tak](https://github.com/rohgeuntak76) - Migrating the helm chart over to PCAI.
- [Daniel Cao](https://github.com/caovd) - Solution architect for designing, testing, adapting and demoing these use cases on PCAI. 

### Contribution: 
- No contribution/further development is expected except for POC/demo purposes.

