<div align=center>
<img src="https://raw.githubusercontent.com/hpe-design/logos/master/Requirements/color-logo.png" alt="HPE Logo" height="100"/>
</div>

# HPE Private Cloud AI

##  AI Solution Vertical Demos

This folder contains demos bound to a specific vertical, sometimes adapted from more generic use cases to better fit a vertical narrative. 

Unlike demos from the root level of this repository, these demos are **not expected to easily be run with your own data**.

Here is the list of demos you will find in this vertical folder:

| Demo                                 | Short Description          | Demo Video      | 
| -------------------------------------|----------------------------|-----------------|
| [Molecular Aligned Multi-Modal Architecture and Language (Biomed-MAMMAL)](biomed-mammal) | A **BentoML** inference service for a **biomedical foundation model** which achieves state-of-the-art results over a variety of tasks across the entire **drug discovery** pipeline and diverse **biomedical domains**. | - |
| [Blood Vessel Geometry Analysis and Reconstruction](blood-vessel-geometry-analysis-and-reconstruction)              | A streamlit application relying on **NVIDIA Vista 3D model** (deployed using **MLIS**) to analyze, reconstruct and render vessels in 3D. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Enhancing%20Healthcare%20with%20AI_%20Blood%20Vessel%20Analysis%20and%203D%20Reconstruction(1).mp4) |
| [Defence Ops](defence-ops)                          | A web application leveraging a VLM (deployed using **MLIS**)to analyze videos, with preloaded defence-related ones provided for example.             | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/DefenceOps.mp4) |
| [Genome Sequencing](genome-sequencing)                | **Notebooks** leveraging **NVIDIA Parabricks** for genome sequencing.           | - |
| [Hospital Visit Summary](hospital-visit-summary)                      | A **streamlit application** that can display patient information regarding their previous visits from a database, and summarize it. Requires deploying an LLM using **MLIS**.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/PatientVisitSummariesApp.mp4) |
| [Lawfirm Co](lawfirm-co)                        | An application using RAG and video analytics in the context of legal documents analysis. Requires deploying a VLM and embedding model using **MLIS**.          | - |
| [License Plate Number Detection](license-plate-number-detection)                        | An application using an object detection model (YOLO) and an OCR one to extract license plate numbers from videos. Uses **MLIS** for model deployment.| - |
| [Maintenance Ticket Assistant](maintenance-ticket-assistant)                        | An application that can classifies tickets and provide expected resolution steps using a chat model. Also uses OCR to analyze text from network equipment photos for diagnostic purposes. Relies on **MLIS** for model deployment.| - |
| [Predictive Maintenance](predictive-maintenance)                        | A predictive maintenance model trained leveraging **Jupyter Notebook**, tracked in **MLFlow**, packaged with BentoML and deployed via **MLIS**.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/predictive-maintenance-demo.mp4) |
| [Traffic Report](traffic-report)                        | A **streamlit** application that uses a VLM and YOLO to detect vehicles in images/videos and provide an analysis of the scenes. Relies on **MLIS** for model deployment.| [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/traffic-report-demo.mp4) |
| [Water Utility Planner](water-utility-planner)                        | A chat assistant in charge of predicting which sewer pipes require inspection and why, requiring XGBoost model training with **Jupyter Notebooks**, tracking with **MLflow**, packaging with BentoML, deployment with **MLIS**, using **Open WebUI** for interaction. | [link](https://storage.googleapis.com/ai-solution-engineering-videos/public/Water%20Utility%20Agentic%20Planner%20-%20Short%20version.mp4) |

