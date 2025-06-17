#!/bin/bash

# Download A. thaliana SNPs
wget https://1001genomes.org/data/GMI-MPI/releases/v3.1/1001genomes_snp-short-indel_only_ACGTN.vcf.gz
tabix -p vcf 1001genomes_snp-short-indel_only_ACGTN.vcf.gz

# Download and downsample TDr-7 data
# https://trace.ncbi.nlm.nih.gov/Traces/?view=study&acc=SRP012869
curl https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR519/SRR519591/SRR519591_1.fastq.gz | zcat | seqtk sample - 10000000 > TDr-7_10M_R1.fastq
curl https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR519/SRR519591/SRR519591_2.fastq.gz | zcat | seqtk sample - 10000000 > TDr-7_10M_R2.fastq

# Download the TAIR10 reference and build indices
wget https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/735/GCF_000001735.4_TAIR10.1/GCF_000001735.4_TAIR10.1_genomic.fna.gz
gunzip GCF_000001735.4_TAIR10.1_genomic.fna.gz
sed -e "s/^>Chr/>/" GCF_000001735.4_TAIR10.1_genomic.fna > TAIR10_chr_all.fasta

samtools faidx TAIR10_chr_all.fasta
bwa index TAIR10_chr_all.fasta
gatk CreateSequenceDictionary -R TAIR10_chr_all.fasta

# SNPmatch database for 1001 Genomes
wget https://figshare.com/ndownloader/files/9547051 && tar -xf 9547051 && rm 9547051
