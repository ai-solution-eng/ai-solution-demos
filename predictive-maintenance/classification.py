from transformers import AutoTokenizer
from transformers import AutoModelForSequenceClassification
import pandas as pd
import joblib
import torch
import yaml
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.metrics import confusion_matrix
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
from config_handler import load_config

config = load_config()

# ticket_df = pd.read_csv(config["ticket_data"],low_memory=False)
# ticket_df["ticketList_downtime"] = pd.to_datetime(ticket_df["ticketList_downtime"], errors="coerce")

model_name = config["classification_model"]["name"]  
checkpoint_path = config["classification_model"]["checkpoint_path"]  
label_encoder = joblib.load(config["classification_model"]["label_encoder"])
tokenizer = AutoTokenizer.from_pretrained(checkpoint_path)
model = AutoModelForSequenceClassification.from_pretrained(checkpoint_path)


def get_metrics(df):
    accuracy = accuracy_score(df['ticketList_causegroup'], df['prediction'])
    precision = precision_score(df['ticketList_causegroup'], df['prediction'], average='weighted', zero_division=1)
    recall = recall_score(df['ticketList_causegroup'], df['prediction'], average='weighted', zero_division=1)
    f1 = f1_score(df['ticketList_causegroup'], df['prediction'], average='weighted', zero_division=1)
    metrics = {
        'Accuracy': accuracy,
        'Precision': precision,
        'Recall': recall,
        'F1-Score': f1
    }
    return metrics

def batch_predict(df):
    cols=[
        'ticketList_ticket_no',
        'ticketList_causegroup',
        'ticketList_subject',
        'ticketList_detailproblem',
        'prediction',
        'confidence'
    ]
    df = preprocess_subject(df)
    df = preprocess_detail_problem(df)
    df = df.astype(str)
    df['combined'] = df['Subject'] + " [SEP] " + df['Detail_Problem']
    
    inputs = tokenizer(list(df['combined']), return_tensors="pt", truncation=True, padding=True, max_length=128)
    with torch.no_grad():
        outputs = model(**inputs)
    probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
    confidence, predicted_class = torch.max(probs, dim=-1)
    # predicted_classes = torch.argmax(outputs.logits, dim=1).cpu().numpy()
    df['prediction'] = label_encoder.inverse_transform(predicted_class.cpu().numpy())
    df['confidence'] = confidence
    return df[cols]

def predict(subject,detail_problem):
    df = pd.DataFrame(columns = ['ticketList_subject','ticketList_detailproblem'],data=[[subject,detail_problem]])
    df = preprocess_subject(df)
    df = preprocess_detail_problem(df)
    df = df.astype(str)

    text = df['Subject'][0] + " [SEP] " + df['Detail_Problem'][0]
    inputs = tokenizer(text, return_tensors="pt", truncation=True, padding=True, max_length=128)
    outputs = model(**inputs)
    probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
    confidence, predicted_class = torch.max(probs, dim=-1)
    # predicted_class = torch.argmax(outputs.logits, dim=1).item()
    return confidence.item(), label_encoder.inverse_transform([predicted_class])[0]

