# SPDX-FileCopyrightText: Copyright (c) 2024-2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

#!/bin/bash 

# If the data directory already exists then exit 
DATA_DIR="$PWD/data"
if [ -d $DATA_DIR ]; then
  echo "Data directory already exists, exiting."
  exit
fi

# Make directories for data, references, and output 
mkdir -p $DATA_DIR/ref
mkdir -p $DATA_DIR/output

# Download and decompress the exome FASTQ files
cd "$DATA_DIR" && \
	wget https://ftp-trace.ncbi.nih.gov/ReferenceSamples/giab/data/NA12878/Garvan_NA12878_HG001_HiSeq_Exome/NIST7035_TAAGGCGA_L001_R1_001.fastq.gz && \
	gunzip NIST7035_TAAGGCGA_L001_R1_001.fastq.gz && \
	wget https://ftp-trace.ncbi.nih.gov/ReferenceSamples/giab/data/NA12878/Garvan_NA12878_HG001_HiSeq_Exome/NIST7035_TAAGGCGA_L001_R2_001.fastq.gz && \
	gunzip NIST7035_TAAGGCGA_L001_R2_001.fastq.gz && \
	cd -

# Download and untar the Parabricks sample data
wget -O parabricks_sample.tar.gz https://s3.amazonaws.com/parabricks.sample/parabricks_sample.tar.gz
tar xzvf parabricks_sample.tar.gz

# We only need the reference files from this
mv parabricks_sample/Ref parabricks_sample/ref
mv parabricks_sample/ref $DATA_DIR

# Delete the leftover datafiles and tarball 
rm -rf parabricks_sample*
