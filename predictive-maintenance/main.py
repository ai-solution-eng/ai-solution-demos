import streamlit as st
from classification import predict,batch_predict,create_prediction_distribution_plot,plot_confusion_matrix,plot_metrics,get_metrics
from ocr import extract_text_from_image
from resolution_mttr import test_data,predict_resolution,predict_mttr
from datetime import datetime, date
import base64
import os

# Page config
st.set_page_config(
    page_title="Predictive Maintenance Demo",
    page_icon="⚙️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for clean professional UI
st.markdown("""
<style>
    .main-header {
        display: flex;
        align-items: center;
        justify-content: center;
        background: linear-gradient(135deg, #00B2B3 0%, #00D1C7 100%);
        padding: 1.5rem;
        border-radius: 8px;
        margin-bottom: 2rem;
        color: white;
        text-align: center;
    }
    
    .hpe-logo {
        font-size: 2rem;
        font-weight: bold;
        color: #ffffff;
        margin-right: 1rem;
        background: rgba(255, 255, 255, 0.1);
        padding: 0.5rem 1rem;
        border-radius: 4px;
    }
    
    .app-title {
        font-size: 2rem;
        font-weight: 600;
        color: white;
        margin: 0;
    }
    
    .feature-card {
        background: #f8f9fa;
        padding: 1.5rem;
        border-radius: 8px;
        border: 1px solid #e9ecef;
        margin-bottom: 1rem;
        border-left: 4px solid #00B2B3;
    }
    
    .feature-card h3 {
        color: #495057;
        margin-top: 0;
        font-weight: 600;
    }
    
    .stButton > button {
        background: #00B2B3;
        color: white;
        border: none;
        border-radius: 4px;
        padding: 0.5rem 1rem;
        font-weight: 500;
        transition: all 0.2s ease;
    }
    
    .stButton > button:hover {
        background: #008a8b;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
    }
    
    .success-message {
        background: #d4edda;
        color: #155724;
        padding: 1rem;
        border-radius: 4px;
        border-left: 4px solid #28a745;
        margin: 1rem 0;
    }
    
    .config-section {
        background: #f8f9fa;
        padding: 1.5rem;
        border-radius: 8px;
        border: 1px solid #e9ecef;
        margin-bottom: 1rem;
    }
    
    .stTab > div > div > div > div {
        padding: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)

# Header with HPE logo and app title
st.markdown("""
<div class="main-header">
    <div class="hpe-logo">HPE</div>
    <h1 class="app-title">Predictive Maintenance Demo</h1>
</div>
""", unsafe_allow_html=True)

# Initialize session state
if "ticket_df" not in st.session_state:
    st.session_state.ticket_df = test_data

# Initialize config session state
if "config_updated" not in st.session_state:
    st.session_state.config_updated = False

# Main navigation using tabs
tab1, tab2, tab3 = st.tabs(["Resolution & Classification", "Network Inspection OCR", "Endpoint Configuration"])

def save_config_to_env():
    """Save configuration to environment variables"""
    if st.session_state.get('llm_url'):
        os.environ['LLM_INFERENCE_URL'] = st.session_state.llm_url
    if st.session_state.get('llm_token'):
        os.environ['LLM_INFERENCE_TOKEN'] = st.session_state.llm_token
    if st.session_state.get('ocr_url'):
        os.environ['OCR_INFERENCE_URL'] = st.session_state.ocr_url
    if st.session_state.get('ocr_token'):
        os.environ['OCR_INFERENCE_TOKEN'] = st.session_state.ocr_token
    st.session_state.config_updated = True

with tab3:
    st.markdown("""
    <div class="config-section">
        <h3>Endpoint Configuration</h3>
        <p>Configure API endpoints and authentication tokens for the AI models.</p>
    </div>
    """, unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("LLM Configuration")
        st.text_input("LLM Inference URL", key="llm_url", 
                     value=os.environ.get('LLM_INFERENCE_URL', ''),
                     help="URL for the LLM inference server")
        st.text_input("LLM Token", key="llm_token", type="password",
                     value=os.environ.get('LLM_INFERENCE_TOKEN', ''),
                     help="Authentication token for LLM service")
    
    with col2:
        st.subheader("OCR Configuration")
        st.text_input("OCR Inference URL", key="ocr_url",
                     value=os.environ.get('OCR_INFERENCE_URL', ''),
                     help="URL for the OCR inference server")
        st.text_input("OCR Token", key="ocr_token", type="password",
                     value=os.environ.get('OCR_INFERENCE_TOKEN', ''),
                     help="Authentication token for OCR service")
    
    if st.button("Save Configuration"):
        save_config_to_env()
        st.success("Configuration saved successfully!")
        st.rerun()

with tab2:
    st.markdown("""
    <div class="feature-card">
        <h3>Network Inspection OCR</h3>
        <p>Extract network performance metrics from screenshots using advanced computer vision.</p>
    </div>
    """, unsafe_allow_html=True)
    
    df = extract_text_from_image()
    if df is not None:
        st.markdown('<div class="success-message">Inference completed successfully!</div>', unsafe_allow_html=True)
        st.dataframe(df, use_container_width=True)

with tab1:
    st.markdown("""
    <div class="feature-card">
        <h3>Resolution & Classification</h3>
        <p>Predict ticket resolutions, estimate MTTR, and classify tickets using advanced AI models.</p>
    </div>
    """, unsafe_allow_html=True)

    if "custom_inputs" not in st.session_state:
        st.session_state.custom_inputs = False

    if "start_date" not in st.session_state:
        st.session_state.start_date = date.today()
    if "end_date" not in st.session_state:
        st.session_state.end_date = date.today()


    if "submitted" not in st.session_state:
        st.session_state.submitted = False
    if "selected_row" not in st.session_state:
        st.session_state.selected_row = None    

    if "resolution" not in st.session_state:
        st.session_state.resolution = None
    if "mttr" not in st.session_state:
        st.session_state.mttr = None
    if "classification" not in st.session_state:
        st.session_state.classification = None
    if "classification_batch" not in st.session_state:
        st.session_state.classification_batch = None

    # Input mode selection
    st.markdown("### Input Mode")
    col1, col2 = st.columns(2)
    with col1:
        st.checkbox("Custom Inputs", key="custom_inputs", help="Enter ticket details manually")
    with col2:
        if not st.session_state.custom_inputs:
            st.info("Date Range Mode: Select from existing tickets")
        else:
            st.info("Custom Mode: Enter ticket details manually")

    if not st.session_state.custom_inputs:
        st.session_state.selected_row = None   
        st.markdown("### Date Range Selection")
        
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Start Date & Time**")
            st.session_state.start_date = st.date_input("Start Date", value=st.session_state.start_date)
            st.session_state.start_time = st.time_input("Start Time", )
        
        with col2:
            st.markdown("**End Date & Time**")
            st.session_state.end_date = st.date_input("End Date", value=st.session_state.end_date)
            st.session_state.end_time = st.time_input("End Time",)
        st.session_state.start_datetime = datetime.combine(st.session_state.start_date, st.session_state.start_time)
        st.session_state.end_datetime = datetime.combine(st.session_state.end_date, st.session_state.end_time)

        if st.session_state.start_datetime > st.session_state.end_datetime:
            st.error("End date cannot be earlier than start date!")
        if st.button("Submit"):
            if st.session_state.start_datetime <= st.session_state.end_datetime:
                st.success(f"Selected Date Range: {st.session_state.start_datetime} to {st.session_state.end_datetime}")
                start_date_dt = f"{st.session_state.start_date.month}/{st.session_state.start_date.day}/{st.session_state.start_date.year} {st.session_state.start_time.strftime('%I:%M:%S %p')}"
                end_date_dt = f"{st.session_state.end_date.month}/{st.session_state.end_date.day}/{st.session_state.end_date.year} {st.session_state.end_time.strftime('%I:%M:%S %p')}"
    
                st.session_state.df = test_data[
                    (test_data['ticketList_downtime'] >= start_date_dt) & 
                    (test_data['ticketList_downtime'] <= end_date_dt)
                    ].reset_index(drop=True)
                st.success(f"Found {len(st.session_state.df)} tickets")

                st.session_state.df.insert(0, "Select", False)
                st.session_state.submitted = True
                st.session_state.selected_row = None    

    if st.session_state.custom_inputs:
        st.session_state.df = None
        st.session_state.submitted = False
        st.session_state.selected_row = {}
        
        st.markdown("### Custom Ticket Details")
        
        col1, col2 = st.columns(2)
        with col1:
            st.session_state.selected_row["ticketList_subject"] = st.text_input(
                "Ticket Subject", 
                placeholder="Enter the main subject of the ticket"
            )
            st.session_state.selected_row["ticketList_detailproblem"] = st.text_area(
                "Detailed Problem", 
                placeholder="Describe the problem in detail",
                height=100
            )
        
        with col2:
            st.session_state.selected_row["ticketList_source_cause"] = st.text_input(
                "Source Cause", 
                placeholder="Enter the root cause of the issue"
            )
            st.session_state.selected_row["ticketList_product_category"] = st.text_input(
                "Product Category", 
                placeholder="Enter the product category"
            )



    if st.session_state.submitted and st.session_state.df is not None:
        st.markdown("### Ticket Selection")
        st.markdown(f"**Found {len(st.session_state.df)} tickets in the selected date range**")

        st.session_state.resolution = None
        st.session_state.mttr = None
        st.session_state.classification = None
        st.session_state.classification_batch = None
        
        edited_df = st.data_editor(
            st.session_state.df,
            hide_index=False,
            num_rows="fixed",
            key="table_editor",
            column_config={"Select": st.column_config.CheckboxColumn("Select")},
            use_container_width=True   
        )
 
        selected_rows = edited_df[edited_df["Select"]]
        if len(selected_rows) == 1:  # Only allow one row to be selected
            st.session_state.selected_row = selected_rows.iloc[0]
        elif len(selected_rows) ==0:
            st.session_state.selected_row = None
            edited_df["Select"] = False 
        else:
            st.error("Please select only one row!")
            st.session_state.selected_row = None
            edited_df["Select"] = False 


        if st.session_state.selected_row is not None:
            st.markdown("### Selected Ticket Details")
            with st.expander("View ticket details", expanded=True):
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(f"**Subject:** {st.session_state.selected_row.get('ticketList_subject', 'N/A')}")
                    st.markdown(f"**Product Category:** {st.session_state.selected_row.get('ticketList_product_category', 'N/A')}")
                with col2:
                    st.markdown(f"**Source Cause:** {st.session_state.selected_row.get('ticketList_source_cause', 'N/A')}")
                    st.markdown(f"**Detail Problem:** {st.session_state.selected_row.get('ticketList_detailproblem', 'N/A')}")

    if st.session_state.selected_row is not None:
        st.markdown("### AI Predictions")
        st.markdown("Click the buttons below to get AI-powered predictions:")
        
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            if st.button("Resolution", help="Predict the resolution for this ticket"):
                st.session_state.classification_batch = None
                if st.session_state.resolution is None:  # Only predict once
                    ticketList_subject = st.session_state.selected_row['ticketList_subject']
                    ticketList_detailproblem = st.session_state.selected_row['ticketList_detailproblem']
                    ticketList_source_cause = st.session_state.selected_row['ticketList_source_cause']
                    ticketList_product_category = st.session_state.selected_row['ticketList_product_category']

                    st.session_state.resolution = predict_resolution(ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category)
                    
        with col2:
            if st.button("MTTR", help="Predict Mean Time To Resolution"):
                st.session_state.classification_batch = None
                if st.session_state.mttr is None:  
                    ticketList_subject = st.session_state.selected_row['ticketList_subject']
                    ticketList_detailproblem = st.session_state.selected_row['ticketList_detailproblem']
                    ticketList_source_cause = st.session_state.selected_row['ticketList_source_cause']
                    ticketList_product_category = st.session_state.selected_row['ticketList_product_category']
                    st.session_state.mttr = predict_mttr(ticketList_subject, ticketList_detailproblem, ticketList_source_cause, ticketList_product_category)
                    
        with col3:
            if st.button("Classification", help="Classify this ticket"):
                st.session_state.classification_batch = None
                if st.session_state.classification is None:  
                    ticketList_subject = str(st.session_state.selected_row['ticketList_subject'])
                    ticketList_detailproblem = str(st.session_state.selected_row['ticketList_detailproblem'])
                    confidence, st.session_state.classification = predict(ticketList_subject, ticketList_detailproblem)

        with col4:
            if not st.session_state.custom_inputs:
                if st.button("Batch Classification", help="Classify all tickets in batch"):
                    if st.session_state.classification_batch is None:  
                        st.session_state.classification_batch = batch_predict(st.session_state.df)
                        


    # Results Display Section
    if st.session_state.resolution is not None: 
        st.markdown("### Resolution Prediction")
        with st.container():
            st.markdown('<div class="success-message">', unsafe_allow_html=True)
            st.write(st.session_state.resolution.content)
            st.markdown('</div>', unsafe_allow_html=True)
            
    if st.session_state.mttr is not None: 
        st.session_state.classification_batch = None
        st.markdown("### Mean Time To Resolution")
        with st.container():
            st.markdown('<div class="success-message">', unsafe_allow_html=True)
            st.write(st.session_state.mttr.content)
            st.markdown('</div>', unsafe_allow_html=True)
            
    if st.session_state.classification is not None: 
        st.session_state.classification_batch = None
        st.markdown("### Classification Result")
        with st.container():
            st.markdown('<div class="success-message">', unsafe_allow_html=True)
            st.write(st.session_state.classification)
            st.markdown('</div>', unsafe_allow_html=True)
            
    if st.session_state.classification_batch is not None: 
        st.markdown("### Batch Classification Results")
        st.markdown('<div class="success-message">Batch Classification Completed Successfully!</div>', unsafe_allow_html=True)
        
        # Display results table
        st.markdown("#### Classification Results")
        st.dataframe(st.session_state.classification_batch, use_container_width=True)
        
        # Charts section
        st.markdown("#### Analytics Dashboard")
        
        # Distribution plot
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Prediction Distribution**")
            fig = create_prediction_distribution_plot(st.session_state.classification_batch)
            st.pyplot(fig)
        
        with col2:
            st.markdown("**Performance Metrics**")
            metrics = get_metrics(st.session_state.classification_batch)
            fig = plot_metrics(metrics)
            st.pyplot(fig)
        
        # Confusion matrix
        st.markdown("**Confusion Matrix**")
        fig = plot_confusion_matrix(st.session_state.classification_batch)
        st.pyplot(fig)
