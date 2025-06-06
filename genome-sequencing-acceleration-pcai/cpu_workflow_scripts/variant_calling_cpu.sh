#!/bin/bash

REF=/mnt/shared/genome-sequencing-acceleration-pcai/data_preparation_scripts/data_source_arabidopsis_thaliana/TAIR10_chr_all.fasta

TDR_BAM=TDr-7_10M_cpu_sorted_marked.bam

# Number of threads for GATK to use
OMP_NUM_THREADS=7

function haplo_se {
        REF=$1; BAM=$2; PREFIX=$3
        gatk HaplotypeCaller \
                --java-options -Xmx16g \
                --input $BAM \
                --output ${PREFIX}_cpu.vcf \
                --reference ${REF} \
                --native-pair-hmm-threads 16
}

# Call variants on TDr-7 bam file
time haplo_se $REF $TDR_BAM TDr-7_10M
