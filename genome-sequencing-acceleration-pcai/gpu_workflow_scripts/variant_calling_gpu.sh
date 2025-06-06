#!/bin/bash

REF=/mnt/shared/genome-sequencing-acceleration-pcai/data_preparation_scripts/data_source_arabidopsis_thaliana/TAIR10_chr_all.fasta

TDR_BAM=TDr-7_10M_pb_gpu.bam

# Call variants on TDr-7 bam file
time pbrun haplotypecaller --ref $REF --in-bam $TDR_BAM --out-variants TDr-7_10M_pb.vcf --num-gpus 2


# use --htvc-low-memory if seeing out of memory error in case you are using Nvidia T4 GPUs
# time pbrun haplotypecaller --ref $REF --in-bam $TDR_BAM --out-variants TDr-7_10M_pb.vcf --num-gpus 2 --htvc-low-memory