def create_prediction_distribution_plot(sample_df):
    # Set a visually appealing style for dark mode
    plt.style.use('dark_background')
    sns.set_context('talk')

    # Use cubehelix or custom palette for vibrant + dark mode-safe colors
    palette = sns.color_palette("cubehelix", len(sample_df['prediction'].unique()))
    
    # Data prep
    value_counts = sample_df['prediction'].value_counts().sort_values(ascending=False)

    # Create figure with reduced size
    fig, ax = plt.subplots(figsize=(8, 4), facecolor='none')  # Smaller size (8, 5)
    sns.barplot(y=value_counts.index, x=value_counts.values, palette=palette, ax=ax,width=0.5)  # Swap x and y

    # Title and labels
    ax.set_title('Prediction Distribution', fontsize=12, fontweight='bold', color='white', pad=8)
    # ax.set_xlabel('Count', fontsize=8, color='white', labelpad=8)
    ax.set_ylabel('Predicted Classes', fontsize=8, color='white', labelpad=8)
    ax.grid(False)
    ax.set_xticks([])
    # Tick styling with reduced size
    # ax.tick_params(axis='x', colors='white', labelsize=8)
    ax.tick_params(axis='y', colors='white', labelsize=8)

    # Remove spines for a cleaner look
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Annotate bars
    for p in ax.patches:
        ax.annotate(f'{int(p.get_width())}',
                    (p.get_width()+ 0.3, p.get_y() + p.get_height() / 2.),
                    ha='left', va='center',
                    fontsize=8, color='white')

    # Set transparent background for blending with Streamlit dark mode
    fig.patch.set_alpha(0.0)
    ax.set_facecolor('none')

    plt.tight_layout()
    return fig

def plot_confusion_matrix(sample):
    # Compute the confusion matrix
    cm = confusion_matrix(sample['ticketList_causegroup'], sample['prediction'], labels=sample['ticketList_causegroup'].unique())

    # Set the dark background style
    plt.style.use('dark_background')
    sns.set_context('talk')

    # Create the plot
    fig, ax = plt.subplots(figsize=(8, 5))

    # Plot confusion matrix as heatmap with dark theme
    sns.heatmap(cm, annot=True, fmt='d', cmap='inferno', 
                xticklabels=sample['ticketList_causegroup'].unique(),
                yticklabels=sample['ticketList_causegroup'].unique(),
                cbar=False,  
                annot_kws={'size': 8, 'weight': 'bold', },  # annotation styling
                linewidths=0.2, linecolor='white', ax=ax)  # white gridlines

    # Title and labels with dark theme styling
    ax.set_title('Confusion Matrix', fontsize=10, fontweight='bold', color='white', pad=10)
    ax.set_xlabel('Predicted', fontsize=8, color='white', labelpad=5)
    ax.set_ylabel('Actual', fontsize=8, color='white', labelpad=5)
    ax.grid(False)
    # Tick styling with white color for dark background
    ax.tick_params(axis='x', colors='white', labelsize=5)
    ax.tick_params(axis='y', colors='white', labelsize=5)

    # Remove spines for a cleaner look
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Set transparent background for blending with Streamlit dark mode
    fig.patch.set_alpha(0.0)
    ax.set_facecolor('none')


    # Tight layout for cleaner appearance
    plt.tight_layout()
    return fig

def plot_metrics(metrics):
    # Create a figure
    fig, ax = plt.subplots(figsize=(6, 3))

    # Create the barplot
    sns.barplot(x=list(metrics.values()), y=list(metrics.keys()), palette='viridis', ax=ax, width=0.5)

    # Customize the plot
    ax.set_xlim(0, 1)
    # ax.set_xlabel('Score')
    ax.set_title('Model Performance Metrics', fontsize=10, weight='bold')
    ax.set_xticks([])
    ax.set_yticklabels(ax.get_yticklabels(), fontsize=10)
    # Remove spines for a cleaner look
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Add score labels
    for i, (metric, value) in enumerate(metrics.items()):
        ax.text(value + 0.01, i, f'{value:.2f}', va='center')

    fig.patch.set_alpha(0.0)
    ax.set_facecolor('none')

    plt.tight_layout()
    return fig

