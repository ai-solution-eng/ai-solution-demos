import streamlit as st
import pandas as pd
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

# Custom CSS for clean modern UI
st.markdown("""
<style>
    /* Global font and background */
    .main .block-container {
        font-family: 'Segoe UI', 'Roboto', 'Helvetica Neue', Arial, sans-serif;
        background-color: white;
        padding-top: 2rem;
    }
    
    /* Header styling */
    .app-header {
        display: flex;
        align-items: center;
        margin-bottom: 2rem;
        padding-bottom: 1rem;
        border-bottom: 1px solid #e5e5e5;
    }
    
    .app-title {
        color: #2c2c2c;
        font-size: 2.5rem;
        font-weight: 300;
        margin: 0;
        letter-spacing: -0.5px;
    }
    
    /* Clean card styling */
    .feature-card {
        background: white;
        padding: 1.5rem;
        border-radius: 8px;
        border: 1px solid #e5e5e5;
        margin-bottom: 1rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    
    .feature-card h3 {
        color: #2c2c2c;
        margin-top: 0;
        font-weight: 500;
        font-size: 1.25rem;
    }
    
    /* Button styling */
    .stButton > button {
        background: #01a982;
        color: white;
        border: none;
        border-radius: 6px;
        padding: 0.6rem 1.5rem;
        font-weight: 500;
        font-size: 0.95rem;
        transition: all 0.2s ease;
        border: 1px solid #01a982;
    }
    
    .stButton > button:hover {
        background: #018f73;
        box-shadow: 0 2px 8px rgba(1, 169, 130, 0.3);
    }
    
    /* Form styling */
    .stSelectbox > div > div > div {
        background-color: white;
        border: 1px solid #e5e5e5;
    }
    
    .stTextInput > div > div > input {
        background-color: white;
        border: 1px solid #e5e5e5;
    }
    
    /* Tab styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 2rem;
    }
    
    .stTabs [data-baseweb="tab"] {
        color: #666;
        font-weight: 500;
    }
    
    .stTabs [aria-selected="true"] {
        color: #01a982;
    }
    
    /* Message styling */
    .success-message {
        background: #f0f9f7;
        color: #0d5d4a;
        padding: 1rem;
        border-radius: 6px;
        border-left: 4px solid #01a982;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)

# Header with HPE logo and app title - left aligned
st.markdown("""
<div class="app-header">
    <img src="data:image/png;base64,{}" style="height: 40px; width: auto; margin-right: 1rem;">
    <h1 class="app-title">Predictive Maintenance Demo</h1>
</div>
""".format(
    base64.b64encode(open("assets/HPE-logo-2025.png", "rb").read()).decode()
), unsafe_allow_html=True)

# Function to clean column names for display
def clean_column_names(df):
    """Convert database column names to user-friendly display names"""
    column_mapping = {
        'ticketList_subject': 'Subject',
        'ticketList_detailproblem': 'Detail Problem',
        'ticketList_source_cause': 'Source Cause',
        'ticketList_product_category': 'Product Category',
        'ticketList_downtime': 'Downtime',
        'ticketList_uptime': 'Uptime',
        'ticketList_status': 'Status',
        'ticketList_resolution': 'Resolution',
        'ticketList_mttrall': 'MTTR',
        'ticketList_priority': 'Priority',
        'ticketList_createtime': 'Created',
        'ticketList_closetime': 'Closed',
        'ticketList_cityname': 'City',
        'ticketList_regionname': 'Region'
    }
    
    # Create a copy and rename columns for display
    display_df = df.copy()
    display_df = display_df.rename(columns=column_mapping)
    return display_df

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
                
                try:
                    # Convert datetime objects to pandas datetime for proper comparison
                    start_datetime_pd = pd.to_datetime(st.session_state.start_datetime)
                    end_datetime_pd = pd.to_datetime(st.session_state.end_datetime)
                    
                    # Convert the ticketList_downtime column to datetime if it's not already
                    if test_data['ticketList_downtime'].dtype == 'object':
                        test_data_converted = test_data.copy()
                        # Show original format before conversion
                        st.info(f"Original data format examples: {list(test_data['ticketList_downtime'].dropna().head(3))}")
                        
                        # Convert with explicit format specification for MM/DD/YYYY HH:MM
                        test_data_converted['ticketList_downtime'] = pd.to_datetime(
                            test_data['ticketList_downtime'], 
                            format='%m/%d/%Y %H:%M',
                            errors='coerce'
                        )
                        
                        # Show converted format
                        converted_examples = list(test_data_converted['ticketList_downtime'].dropna().head(3))
                        st.info(f"Converted data format: {converted_examples}")
                    else:
                        test_data_converted = test_data
                    
                    # Show search range for comparison
                    st.info(f"Search range: {start_datetime_pd} to {end_datetime_pd}")
                    
                    # Filter using datetime comparison
                    st.session_state.df = test_data_converted[
                        (test_data_converted['ticketList_downtime'] >= start_datetime_pd) & 
                        (test_data_converted['ticketList_downtime'] <= end_datetime_pd)
                    ].reset_index(drop=True)
                    
                    st.success(f"Found {len(st.session_state.df)} tickets")
                    
                    # Debug information - always show for troubleshooting
                    with st.expander("Dataset Analysis", expanded=len(st.session_state.df) == 0):
                        st.write(f"**Total tickets in dataset:** {len(test_data_converted)}")
                        st.write(f"**Date range searched:** {start_datetime_pd} to {end_datetime_pd}")
                        st.write(f"**Tickets found:** {len(st.session_state.df)}")
                        
                        # Show date range in the dataset
                        valid_dates = test_data_converted['ticketList_downtime'].dropna()
                        if len(valid_dates) > 0:
                            min_date = valid_dates.min()
                            max_date = valid_dates.max()
                            st.write(f"**Dataset date range:** {min_date} to {max_date}")
                            
                            # Show tickets in the selected date range (verification)
                            selected_range_tickets = test_data_converted[
                                (test_data_converted['ticketList_downtime'] >= start_datetime_pd) & 
                                (test_data_converted['ticketList_downtime'] <= end_datetime_pd)
                            ]
                            st.write(f"**Tickets in selected range:** {len(selected_range_tickets)}")
                            
                            # Show months with most tickets (sorted by count)
                            st.write("**Months with most tickets (Top 10):**")
                            date_counts = valid_dates.dt.to_period('M').value_counts().sort_values(ascending=False)
                            
                            for period, count in date_counts.head(10).items():
                                # Convert period back to datetime for range suggestion
                                period_start = period.start_time.date()
                                period_end = period.end_time.date()
                                st.write(f"- **{period}**: {count} tickets (Range: {period_start} to {period_end})")
                            
                            if len(date_counts) > 10:
                                st.write(f"... and {len(date_counts) - 10} more months")
                            
                            # Suggest a good range
                            if len(date_counts) > 0:
                                best_month = date_counts.index[0]
                                best_count = date_counts.iloc[0]
                                if best_count >= 5:
                                    best_start = best_month.start_time.date()
                                    best_end = best_month.end_time.date()
                                    st.success(f"**Recommended range:** {best_start} to {best_end} ({best_count} tickets)")
                                else:
                                    # Find a range that spans multiple months to get 5+ tickets
                                    cumulative = 0
                                    months_needed = []
                                    for period, count in date_counts.items():
                                        months_needed.append(period)
                                        cumulative += count
                                        if cumulative >= 5:
                                            range_start = months_needed[0].start_time.date()
                                            range_end = months_needed[-1].end_time.date()
                                            st.success(f"**Recommended range:** {range_start} to {range_end} ({cumulative} tickets across {len(months_needed)} months)")
                                            break
                        else:
                            st.warning("No valid dates found in the dataset!")
                            
                except Exception as e:
                    st.error(f"Error processing dates: {str(e)}")
                    st.info("Using original string comparison as fallback...")
                    
                    # Fallback to original method
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
                "Subject", 
                placeholder="Enter the main subject of the ticket"
            )
            st.session_state.selected_row["ticketList_detailproblem"] = st.text_area(
                "Detail Problem", 
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
        
        # Display data with cleaned column names
        display_df = clean_column_names(st.session_state.df)
        edited_df = st.data_editor(
            display_df,
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
        classification_display = clean_column_names(st.session_state.classification_batch)
        st.dataframe(classification_display, use_container_width=True)
        
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
