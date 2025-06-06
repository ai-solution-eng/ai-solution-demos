#!/bin/bash

REF=/mnt/shared/genome-sequencing-acceleration-pcai/data_preparation_scripts/data_source_arabidopsis_thaliana/TAIR10_chr_all.fasta

TDR_1=/mnt/shared/genome-sequencing-acceleration-pcai/data_preparation_scripts/data_source_arabidopsis_thaliana/TDr-7_10M_R1.fastq
TDR_2=/mnt/shared/genome-sequencing-acceleration-pcai/data_preparation_scripts/data_source_arabidopsis_thaliana/TDr-7_10M_R2.fastq

# Align TDr-7 reads
time pbrun fq2bam --ref $REF --in-fq $TDR_1 $TDR_2 --out-bam TDr-7_10M_pb_gpu.bam --num-gpus 2

# When using Nvidia T4 GPUs or similar that has less memory (16GB) use the --low-memory option.
# time pbrun fq2bam --ref $REF --in-fq $TDR_1 $TDR_2 --out-bam TDr-7_10M_pb_gpu.bam --num-gpus 2 --low-memory