def preprocess_subject(df1):
    df1['ticketList_subject'] = df1['ticketList_subject'].str.lower()
    df1['Subject'] = -1
    
    pattern = r'link supernet .* down'
    df1.loc[df1['ticketList_subject'].str.contains(pattern, case=False, na=False, regex=True), 'Subject'] = "link supernet down"


    pattern = [r'detect alarm .* inquiry',r'detect alarm .* degrade',r'detect alarm .* intermittent']
    pattern = '|'.join(pattern)
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "detect alarm degrade"

    pattern = r"link .* can't access website"
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "can't access website"

    pattern = r'tunneling stm.* down'
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "tunneling stm down"

    pattern = 'alarm genset fail'
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "alarm genset fail"

    pattern = r'link .* bad performance'
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "link bad performance"

    pattern = r'link .* service request'
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "link service request"

    pattern = [r'link .* down',r'link .* intermittent',r'link .* degrade']
    pattern = '|'.join(pattern)
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "link down"

    pattern = r'link .* high latency'
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "link high latency"

    pattern = r'link .* flapping'
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "link flapping"

    pattern = [r'[ipcore] .* flapping',r'[ipcore] .* degrade']
    pattern = '|'.join(pattern)
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "ipcore flapping"

    pattern = r'\[enterprise\].*down'
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False,regex=True)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "enterprise down"

    pattern = [r'dark core .* down',r'dark core .* degrade']
    pattern = '|'.join(pattern)
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "dark core down"

    pattern = [r'1 core .* down',r'2 core .* down',r'4 core .* down',r'1 core .*degrade',r'2 core .*degrade',r'4 core .*degrade']
    pattern = '|'.join(pattern)
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "n core down"

    pattern = r'fiberisation .* down'
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "fiberisation down"

    pattern = [r'high urgent maintenance .* cable',r'high urgent maintenance .* core',r'high urgent maintenance .* ampere']
    pattern = '|'.join(pattern)
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "high urgent maintainance cable"

    pattern = ['request perapihan','request pemindahan','pengaduan masyarakat']
    pattern = '|'.join(pattern)
    df1.loc[
        (df1['ticketList_subject'].str.contains(pattern, case=False, na=False)) &
        (df1['Subject'] == -1),  
        'Subject'
    ] = "pengaduan masyarakat"
    
    return df1

