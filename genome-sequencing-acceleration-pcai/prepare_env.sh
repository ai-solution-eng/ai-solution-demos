#!/bin/bash

# --- Step 1: Define environment variables ---

# Define the PATH and LD_LIBRARY_PATH additions
NEW_PATH="/usr/local/parabricks_related/parabricks/binaries/bin:/usr/local/parabricks_related/parabricks:/usr/local/parabricks_related/nvidia/bin:/usr/local/parabricks_related/cuda/bin:/usr/local/parabricks_related/sbin:/usr/local/parabricks_related/bin:/usr/sbin/parabricks_related:/usr/bin/parabricks_related:/sbin/parabricks_related:/bin/parabricks_related:/bin:/sbin:/usr/bin:/usr/sbin"

NEW_LD_LIBRARY_PATH="/usr/local/parabricks_related/nvidia/lib:/usr/local/parabricks_related/nvidia/lib64:/usr/lib/x86_64-linux-gnu:/usr/lib/parabricks_related/x86_64-linux-gnu:/usr/local/lib64:/usr/local/parabricks_related/lib64:/usr/local/bin/lib:/usr/local/lib64:/usr/local/lib:/usr/local/parabricks_related/lib:/usr/local/parabricks_related/parabricks/binaries/giraffe_libs:/usr/local/parabricks_related/parabricks/binaries/deepvariant_libs:/usr/local/parabricks_related/parabricks/binaries/lib:/usr/local/parabricks_related/parabricks/binaries/tensorrt_libs:/usr/local/bin/lib:/usr/local/lib:/usr/local/parabricks_related/cuda/targets/x86_64-linux/lib:/usr/local/parabricks_related/cuda-12.8/targets/x86_64-linux/lib"

# Export updated LD_LIBRARY_PATH
if [[ -z "$LD_LIBRARY_PATH" ]]; then
    export LD_LIBRARY_PATH="$NEW_LD_LIBRARY_PATH"
else
    export LD_LIBRARY_PATH="$NEW_LD_LIBRARY_PATH:$LD_LIBRARY_PATH"
fi

# Export updated PATH
if [[ -z "$PATH" ]]; then
    export PATH="$NEW_PATH"
else
    export PATH="$NEW_PATH:$PATH"
fi

# --- Step 2: Persist changes in .bashrc ---

echo "Updating current user's .bashrc..."

{
  echo ""
  echo "# Added by prepare_env.sh"
  echo "export PATH=\"$NEW_PATH:\$PATH\""
  echo "export LD_LIBRARY_PATH=\"$NEW_LD_LIBRARY_PATH\""
} >> ~/.bashrc

# Ensure .bashrc is sourced from .bash_profile or .profile
if [ -f ~/.bash_profile ]; then
    if ! grep -qF '. ~/.bashrc' ~/.bash_profile; then
        echo 'Adding .bashrc sourcing to ~/.bash_profile...'
        echo 'if [ -f ~/.bashrc ]; then . ~/.bashrc; fi' >> ~/.bash_profile
    fi
elif [ -f ~/.profile ]; then
    if ! grep -qF '. ~/.bashrc' ~/.profile; then
        echo 'Adding .bashrc sourcing to ~/.profile...'
        echo 'if [ -f ~/.bashrc ]; then . ~/.bashrc; fi' >> ~/.profile
    fi
else
    echo 'Creating ~/.profile and sourcing ~/.bashrc...'
    echo 'if [ -f ~/.bashrc ]; then . ~/.bashrc; fi' > ~/.profile
fi

# --- Step 3: Install tabix ---
echo "Installing tabix..."
echo "root" | su -c "apt update && apt install -y tabix"

# --- Step 4: Apply changes immediately to current terminal session ---
echo "Applying environment changes to current terminal..."
source ~/.bashrc

echo "Environment setup complete."
