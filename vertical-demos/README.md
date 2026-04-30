<div align=center>
<img src="https://raw.githubusercontent.com/hpe-design/logos/master/Requirements/color-logo.png" alt="HPE Logo" height="100"/>
</div>

# HPE Private Cloud AI

##  AI Solution Vertical Demos

This folder contains demos bound to a specific vertical, sometimes adapted from more generic use cases to better fit a vertical narrative. 

Unlike demos from the root level of this repository, these demos are **not expected to easily be run with your own data**.

Here is the list of demos you will find in this vertical folder:

| Demo                                                          | Short Description          |
| --------------------------------------------------------------|----------------------------|
| [Blood Vessel Geometry Analysis and Reconstruction](blood-vessel-geometry-analysis-and-reconstruction)              | A streamlit application relying on **NVIDIA Vista 3D model** (deployed using **MLIS**) to analyze, reconstruct and render vessels in 3D. |
| [Defence Ops](defence-ops)                          | A web application leveraging a VLM (deployed using **MLIS**)to analyze videos, with preloaded defence-related ones provided for example.             |
| [Genome Sequencing](genome-sequencing)                | **Notebooks** leveraging **NVIDIA Parabricks** for genome sequencing.           |
| [Hospital Visit Summary](hospital-visit-summary)                      | A **streamlit application** that can display patient information regarding their previous visits from a database, and summarize it. Requires deploying an LLM using **MLIS**.|
| [Lawfirm Co](lawfirm-co)                        | An application using RAG and video analytics in the context of legal documents analysis. Requires deploying a VLM and embedding model using **MLIS**.          |
| [License Plate Number Detection](license-plate-number-detection)                        | An application using an object detection model (YOLO) and an OCR one to extract license plate numbers from videos. Uses **MLIS** for model deployment.|
| [Predictive Maintenance](predictive-maintenance)                        | An application that can classifies tickets and provide expected resolution steps using a chat model. Also uses OCR to analyze text from network equipment photos for diagnostic purposes. Relies on **MLIS** for model deployment.|
| [Traffic Report](traffic-report)                        | A **streamlit** application that uses a VLM and YOLO to detect vehicles in images/videos and provide an analysis of the scenes. Relies on **MLIS** for model deployment.|

