#!/bin/bash

set -euo pipefail

# ===== Logging Setup =====
LOG_FILE="prepare_genome_data.log"
exec > >(tee -i "$LOG_FILE")
exec 2>&1

timestamp() {
    date +"[%Y-%m-%d %H:%M:%S]"
}

log() {
    echo "$(timestamp) $*"
}

# ===== Define Directories =====
BASE_DIR="$PWD/parabricks_test"
TEST_DIR="$BASE_DIR/test"
REF_DIR="$BASE_DIR/ref"
SNP_DIR="$BASE_DIR/snp"

mkdir -p "$BASE_DIR" "$TEST_DIR" "$REF_DIR" "$SNP_DIR"

# ===== Step 1: Download Reference Genome =====
log "Step 1: Downloading TAIR10 Reference Genome"
cd "$REF_DIR"
#curl -L -o ncbi_dataset.zip "https://www.ncbi.nlm.nih.gov/datasets/genome/GCF_000001735.4/"
#unzip -o ncbi_dataset.zip
#FASTA_ORIG=$(find ncbi_dataset -name "*_genomic.fna")
wget https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/735/GCF_000001735.4_TAIR10.1/GCF_000001735.4_TAIR10.1_genomic.fna.gz
gunzip GCF_000001735.4_TAIR10.1_genomic.fna.gz


# ===== Step 2: Clean FASTA Headers =====
log "Step 2: Removing 'Chr' from chromosome headers"
FASTA_CLEANED="TAIR10_chr_all.fasta"
#sed -e "s/^>Chr/>/" "$FASTA_ORIG" > "$FASTA_CLEANED"
sed -e "s/^>Chr/>/" GCF_000001735.4_TAIR10.1_genomic.fna > "$FASTA_CLEANED"

# ===== Step 3: Samtools Index =====
log "Step 3: Indexing FASTA with samtools"
samtools faidx "$FASTA_CLEANED"

# ===== Step 4: BWA Index =====
log "Step 4: Indexing FASTA with BWA"
bwa index "$FASTA_CLEANED"

# ===== Step 5: GATK Dictionary =====
log "Step 5: Creating GATK Sequence Dictionary"
gatk CreateSequenceDictionary -R "$FASTA_CLEANED"

# ===== Step 6: Download FASTQ Files =====
log "Step 6: Downloading paired-end FASTQ reads"
cd "$TEST_DIR"
FASTQ1_URL="https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR519/SRR519591/SRR519591_1.fastq.gz"
FASTQ2_URL="https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR519/SRR519591/SRR519591_2.fastq.gz"

curl -O "$FASTQ1_URL"
curl -O "$FASTQ2_URL"

# ===== Step 7: Downsample FASTQ =====
log "Step 7: Downsampling FASTQ files to 10 million reads"
zcat SRR519591_1.fastq.gz | seqtk sample - 10000000 > TDr-7_10M_R1.fastq
zcat SRR519591_2.fastq.gz | seqtk sample - 10000000 > TDr-7_10M_R2.fastq

# ===== Step 8: Verify FASTQ =====
log "Step 8: Verifying FASTQ file content"
head -n 10 TDr-7_10M_R1.fastq

# =====NOTE - STEP 9 and 10 are optional for the demo, we can comment the below lines====
# ===== Step 9: Download SNP VCF & Index =====
log "Step 9: Downloading SNP VCF and indexing"
cd "$SNP_DIR"
wget https://1001genomes.org/data/GMI-MPI/releases/v3.1/1001genomes_snp-short-indel_only_ACGTN.vcf.gz
tabix -p vcf 1001genomes_snp-short-indel_only_ACGTN.vcf.gz

# ===== Step 10: Download SNPmatch DB =====
log "Step 10: Downloading SNPmatch database"
wget -O snpmatch_db.tar https://figshare.com/ndownloader/files/9547051
tar -xf snpmatch_db.tar && rm snpmatch_db.tar

log "All steps completed successfully."