def preprocess_detail_problem(df1):
    df1['ticketList_detailproblem'] = df1['ticketList_detailproblem'].str.lower()
    df1['Detail_Problem']=-1
    words_to_check = ['problem at customer side',
                      'issue at customer side',
                      'problem at customer site',
                      'issue at customer site',
                      'activity at customer',
                      'activity customer',
                      'customer problem',
                      'customer activity',
                      "customer's internal problem",
                      'cust side'
                     ]
    pattern = '|'.join(words_to_check)
    df1.loc[df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False), 'Detail_Problem'] = "issue at customer side"
    
    words_to_check = ['no problem at mti',
                      'no problem in mti',
                      'no problem on mti',
                      'no problem from mti',
                      'no problem from mti side',
                      'no issue at mti',
                      'no error at mti',
                      'no issue from mti',
                      'no problem in ABC company',
                      'no problem at ABC company',
                      'no problem on ABC company'
                      'no issue at mti side',
                      'no issue at ABC company',
                      'no issue in ABC company',
                      'no issue on ABC company',
                      'no log in ABC company',
                      'no log at mti',
                      'no log down at',
                      'log drown from mti',
                      'no problem at network ABC company',
                      'no problem at side mti',
                      'no problem at network mti']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "no problem at mti side"    

    words_to_check = ['fo cut', 'fiber optic', 'fiber','optical mux problem','cable core cut','core cut','cut point','otdr cut']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "fiber optic cut"    

    words_to_check = ['fast connector','fastconnector','fast c','fastc','bad fc','fasr connector']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "fast connector"    

    words_to_check = ['patchcord problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "patchcord problem"

    words_to_check = ['barrel problem','bad barrel','barrel','pigtail','barel']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "barrel/pigtail problem"

    words_to_check = ['mikrotik','miktrotik']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "mikrotik problem"

    words_to_check = ['olt problem', 'ont problem','ont faulty']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "olt/ont problem"

    words_to_check = ['splitter problem','modem ','bad spliter','splitter at','splitter &','splitter+']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "splitter/modelm problem"

    words_to_check = ['connector problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "connector problem"

    words_to_check = ['core flapping','flapping problem','detect flapping']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "core flapping"

    words_to_check = ['service request']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "service request"

    words_to_check = ['power failure',
                      'power fail',
                      'power level drop',
                      'power issue',
                      'issue power',
                      'power outage',
                      'power trip',
                      'power problem',
                      'power outlet problem',
                      'power module failure',
                      'flicker problem from pln',
                      'power unit supply (psu) problem',
                      'power supply unit (psu) problem',
                      'psu',
                      'power panel',
                      'pln off','pln problem','power off','power pro','power module problem',
                      'unstable voltage']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "power failure"

    words_to_check = ['ups problem','inverter problem','ups']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "ups problem"

    words_to_check = ['mcb trip','mcb problem','high temperature','high temp']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "mcb/high temp problem"

    words_to_check = ['detect flicker section','detect flicker segment',]
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "detect flicker section"

    words_to_check = ['core down','bad core','core problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "core down"

    words_to_check = ['routing problem','routing to','router problem','router faulty']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "routing problem"

    words_to_check = ['problem at switch','switch problem','mcb & switch problem','detect switch','firmware']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "switch problem"

    words_to_check = ['email problem',]
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "email problem"

    words_to_check = ['high latency',]
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "high latency"

    words_to_check = ['radio problem',]
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "radio problem"

    words_to_check = ['ip problem','inquiry ip','inquiry vpn','ip block','block ip','configurationip','configuration ip','ipv6','ip address','setting ip','check ip','detail ip','ip prefix','ip destination','ip spam']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "ip problem"

    words_to_check = ['software problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "software problem"

    words_to_check = ['omcc problem',]
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "omcc problem"

    words_to_check = ['xml problem','vlan','upgrade ios router','snmp problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "xml/vlan/snmp/router problem"

    words_to_check = ['access point problem','adapter problem','access point and','adaptor problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "access point/adapter problem"

    words_to_check = ['administration issue','administration problem','issue administration']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "administration issue"

    words_to_check = ['ddos attack','ddos problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "ddos attack"

    words_to_check = ['port problem','port down','port ethernet problem','port optical problem','port flapping']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "port problem"

    words_to_check = ['unknown fault','unknown problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "unknown fault"

    words_to_check = ['alarm filter clogged']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "alarm filter clogged"

    words_to_check = ["can't access web",'cannot access web']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "cannot access web"

    words_to_check = ['detected monitoring down by customer']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "detected monitoring down by customer"

    words_to_check = ['detect bad performance']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "detect bad performance"

    words_to_check = ['dropwire','drop wire problem',]
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "dropwire problem"

    words_to_check = ['issue alarm device at customer side']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "issue alarm device at customer side"

    words_to_check = ['full traffic at customer side']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "full traffic at customer side"

    words_to_check = ['impact problem from']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "impact problem from"

    words_to_check = ['detect alarm power','detect alarm in power']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "detect alarm power"

    words_to_check = ['mpls problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "mpls problem"

    words_to_check = ['bandwidth']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "bandwidth problem"

    words_to_check = ['lacp problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "lacp problem"

    words_to_check = ['iptv','bgp','vpls','smtp']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "iptv/bgp/vpls.smtp problem"

    words_to_check = ['bending']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "bending"

    words_to_check = ['sfp ']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "sfp problem"

    words_to_check = ['ethernet']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "ethernet"

    words_to_check = ['request pemindahan','request perapihan']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "PM (Public Complaint)"

    words_to_check = ['lan ']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "lan problem"

    words_to_check = ['module card problem','card problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "module card problem"

    words_to_check = ['dw problem','dw cable problem']
    pattern = '|'.join(words_to_check)
    df1.loc[
        (df1['ticketList_detailproblem'].str.contains(pattern, case=False, na=False)) &
        (df1['Detail_Problem'] == -1),  
        'Detail_Problem'
    ] = "dw problem"
    
    return df1